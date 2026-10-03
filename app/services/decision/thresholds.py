"""Числовые пороги из матрицы: единая проверка для всех ключей thresholds.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ
=====================
`thresholds` в config/routing_matrix.json — это данные, и заказчик вправе
добавить туда любой числовой порог, не трогая Python. Старая реализация
движка читала ровно один ключ (`min_size_mm`), а остальные молча
игнорировала: триггер срабатывал вопреки собственному порогу. Это не
частный случай, а класс дефекта — «правило есть, проверки нет».

Поэтому здесь пороги описаны ДАННЫМИ:

  * ключ разбирается на префикс (`min_` / `max_`) и метрику (хвост);
  * для каждой метрики есть резолвер: как из находки и текста заключения
    получить наблюдаемое значение и дословную цитату;
  * неизвестная метрика НЕ игнорируется молча (это был исходный баг) и НЕ
    роняет движок: срабатывание блокируется, а в detail попадает имя
    неизвестного ключа, чтобы это было видно врачу и в логах.

КЛИНИЧЕСКАЯ ЛОГИКА
==================
Порог `min_*` — это «степень выраженности не ниже N». Степень выраженности
берётся как МАКСИМУМ по тексту заключения: если где-то в протоколе написано
«до 30 %», а в другом месте «до 45 %», пациент тяжелее во втором месте, и
критерий «значимый стеноз ≥ 70 %» должен смотреть на худший участок.

Если значения в тексте нет, порог считается НЕВЫПОЛНЕННЫМ: нельзя
подтвердить порог, которого нет в протоколе. Это ровно та же политика, что
уже действует для `min_size_mm`, и она даёт врачу проверяемое объяснение
вместо молчаливого пропуска проверки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.extraction.base import Finding

# Метрика → человекочитаемое имя и единица измерения для пояснения врачу.
_METRIC_LABELS = {
    "mm": ("размер", "мм"),
    "percent": ("степень сужения", "%"),
    "birads": ("категория BI-RADS", ""),
}

# Порядок сравнения для разных префиксов ограничений.
_COMPARATORS = {
    "min": lambda observed, limit: observed >= limit,
    "max": lambda observed, limit: observed <= limit,
}

# Маркеры блока заключения: проценты ищем после последнего из них, чтобы
# числа из раздела «Описание» не выдавали степень выраженности.
_CONCLUSION_MARKERS = ("ЗАКЛЮЧЕНИЕ", "ЗАКЛЮЧЕНИЕ:", "ДИАГНОЗ")

# Признаки полной окклюзии. Окклюзия — это стеноз 100 % по определению,
# поэтому она удовлетворяет любому порогу min_stenosis_percent без явного
# числа в тексте. Без этого правила пациент с окклюзией бедренной артерии
# («окклюзия левой ЗББА») терял бы маршрут к сосудистому хирургу.
_OCCLUSION_MARKERS = ("окклюзия", "окклюзирован", "окклюзию", "тотальная окклюзия")

# Порог окклюзии: полное закрытие просвета.
_OCCLUSION_PERCENT = 100.0

# Граница клаузы: цитата не должна начинаться с середины предложения.
_CLAUSE_BREAKS = ".,;:!?()\n—-"

_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%", re.IGNORECASE)
_BIRADS_RE = re.compile(r"BI[\s\-–]*RADS[\s\-–]*(\d)", re.IGNORECASE)
_BIRADS_LOOSE_RE = re.compile(r"(\d)", re.IGNORECASE)


@dataclass(frozen=True)
class Measured:
    """Наблюдаемое значение метрики и дословная цитата, из которой оно взято.

    `quote` — обязательное поле: врач должен видеть, откуда взято число.
    """

    value: float
    quote: str
    source: str = "text"
    """'finding' — из находки декодера, 'text' — из текста заключения."""


@dataclass(frozen=True)
class ThresholdOutcome:
    """Итог проверки всех числовых порогов триггера."""

    passed: bool
    detail: str = ""
    quote: str = ""
    """Цитата-основание для врага; при отказе всегда непустая."""

    checked: tuple[str, ...] = ()
    """Ключи порогов, которые реально проверялись (для отладки и тестов)."""

    skipped: tuple[str, ...] = ()
    """Ключи порогов, которые движок поддержать не смог."""


def metric_of(key: str) -> str:
    """Метрика по ключу порога: «min_size_mm» → «mm», «min_birads» → «birads»."""
    parts = key.split("_")
    return parts[-1].lower() if len(parts) > 1 else ""


def constraint_of(key: str) -> str:
    """Префикс ограничения: «min_…» → «min», без префикса → «min»."""
    lowered = key.lower()
    for prefix in _COMPARATORS:
        if lowered.startswith(prefix + "_"):
            return prefix
    return "min"


def supports_threshold(key: str) -> bool:
    """Умеет ли движок проверить такой порог.

    Используется в тестах (защита от повторения бага) и в
    matrix.validate() (предупреждение врачу о неподдержанном пороге).
    """
    return metric_of(key) in _RESOLVERS


def numeric_thresholds(thresholds: dict) -> list[tuple[str, float]]:
    """Пороги из конфига в виде (ключ, число), отсортированные по ключу.

    Нечисловые значения (строки, списки) пропускаются: это не пороги,
    а декодировать их движок не обязан.
    """
    return sorted(
        (
            (str(key), float(value))
            for key, value in (thresholds or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
    )


def evaluate_thresholds(
    trigger,
    candidate: Finding,
    conclusion_text: str,
) -> ThresholdOutcome:
    """Проверить ВСЕ числовые пороги триггера против находки и текста.

    Args:
        trigger: TriggerDef из матрицы.
        candidate: находка, совпавшая с синонимом триггера.
        conclusion_text: текст протокола; нужен, когда декодер не вытащил
            значение метрики (проценты, BI-RADS) в находку.

    Returns:
        ThresholdOutcome. `passed=False` означает «триггер подавлен порогом».
    """
    limits = numeric_thresholds(trigger.thresholds)
    if not limits:
        return ThresholdOutcome(passed=True, detail="пороги не заданы")

    region = _conclusion_region(conclusion_text)
    checked: list[str] = []
    skipped: list[str] = []

    for key, limit in limits:
        metric = metric_of(key)
        constraint = constraint_of(key)
        compare = _COMPARATORS[constraint]
        label, unit = _METRIC_LABELS.get(metric, (metric or key, ""))
        requirement = f"{label} {_relation(constraint, limit)}{_unit(unit)}"

        if metric not in _RESOLVERS:
            # Не игнорируем молча: порог из конфига мы проверить не можем.
            # Блокируем срабатывание и называем ключ, чтобы это всплыло.
            skipped.append(key)
            return ThresholdOutcome(
                passed=False,
                detail=(
                    f"требуется {requirement}, но порог «{key}» не поддерживается "
                    "движком — срабатывание заблокировано, обратитесь к разработчику"
                ),
                quote=candidate.quote,
                checked=tuple(checked),
                skipped=tuple(skipped),
            )

        measured = _RESOLVERS[metric](candidate, region, conclusion_text)
        checked.append(key)

        if measured is None:
            return ThresholdOutcome(
                passed=False,
                quote=candidate.quote,
                detail=f"требуется {requirement}, в тексте значение не указано",
                checked=tuple(checked),
                skipped=tuple(skipped),
            )

        if not compare(measured.value, limit):
            return ThresholdOutcome(
                passed=False,
                quote=measured.quote or candidate.quote,
                detail=f"требуется {requirement}, в тексте {_number(measured.value)}{_unit(unit)}",
                checked=tuple(checked),
                skipped=tuple(skipped),
            )

    detail = (
        f"пороги выполнены: {', '.join(f'{k}={_number(v)}' for k, v in limits)}"
        if limits
        else "пороги не заданы"
    )
    return ThresholdOutcome(
        passed=True,
        quote=candidate.quote,
        detail=detail,
        checked=tuple(checked),
        skipped=tuple(skipped),
    )


# --------------------------------------------------------------------------
# Резолверы метрик
# --------------------------------------------------------------------------
# Каждый возвращает Measured | None и никогда не бросает исключений:
# «не смогли измерить» — это None, а не падение.


def _resolve_mm(candidate: Finding, region: str, full_text: str = "") -> Measured | None:
    """Размер в мм: сначала находка декодера, затем текст заключения.

    Если в заключении числа нет, ищем во всём протоколе: врач мог описать
    размер в разделе «Описание», а заключение сказать «без динамики».
    Тогда в объяснении будет конкретная цитата вместо «значение не указано».
    """
    if candidate.size_mm is not None:
        return Measured(value=candidate.size_mm, quote=candidate.quote, source="finding")

    for source in _sources(candidate, region, full_text):
        measured = _max_measure(source, _MM_RE, candidate.quote)
        if measured is not None:
            return measured
    return None


def _resolve_percent(
    candidate: Finding, region: str, full_text: str = ""
) -> Measured | None:
    """Степень сужения в процентах: максимум по тексту протокола.

    Ищем по трём источникам по убыванию доверия: заключение → весь
    протокол → цитата самой находки. Последнее важно: декодер мог не
    вытащить число в находку, но его цитата («окклюзия ЗББА») — это
    тоже доказательство из протокола.

    Отдельно ловим окклюзию: это 100 % по определению, даже если в тексте
    стоит «окклюзия ЗББА» без числа. Иначе настоящий пациент с окклюзией
    отсеивается порогом 70 % только потому, что врач не написал процент.
    """
    sources = _sources(candidate, region, full_text)

    percent = _max_measure(region, _PERCENT_RE, candidate.quote)
    if percent is None:
        for source in sources:
            percent = _max_measure(source, _PERCENT_RE, candidate.quote)
            if percent is not None:
                break

    occlusion = None
    for source in sources:
        occlusion = _find_occlusion(source)
        if occlusion is not None:
            break

    if occlusion is None:
        return percent
    if percent is None or percent.value < _OCCLUSION_PERCENT:
        return occlusion
    return percent


def _sources(candidate: Finding, region: str, full_text: str) -> list[str]:
    """Тексты, где ищем значение метрики, в порядке убывания доверия."""
    ordered = [region, full_text, candidate.quote]
    seen: set[str] = set()
    return [text for text in ordered if text and not (text in seen or seen.add(text))]


def _find_occlusion(region: str) -> Measured | None:
    """Есть ли в тексте окклюзия — и дословная цитата с ней."""
    if not region:
        return None
    lowered = region.lower()
    for marker in _OCCLUSION_MARKERS:
        position = lowered.find(marker)
        if position < 0:
            continue
        quote = _clause(region, position)
        if not quote:
            quote = region[position: position + len(marker)]
        return Measured(value=_OCCLUSION_PERCENT, quote=quote, source="text")
    return None


def _resolve_birads(
    candidate: Finding, region: str, full_text: str = ""
) -> Measured | None:
    """Категория BI-RADS: из classification находки, затем из текста."""
    if candidate.classification:
        match = _BIRADS_LOOSE_RE.search(candidate.classification)
        if match:
            return Measured(
                value=float(match.group(1)),
                quote=candidate.classification,
                source="finding",
            )
    for source in _sources(candidate, region, full_text):
        measured = _max_measure(source, _BIRADS_RE, candidate.quote)
        if measured is not None:
            return measured
    return None


_RESOLVERS = {
    "mm": _resolve_mm,
    "percent": _resolve_percent,
    "birads": _resolve_birads,
}

_MM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:[хxX*]\s*\d+(?:[.,]\d+)?)?\s*мм", re.IGNORECASE)


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _conclusion_region(text: str) -> str:
    """Часть протокола, где ищем значение метрики.

    Берём текст от ПЕРВОГО маркера заключения до конца: блок «Заключение»
    обычно заканчивается подписью и «Диагнозом», поэтому брать текст после
    последнего маркера нельзя — тогда выпадает всё, что между ними.
    При этом числа из раздела «Описание» (КИМ 1,1 мм, эхогенность 80 %)
    всё равно не попадают в поиск.

    Нет маркера — ищем по всему протоколу: это грубее, но лучше, чем
    объявить порог невыполненным из-за отсутствия заголовка.
    """
    if not text:
        return ""
    upper = text.upper()
    positions = [p for p in (upper.find(m) for m in _CONCLUSION_MARKERS) if p >= 0]
    return text[min(positions):] if positions else text


def _max_measure(region: str, pattern: re.Pattern, near_quote: str) -> Measured | None:
    """Максимальное значение, найденное паттерном, с дословной цитатой.

    Цитата — клауза целиком: врач видит «со стенозами до 30 %», а не
    безликое «30». Это требование кейса: каждое решение проверяемо.
    """
    best: Measured | None = None
    for match in pattern.finditer(region):
        try:
            value = float(match.group(1).replace(",", "."))
        except (ValueError, IndexError):
            continue
        if best is None or value > best.value:
            best = Measured(value=value, quote=_clause(region, match.start()))
    if best is None:
        return None
    if near_quote and not best.quote:
        return Measured(value=best.value, quote=near_quote, source="text")
    return best


def _clause(text: str, position: int) -> str:
    """Дословный фрагмент текста вокруг позиции — от границы клаузы.

    Возвращается СУБСТРОКА исходного текста без нормализации: цитата
    обязана дословно встречаться в протоколе.
    """
    start = 0
    for index in range(position - 1, -1, -1):
        if text[index] in _CLAUSE_BREAKS:
            start = index + 1
            break
    end = position
    while end < len(text) and text[end] not in _CLAUSE_BREAKS:
        end += 1
    fragment = text[start:end].strip()
    if fragment:
        return fragment
    # Попали на текст без границ клауз — отдаём строку целиком.
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end].strip()


def _relation(constraint: str, limit: float) -> str:
    """«≥ 70» / «≤ 5» — знак сравнения, как его видит врач."""
    symbol = "≥" if constraint == "min" else "≤"
    return f"{symbol} {_number(limit)}"


def _unit(unit: str) -> str:
    """Единица измерения с пробелом; пустая для безразмерных шкал."""
    return f" {unit}" if unit else ""


def _number(value: float) -> str:
    """Число без хвоста «.0» — «70», «30», «12.5»."""
    rounded = round(float(value), 2)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)
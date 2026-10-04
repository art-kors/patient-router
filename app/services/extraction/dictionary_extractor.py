"""Рабочая заглушка декодера: поиск находок по словарям.

ЗАЧЕМ
=====
Декодер пишет сокомандник. Но без него нельзя проверить остальную
систему: маршрут, объяснимость, автомат, экраны. Поэтому здесь —
работающая, но честно ограниченная реализация:

  УМЕЕТ:  найти находку по словарю синонимов из конфига,
          вытащить размер в мм, выставить флаг отрицания.
  НЕ УМЕЕТ: понимать свободный текст, морфологию, би-RADS/ORADS
          в свободной формулировке, несколько находок одного типа.

Это НЕ заглушка-пустышка: на наших протоколах она даёт ненулевой recall
и позволяет прогнать сценарии целиком. Но метрики, посчитанные на ней,
нельзя показывать жюри как «качество распознавания».

ГРАНИЦА ОТВЕТСТВЕННОСТИ ЗА ОТРИЦАНИЯ
==================================
in_negative_context у Finding — это ФАКТ о конкретной находке: «в этом
фрагменте находка упомянута как отсутствующая». Его выставляет декодер.

Итоговое «сработал триггер или нет» решает НЕ декодер, а движок
(DecisionEngine): он дополнительно ищет negative_contexts уже по всему
тексту заключения, потому что знает отрицания всех триггеров сразу.
Поэтому декодер НЕ ищет отрицание по всему протоколу: «Почки — без
очаговых образований» не должно гасить находку матки. Область поиска
декодера — строка находки и следующее за ней предложение («Полип
эндометрия 12 мм. Патологий не выявлено.» — это норма).

ПОДКЛЮЧЕНИЕ РЕАЛЬНОГО ДЕКОДЕРА
==============================
Заменить один класс в app/services/extraction/__init__.py: get_extractor().
Контракт — ProtocolExtractor в base.py. Больше ничего менять не нужно.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.extraction.base import (
    ExtractionResult,
    Finding,
    ProtocolExtractor,
    ProtocolMeta,
)
from app.settings import get_settings

# Размер вида «12х11мм», «26 x 38 мм», «до 8-9 мм» — берём первое число.
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:[хx*]\s*\d+(?:[.,]\d+)?)?\s*мм", re.IGNORECASE)

# Маркеры начала заключения. «Заключение» встречается с двоеточием,
# без него и заглавными; «Диагноз» — отдельный блок примерно у 30 % протоколов.
_CONCLUSION_MARKERS = ("ЗАКЛЮЧЕНИЕ", "Заключение:", "Диагноз")

# Признаки отрицания В СТРОКЕ С НАХОДКОЙ. Список приблизительный —
# это главное, что должен улучшить сокомандник.
#
# Важно: здесь только обороты, которые означают ОТСУТСТВИЕ находки.
# Обороты про динамику или признаки («без динамики от предыдущего узи»,
# «без признаков роста») находку не отменяют — наоборот, они обычно
# стоят в одной фразе с подтверждённой находкой. Раньше они стояли
# в этом списке и гасили настоящие находки (проверено на размеченных
# исследованиях: 2 ложных подавления миомы матки).
_NEGATION_MARKERS = (
    "не выяв",
    "не определя",
    "не обнаруж",
    "не визуализ",
    "не соответств",
    "отсутств",
    "патологии не",
)

# Норма-заявление, которая стоит СРАЗУ ПОСЛЕ находки:
# «Полип эндометрия 12 мм. Патологий не выявлено.»
# Именно такая формулировка чаще всего и означает норму, но к строке
# находки отношения не имеет. Ловим по голове предложения: если первое
# же слово — отрицание, то оно относится к тому, что назвали строкой выше.
# Если же в начале стоит орган или другое название («Почки — без очаговых
# образований»), предложение к находке не относится и не учитывается.
_STATEMENT_NEGATION_RE = re.compile(
    r"^\s*(?:патологи(?:я|и|й|ческих?)|изменени(?:й|я|и)|признаков|очагов)?\s*"
    r"(?:не\s+(?:выявлен\w*|определя\w*|обнаруж\w*|визуализир\w*|соответств\w*)"
    r"|отсутств\w*"
    r"|без\s+(?:очагов\w*\s+образований|признаков|патологии))",
    re.IGNORECASE,
)


class DictionaryExtractor(ProtocolExtractor):
    """Извлечение находок словарём из конфигурации."""

    def __init__(self, config_path: Path | None = None) -> None:
        """
        Args:
            config_path: путь к routing_matrix.json. Если не задан —
                берётся из CONFIG_DIR (по умолчанию ./config).
        """
        self._config_path = config_path
        # Загружаем один раз при старте: дальше только чтение словаря.
        self._synonyms: list[tuple[str, str]] = []
        self._negatives: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        """Прочитать словари из матрицы маршрутизации.

        Структура ожидается такая (см. app/services/decision/matrix.py):
        [
          {"trigger_id": "endometrial_polyp",
           "display_name": "Полип эндометрия",
           "synonyms": ["полип эндометрия", ...],
           "negative_contexts": ["полипа не выявлено", ...]}
        ]
        """
        # Путь к матрице берём из настроек, чтобы он совпадал с путём
        # остального приложения (в контейнере это /app/config).
        path = self._config_path or get_settings().routing_matrix_path
        if not path or not Path(path).exists():
            # Нет конфига — детектор работает, но ничего не найдёт.
            # Это лучше, чем падать на старте: остальная система жива.
            return

        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        for trigger in raw if isinstance(raw, list) else raw.get("triggers", []):
            name = trigger.get("display_name") or trigger.get("trigger_id", "")
            negatives = list(trigger.get("negative_contexts") or [])
            self._negatives[name] = negatives
            for synonym in trigger.get("synonyms") or []:
                self._synonyms.append((synonym.lower(), name))

    @property
    def name(self) -> str:
        return f"DictionaryExtractor({len(self._synonyms)} синонимов)"

    def extract(self, text: str, *, study_type: str | None = None) -> ExtractionResult:
        """Найти находки по словарю.

        Алгоритм:
          1. Найти блок заключения (иначе ищем по всему тексту).
          2. Для каждого синонима найти первое вхождение.
          3. Проверить отрицание: строка находки, negative_contexts этой
             находки и следующее предложение (норма может стоять
             отдельной строкой: «Патологий не выявлено»).
          4. Вытащить размер, если он есть в той же строке.
        """
        if not text or not self._synonyms:
            return ExtractionResult(
                extractor_name=self.name,
                raw_text=text,
                conclusion_extracted=False,
            )

        haystack, offset = self._conclusion_span(text)
        # offset > 0 означает, что блок заключения найден и мы ищем в нём.
        # Если маркера нет — ищем по всему тексту, conclusion_extracted=False.
        conclusion_found = offset > 0
        lowered = haystack.lower()

        # Одно совпадение на находку: дубли не несут информации.
        seen: set[str] = set()
        findings: list[Finding] = []

        for synonym, display_name in self._synonyms:
            if display_name in seen:
                continue

            pos = lowered.find(synonym)
            if pos < 0:
                continue

            start, end = pos, pos + len(synonym)
            quote = haystack[start:end]
            line_start = haystack.rfind("\n", 0, start) + 1
            line_end = haystack.find("\n", end)
            if line_end < 0:
                line_end = len(haystack)
            line = haystack[line_start:line_end]

            findings.append(
                Finding(
                    finding=display_name,
                    quote=quote,
                    char_start=start + offset,
                    char_end=end + offset,
                    size_mm=_extract_size(line),
                    in_negative_context=_is_negative(
                        haystack,
                        line_start,
                        line_end,
                        display_name,
                        self._negatives.get(display_name, []),
                        _next_statement(haystack, line_end),
                    ),
                    confidence=1.0,  # словарное совпадение — уверенность высокая
                    source_section="conclusion" if offset else "text",
                )
            )
            seen.add(display_name)

        return ExtractionResult(
            meta=ProtocolMeta(study_type=study_type),
            findings=findings,
            conclusion_text=haystack,
            conclusion_extracted=conclusion_found,
            extractor_name=self.name,
            raw_text=text,
        )

    @staticmethod
    def _conclusion_span(text: str) -> tuple[str, int]:
        """Где начинается заключение. Возвращает (текст_для_поиска, смещение)."""
        upper = text.upper()
        for marker in _CONCLUSION_MARKERS:
            pos = upper.find(marker)
            if pos >= 0:
                return text[pos:], pos
        return text, 0


def _extract_size(line: str) -> float | None:
    """Первое число с единицей измерения мм в строке."""
    match = _SIZE_RE.search(line)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _next_statement(haystack: str, after: int) -> str:
    """Первое предложение после позиции `after` (или пустая строка).

    Нужно, чтобы поймать норму, записанную отдельной строкой:
    «Полип эндометрия 12 мм. Патологий не выявлено.»
    """
    rest = haystack[after:]
    stripped = rest.lstrip(" \t\r\n")
    for terminator in (".", ";", "!", "?"):
        idx = stripped.find(terminator)
        if 0 <= idx <= 400:
            return stripped[: idx + 1]
    return ""


def _is_negative(
    haystack: str,
    line_start: int,
    line_end: int,
    display_name: str,
    negatives: list[str],
    next_statement: str = "",
) -> bool:
    """Отрицана ли находка. Три источника, все детерминированные.

    1. СТРОКА находки: общий оборот отрицания — «полипа не выявлено».
    2. negative_contexts ЭТОЙ находки в той же строке — то, ради чего
       поле читается при загрузке матрицы.
    3. СЛЕДУЮЩЕЕ предложение, если оно начинается с отрицания:
       «Полип эндометрия 12 мм. Патологий не выявлено.» — типичная
       норма, у которой отдельного упоминания находки нет.

    Чего здесь НЕТ и почему:
      * поиска по всему заключению — отрицание другого органа
        («Почки — без очаговых образований») не должно гасить находку
        матки. Глобальный поиск по тексту и по шаблонам ВСЕХ триггеров
        — забота движка (DecisionEngine._find_negation): он отвечает за
        итог «сработало / подавлено», и «Почки — без очаговых
        образований» гасит там не находку, а триггер;
      * шаблонов этой находки в СЛЕДУЮЩЕМ предложении — там они обычно
        относятся к другому органу, а если относятся к находке, то их
        найдёт движок по всему тексту.

    Здесь мы сообщаем только факт о конкретной находке: «в этом
    фрагменте находка упомянута как отсутствующая».
    """
    line = haystack[line_start:line_end].lower()
    if any(marker in line for marker in _NEGATION_MARKERS):
        return True

    if any(pattern.lower() in line for pattern in negatives if pattern):
        return True

    return bool(next_statement and _STATEMENT_NEGATION_RE.match(next_statement))

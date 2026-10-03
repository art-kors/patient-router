"""Движок решений: находки → триггеры → маршрут.

ЗДЕСЬ ЖИВЁТ КЛЮЧЕВОЕ ОТЛИЧИЕ ПРОЕКТА ОТ КОНКУРЕНТОВ
===================================================
Webiomed и СберМедДи извлекают факты и на этом останавливаются.
Мы идём дальше и принимаем решение ДЕТЕРМИНИРОВАННО, по правилам.

Почему это важно:

1. ВОСПРОИЗВОДИМОСТЬ. Один и тот же протокол всегда даёт один и тот же
   маршрут. LLM может «передумать» между запусками.

2. ОБЪЯСНИМОСТЬ. Решение описывается как «правило X версии Y сработало
   на цитате Z». Не «модель решила так».

3. БЕЗОПАСНОСТЬ. У LLM нет прав назначить лечение или поставить диагноз.
   Она читает текст. Решение принимает код, проверенный врачом.

4. ВЕРСИОНИРОВАНИЕ. Поменяли порог — новая версия правила в объяснении.

ГЛАВНЫЙ ПРИНЦИП — ОБЪЯСНЕНИЕ ВСЕГО
===================================
Для каждого триггера, сработавшего И НЕ сработавшего, мы храним причину.
Кейс требует: «Для нормы — почему триггер не сработал, например из-за
отрицания». Это не опция, а ядро качества: без объяснения норм мы не
можем измерить ложные срабатывания (целевая метрика ≤ 0.05).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.decision.matrix import TriggerDef, load_triggers
from app.services.decision.thresholds import evaluate_thresholds
from app.services.extraction.base import Finding


@dataclass
class TriggerMatch:
    """Результат проверки одного триггера против одного протокола.

    Содержит и срабатывания, и подавления. Объект один для обоих случаев
    намеренно: в базе тоже одна таблица, и в дашборде «почему норма не
    сработала» получается тем же запросом, что и «почему сработала».
    """

    trigger: TriggerDef
    fired: bool
    suppressed: bool = False
    suppression_reason: str | None = None
    """negative_context | threshold_not_met | study_type_mismatch | no_match"""

    quote: str = ""
    """Цитата, на которой основано решение. Для нормы — тоже заполняется."""

    confidence: float = 0.0
    applied_rule: str = ""
    """Человекочитаемая строка для интерфейса: «endometrial_polyp@v1»."""

    detail: str = ""
    """Пояснение для врача: «порог 10 мм, найдено 8 мм»."""

    @property
    def explanation(self) -> dict:
        """Структура для JSON-ответа и для записи в БД."""
        return {
            "trigger_id": self.trigger.trigger_id,
            "display_name": self.trigger.display_name,
            "fired": self.fired,
            "suppressed": self.suppressed,
            "suppression_reason": self.suppression_reason,
            "quote": self.quote,
            "confidence": self.confidence,
            "applied_rule": self.applied_rule,
            "detail": self.detail,
            "version": self.trigger.version,
        }


@dataclass
class DecisionResult:
    """Итог работы движка по одному протоколу."""

    matches: list[TriggerMatch] = field(default_factory=list)
    fired: list[TriggerMatch] = field(default_factory=list)
    """Сработавшие — в порядке приоритета, первый = победитель."""

    suppressed: list[TriggerMatch] = field(default_factory=list)

    @property
    def winner(self) -> TriggerMatch | None:
        """Триггер, по которому создаётся маршрут. None — норма."""
        return self.fired[0] if self.fired else None

    @property
    def is_emergency(self) -> bool:
        """Есть ли экстренная находка: маршрут не создаём, зовём персонал."""
        return any(m.trigger.emergency_flag and m.fired for m in self.fired)


class DecisionEngine:
    """Детерминированный движок: находки + матрица → решение о маршруте.

    Порядок проверки каждого триггера важен и не случаен:
      1. Соответствие типу исследования — иначе ищем не там.
      2. Наличие синонима в тексте.
      3. Отрицательный контекст — гасим находку.
      4. Числовые пороги из матрицы — ВСЕ, а не один захардкоженный.
         Логика проверки живёт в thresholds.py: она общая для любого
         числового порога, добавленного заказчиком в routing_matrix.json.
      5. Флаг экстренности.
    """

    def __init__(self, triggers: list[TriggerDef] | None = None) -> None:
        self._triggers = triggers if triggers is not None else load_triggers()

    @property
    def triggers(self) -> list[TriggerDef]:
        return self._triggers

    def decide(
        self,
        findings: list[Finding],
        *,
        study_type: str | None = None,
        conclusion_text: str = "",
    ) -> DecisionResult:
        """Разобрать находки и решить, создавать ли маршрут.

        Args:
            findings: находки от декодера.
            study_type: тип исследования; ограничивает поиск по матрице.
            conclusion_text: полный текст заключения — нужен для поиска
                отрицаний и порогов, которых может не быть в находке.
        """
        result = DecisionResult()
        haystack = conclusion_text.lower()

        for trigger in self._triggers:
            match = self._evaluate(trigger, findings, study_type, haystack, conclusion_text)
            result.matches.append(match)
            if match.fired:
                result.fired.append(match)
            else:
                result.suppressed.append(match)

        # Приоритет: меньше число = важнее. При равенстве — порядок в матрице.
        result.fired.sort(key=lambda m: m.trigger.priority)
        return result

    def _evaluate(
        self,
        trigger: TriggerDef,
        findings: list[Finding],
        study_type: str | None,
        haystack: str,
        conclusion_text: str = "",
    ) -> TriggerMatch:
        """Проверить один триггер. Возвращает и срабатывание, и подавление.

        Args:
            haystack: текст заключения в нижнем регистре — для поиска
                отрицаний (регистронезависимо).
            conclusion_text: тот же текст в исходном регистре — для поиска
                значений порогов. Нужен именно в исходном виде: цитата в
                отказе по порогу обязана дословно встречаться в протоколе,
                а lower() это разрушает.
        """
        rule = f"{trigger.trigger_id}@v{trigger.version}"

        # 1. Тип исследования: ищем полип эндометрия в УЗИ почек — бессмысленно.
        if study_type and trigger.source_study and study_type != trigger.source_study:
            return TriggerMatch(
                trigger=trigger,
                fired=False,
                suppressed=True,
                suppression_reason="study_type_mismatch",
                applied_rule=rule,
                detail=f"триггер только для «{trigger.source_study}», получено «{study_type}»",
            )

        # 2. Находим находку, совпавшую с синонимом.
        candidate = self._find_candidate(trigger, findings)
        if candidate is None:
            return TriggerMatch(
                trigger=trigger,
                fired=False,
                suppressed=False,
                suppression_reason="no_match",
                applied_rule=rule,
                detail="ни один синоним не встретился в тексте",
            )

        # 3. Отрицание. Проверяем и флаг декодера, и текст заключения:
        #    декодер мог пропустить, а шаблон отрицания в матрице — нет.
        negation_quote = self._find_negation(trigger, haystack)
        if candidate.in_negative_context or negation_quote:
            quote = candidate.quote or negation_quote
            return TriggerMatch(
                trigger=trigger,
                fired=False,
                suppressed=True,
                suppression_reason="negative_context",
                quote=quote,
                confidence=candidate.confidence,
                applied_rule=rule,
                detail=(
                    "находка упомянута в отрицательном контексте"
                    if candidate.in_negative_context
                    else f"в тексте есть отрицание: «{negation_quote}»"
                ),
            )

        # 4. Пороги. Проверяются ВСЕ числовые пороги триггера из матрицы,
        #    а не только min_size_mm: неподдержанный или невыполненный
        #    порог гасит срабатывание и объясняет врачу почему.
        outcome = evaluate_thresholds(trigger, candidate, conclusion_text)
        if not outcome.passed:
            return TriggerMatch(
                trigger=trigger,
                fired=False,
                suppressed=True,
                suppression_reason="threshold_not_met",
                quote=outcome.quote or candidate.quote,
                confidence=candidate.confidence,
                applied_rule=rule,
                detail=outcome.detail,
            )

        # Всё сошлось — триггер сработал.
        return TriggerMatch(
            trigger=trigger,
            fired=True,
            quote=candidate.quote,
            confidence=candidate.confidence,
            applied_rule=rule,
            detail=outcome.detail or "находка найдена, отрицания нет, порог выполнен",
        )

    @staticmethod
    def _find_candidate(trigger: TriggerDef, findings: list[Finding]) -> Finding | None:
        """Первая находка, совпавшая с синонимом триггера.

        Сопоставляем по названию находки: декодер уже нормализовал его
        (например, «Полип эндометрия»), а синонимы в матрице записаны
        в нижнем регистре и включают словоформы («полипа эндометрия»).
        """
        for finding in findings:
            lowered = finding.finding.lower()
            if any(s.lower() in lowered for s in trigger.synonyms):
                return finding
        return None

    @staticmethod
    def _find_negation(trigger: TriggerDef, haystack: str) -> str:
        """Ищем шаблон отрицания этого триггера в тексте заключения."""
        for negative in trigger.negative_contexts:
            if negative.lower() in haystack:
                return negative
        return ""

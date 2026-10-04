"""Тесты отрицаний: экстрактор -> движок -> срабатывание триггера.

ЧТО ФИКСИРУЕТ ФАЙЛ
==================
Раньше DictionaryExtractor загружал negative_contexts из
config/routing_matrix.json и НЕ ИСПОЛЬЗОВАЛ их: поле self._negatives
было мёртвым, а _is_negative() смотрел только в строку совпадения.
Из-за этого норма вида «Полип эндометрия. Патологий не выявлено.» давала
находку с in_negative_context=False и триггер СРАБАТЫВАЛ — ложное
срабатывание на норме. Для кейса это худшая ошибка из возможных.

Что проверяем:
  1. норма не даёт срабатывания ни через экстрактор, ни через движок;
  2. норма-заявление в СЛЕДУЮЩЕМ предложении распознаётся;
  3. negative_contexts из матрицы реально применяются (не мёртвое поле);
  4. обороты «без динамики» / «без признаков роста» НЕ гасят находку:
     они описывают подтверждённую находку, а не её отсутствие;
  5. отрицание чужого органа не гасит находку другого;
  6. у подавленной находки остаётся дословная цитата.

Матрица настоящая (config/routing_matrix.json) — тест должен ломаться
вместе с конфигом, иначе он ничего не защищает.
"""

import pytest

from app.services.decision import DecisionEngine, load_triggers
from app.services.extraction import get_extractor


@pytest.fixture(scope="module")
def extractor():
    return get_extractor()


@pytest.fixture(scope="module")
def engine():
    return DecisionEngine(load_triggers())


def match_of(engine, extractor, text, trigger_id, study_type=None):
    """Прогнать текст по всей цепочке и вернуть TriggerMatch триггера."""
    result = extractor.extract(text, study_type=study_type)
    decision = engine.decide(
        result.findings,
        study_type=study_type,
        conclusion_text=result.conclusion_text,
    )
    match = next(m for m in decision.matches if m.trigger.trigger_id == trigger_id)
    return match, result


class TestNormDoesNotFire:
    """Ключевой инвариант кейса: норма не превращается в маршрут."""

    @pytest.mark.parametrize(
        ("text", "trigger_id"),
        [
            ("ЗАКЛЮЧЕНИЕ\nПолип эндометрия не выявлен.", "endometrial_polyp"),
            ("ЗАКЛЮЧЕНИЕ\nПолип полости матки не выявлен.", "endometrial_polyp"),
            ("ЗАКЛЮЧЕНИЕ\nГиперплазия эндометрия не выявлена.", "endometrial_polyp"),
            ("ЗАКЛЮЧЕНИЕ\nОбразование яичника не выявлено.", "ovarian_cyst"),
            ("ЗАКЛЮЧЕНИЕ\nСубмукозной миомы не выявлено.", "submucosal_fibroid"),
            ("ЗАКЛЮЧЕНИЕ\nУзел щитовидной железы не выявлен.", "thyroid_nodule"),
            ("ЗАКЛЮЧЕНИЕ\nПолип желчного пузыря не выявлен.", "gallbladder_polyp"),
            ("ЗАКЛЮЧЕНИЕ\nКамни в желчном пузыре не выявлены.", "cholelithiasis"),
            ("ЗАКЛЮЧЕНИЕ\nКонкремент почки не выявлен.", "hydronephrosis"),
        ],
    )
    def test_норма_не_срабатывает(self, engine, extractor, text, trigger_id):
        match, _ = match_of(engine, extractor, text, trigger_id)
        assert match.fired is False, "норма дала срабатывание триггера"
        assert match.suppression_reason == "negative_context"

    @pytest.mark.parametrize(
        ("text", "trigger_id"),
        [
            # Отрицание стоит ОТДЕЛЬНЫМ предложением после находки.
            ("ЗАКЛЮЧЕНИЕ\nПолип эндометрия 12 мм.\nПатологий не выявлено.", "endometrial_polyp"),
            ("ЗАКЛЮЧЕНИЕ\nМиома матки 40 мм.\nПатологий не выявлено.", "fibroid_uterus"),
            ("ЗАКЛЮЧЕНИЕ\nОбразование яичника 30 мм.\nПатологий не выявлено.", "ovarian_cyst"),
            ("ЗАКЛЮЧЕНИЕ\nКамни в желчном пузыре.\nНе выявлено.", "cholelithiasis"),
            ("ЗАКЛЮЧЕНИЕ\nСубмукозная миома 25 мм.\nНе выявлено.", "submucosal_fibroid"),
        ],
    )
    def test_норма_в_следующем_предложении_не_срабатывает(
        self, engine, extractor, text, trigger_id
    ):
        """Та самая формулировка, ради которой чинился дефект.

        Находка названа в одной строке, норма — в следующей. Раньше
        отрицание не попадало в область поиска, и триггер срабатывал.
        """
        match, result = match_of(engine, extractor, text, trigger_id)
        assert match.fired is False, f"«{text.splitlines()[1]}» + норма следом дала срабатывание"
        assert match.suppression_reason == "negative_context"
        assert result.findings, "находка должна быть извлечена, но помечена отрицанием"
        assert all(f.in_negative_context for f in result.findings)

    def test_отрицательная_классификация_гасит_триггер_в_движке(self, engine, extractor):
        """ORADS 1 из negative_contexts яичников — норма, а не находка.

        Классификация стоит отдельной строкой, поэтому находку не
        гасит декодер (у него область — строка находки), а движок:
        он ищет negative_contexts по всему заключению.
        """
        text = "ЗАКЛЮЧЕНИЕ\nОбразование яичника 12 мм.\nORADS 1."
        match, _ = match_of(engine, extractor, text, "ovarian_cyst")
        assert match.fired is False
        assert match.suppression_reason == "negative_context"

    def test_у_нормы_остаётся_дословная_цитата(self, engine, extractor):
        """Объяснение нормы должно на чём-то опираться (кейс: подсветка)."""
        text = "ЗАКЛЮЧЕНИЕ\nПолип эндометрия не выявлен."
        match, _ = match_of(engine, extractor, text, "endometrial_polyp")
        assert match.quote and match.quote in text
        assert match.applied_rule.startswith("endometrial_polyp@")


class TestNegativeContextsAreUsed:
    """Поле negative_contexts не должно быть мёртвым."""

    def test_шаблоны_матрицы_применяются_экстрактором(self, extractor):
        """Фраза из negative_contexts гасит находку сама по себе.

        «ORADS 1» нет ни в одном общем маркере отрицания — сработать
        может только шаблон из routing_matrix.json.
        """
        text = "ЗАКЛЮЧЕНИЕ\nУЗ-признаки параовариальной кисты справа, ORADS 1."
        result = extractor.extract(text)
        assert result.findings, "находка должна быть найдена"
        assert result.findings[0].in_negative_context is True

    def test_каждый_триггер_загружает_свои_отрицания(self, extractor):
        """Поле загружено и не пустое — иначе это снова мёртвый код."""
        loaded = getattr(extractor, "_negatives", {})
        assert loaded, "negative_contexts не загружены"
        for trigger in load_triggers():
            assert loaded.get(trigger.display_name), (
                f"у триггера {trigger.trigger_id} нет загруженных отрицаний, "
                f"хотя в матрице их {len(trigger.negative_contexts)}"
            )


class TestNegationScope:
    """Слишком широкая проверка тоже дефект — она теряет находки."""

    def test_оборот_про_динамику_не_гасит_находку(self, engine, extractor):
        """«без динамики» описывает подтверждённую находку, а не её отсутствие.

        Реальный протокол: «Миома матки (без динамики от предыдущего узи)».
        Раньше этот оборот стоял в общем списке маркеров и гасил находку,
        то есть превращался в пропуск патологии.
        """
        text = "ЗАКЛЮЧЕНИЕ\nМиома матки 40 мм (без динамики от предыдущего узи)."
        match, result = match_of(engine, extractor, text, "fibroid_uterus")
        assert result.findings[0].in_negative_context is False
        assert match.fired is True

    def test_без_признаков_роста_не_гасит_находку(self, engine, extractor):
        text = "ЗАКЛЮЧЕНИЕ\nМиомы матки 12 мм без признаков роста."
        match, result = match_of(engine, extractor, text, "fibroid_uterus")
        assert result.findings[0].in_negative_context is False
        assert match.fired is True

    def test_отрицание_чужого_органа_не_гасит(self, extractor):
        """Экстрактор отвечает за факт о находке, а не за весь текст.

        «Почки — без очаговых образований» не отменяет миому матки.
        """
        text = "ЗАКЛЮЧЕНИЕ\nМиома матки 40 мм.\nПочки — без очаговых образований."
        result = extractor.extract(text)
        assert result.findings[0].in_negative_context is False

    def test_настоящая_находка_даёт_срабатывание(self, engine, extractor):
        """Проверка на то, что тесты выше не проходят «за счёт» тишины."""
        text = "ЗАКЛЮЧЕНИЕ\nПолип эндометрия 18 мм.\nРекомендована консультация гинеколога."
        match, _ = match_of(engine, extractor, text, "endometrial_polyp")
        assert match.fired is True
        assert match.suppression_reason is None

"""Тесты движка решений.

Это ЯДРО проекта: здесь проверяется, что маршрут выбирается правильно
и что объяснение есть в каждом случае — и при срабатывании, и при
подавлении. Требование кейса: «Для нормы — почему триггер не сработал,
например из-за отрицания».

Тесты не зависят от БД и от реального декодера: находки конструируются
вручную, триггеры — из config/routing_matrix.json.
"""

import pytest

from app.services.decision import DecisionEngine, load_triggers, validate
from app.services.extraction.base import Finding


# Хелпер: сделать находку с минимальными обязательными полями.
def finding(name: str, quote: str = "цитата", **kw) -> Finding:
    return Finding(finding=name, quote=quote, **kw)


@pytest.fixture(scope="module")
def engine() -> DecisionEngine:
    return DecisionEngine(load_triggers())


class TestMatrixLoading:
    def test_матрица_читается(self):
        triggers = load_triggers()
        assert len(triggers) >= 10

    def test_у_каждого_тригггера_есть_синонимы(self):
        for trigger in load_triggers():
            assert trigger.synonyms, f"{trigger.trigger_id} без синонимов"

    def test_у_каждого_тригггера_есть_профиль(self):
        for trigger in load_triggers():
            assert trigger.specialty, f"{trigger.trigger_id} без специальности"

    def test_валидация_не_падает(self):
        """Матрица заполнена врачом не полностью — предупреждения допустимы."""
        warnings = validate(load_triggers())
        assert isinstance(warnings, list)

    def test_экстренный_триггер_с_приоритетом_1(self):
        for trigger in load_triggers():
            if trigger.emergency_flag:
                assert trigger.priority == 1, f"{trigger.trigger_id}: emergency требует priority=1"


class TestFiring:
    """Триггер должен сработать, когда находка есть, отрицания нет, порог выполнен."""

    def test_полип_эндометрия_срабатывает(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Полип эндометрия", "Эхографические признаки полипа эндометрия")],
            study_type="УЗИ органов малого таза",
            conclusion_text="УЗ-признаки полипа эндометрия",
        )
        assert result.winner is not None
        assert result.winner.trigger.trigger_id == "endometrial_polyp"
        assert result.winner.fired is True

    def test_у_сработавшего_есть_цитата(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Миома матки", "УЗ признаки миомы матки 30 мм", size_mm=30)],
            study_type="УЗИ органов малого таза",
        )
        # winner — тот триггер, по которому создаётся маршрут;
        # у него обязана быть цитата-доказательство.
        assert result.winner is not None
        assert result.winner.quote == "УЗ признаки миомы матки 30 мм"

    def test_у_сработавшего_есть_версия_правила(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Миома матки", size_mm=30)], study_type="УЗИ органов малого таза"
        )
        assert result.winner is not None
        assert "@v" in result.winner.applied_rule

    def test_порог_без_размера_даёт_подавление_а_не_маршрут(self, engine: DecisionEngine):
        """У триггера «миома матки» порог 5 мм.

        Без размера в тексте порог подтвердить нельзя, поэтому маршрут
        НЕ создаётся, но объяснение «почему» остаётся — этого требует кейс.
        """
        result = engine.decide([finding("Миома матки")], study_type="УЗИ органов малого таза")
        match = next(m for m in result.suppressed if m.trigger.trigger_id == "fibroid_uterus")
        assert match.suppression_reason == "threshold_not_met"
        assert "не указан" in match.detail


class TestNegation:
    """НОРМА НЕ ДОЛЖНА ПРЕВРАЩАТЬСЯ В ТРИГГЕР. Целевая метрика: ≤ 0.05."""

    def test_отрицание_гасит_триггер(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Миома матки", "миомы матки", in_negative_context=True)],
            study_type="УЗИ органов малого таза",
        )
        match = next(m for m in result.suppressed if m.trigger.trigger_id == "fibroid_uterus")
        assert match.suppressed is True
        assert match.suppression_reason == "negative_context"
        assert "отрицательном контексте" in match.detail

    def test_отрицание_из_текста_гасит_триггер(self, engine: DecisionEngine):
        """Декодер мог пропустить флаг, но шаблон отрицания в матрице сработает."""
        result = engine.decide(
            [finding("Миома матки", "миомы матки")],
            study_type="УЗИ органов малого таза",
            conclusion_text="достоверно узловых и очаговых образований не определяется",
        )
        match = next(m for m in result.suppressed if m.trigger.trigger_id == "fibroid_uterus")
        assert match.suppression_reason == "negative_context"
        assert match.quote  # цитата отрицания заполнена

    def test_оргас_не_срабатывает(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Образование молочной железы BI-RADS 3–5")],
            study_type="УЗИ органов малого таза",
        )
        assert result.winner is None, "BI-RADS не должен сработать на УЗИ МТ"

    def test_норма_без_находок_даёт_объяснение_для_каждого(self, engine: DecisionEngine):
        """Для КАЖДОГО триггера есть запись, даже если он просто не совпал."""
        result = engine.decide([], conclusion_text="УЗ патологии не выявлено")
        assert len(result.matches) == len(engine.triggers)
        assert result.winner is None
        for match in result.matches:
            assert match.applied_rule
            assert match.detail


class TestThresholds:
    """Пороги отделяют «наблюдение» от «маршрут в операцию»."""

    def test_полип_жп_8мм_не_срабатывает(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Полип желчного пузыря", "полип желчного пузыря 8 мм", size_mm=8)],
            study_type="УЗИ брюшной полости",
        )
        match = next(m for m in result.suppressed if m.trigger.trigger_id == "gallbladder_polyp")
        assert match.suppression_reason == "threshold_not_met"
        assert "8" in match.detail and "10" in match.detail

    def test_полип_жп_12мм_срабатывает(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Полип желчного пузыря", "полип 12 мм", size_mm=12)],
            study_type="УЗИ брюшной полости",
        )
        assert result.winner.trigger.trigger_id == "gallbladder_polyp"

    def test_узел_щж_без_размера_не_срабатывает(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Узловое образование щитовидной железы")],
            study_type="УЗИ щитовидной железы",
        )
        match = next(m for m in result.suppressed if m.trigger.trigger_id == "thyroid_nodule")
        assert match.suppression_reason == "threshold_not_met"
        assert "не указан" in match.detail


class TestPriority:
    """Если триггеров несколько, побеждает более приоритетный."""

    def test_онкология_побеждает_плановую_гинекологию(self, engine: DecisionEngine):
        result = engine.decide(
            [
                finding("Полип эндометрия", quote="полип эндометрия"),
                finding("Образование яичника", quote="образование яичника 50 мм", size_mm=50),
            ],
            study_type="УЗИ органов малого таза",
        )
        assert len(result.fired) >= 2
        # первый = минимальный priority
        priorities = [m.trigger.priority for m in result.fired]
        assert priorities == sorted(priorities)

    def test_приоритеты_отсортированы(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Миома матки"), finding("Образование яичника", size_mm=50)],
            study_type="УЗИ органов малого таза",
        )
        assert [m.trigger.priority for m in result.fired] == sorted(
            m.trigger.priority for m in result.fired
        )


class TestEmergencySafety:
    """ТРЕБОВАНИЕ КЕЙСА: экстренные находки — только эскалация персоналу."""

    def test_экстренный_триггер_помечен(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Значимый стеноз артерий нижних конечностей", "окклюзия ЗББА")],
            study_type="УЗДГ артерий нижних конечностей",
        )
        assert result.is_emergency is True

    def test_обычный_триггер_не_экстренный(self, engine: DecisionEngine):
        result = engine.decide([finding("Полип эндометрия")], study_type="УЗИ органов малого таза")
        assert result.is_emergency is False

    def test_в_матрице_есть_экстренный_триггер(self, engine: DecisionEngine):
        assert any(t.emergency_flag for t in engine.triggers), (
            "нужен хотя бы один emergency-триггер для демонстрации safety"
        )


class TestExplanationCompleteness:
    """Объяснение обязано быть у КАЖДОГО решения — и в этом суть качества."""

    def test_все_матчи_объяснены(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Миома матки", "миомы матки 30 мм", size_mm=30)],
            study_type="УЗИ органов малого таза",
            conclusion_text="УЗ признаки миомы матки 30 мм",
        )
        for match in result.matches:
            data = match.explanation
            assert data["applied_rule"], f"{match.trigger.trigger_id} без правила"
            assert data["detail"], f"{match.trigger.trigger_id} без пояснения"
            assert data["version"] >= 1

    def test_подавленный_имеет_причину(self, engine: DecisionEngine):
        result = engine.decide([], conclusion_text="норма")
        for match in result.suppressed:
            assert match.suppression_reason is not None

    def test_сработавший_не_подавлен(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Миома матки", size_mm=30)], study_type="УЗИ органов малого таза"
        )
        for match in result.fired:
            assert match.suppressed is False
            assert match.suppression_reason is None

    def test_соотношение_срабатываний_и_подавлений(self, engine: DecisionEngine):
        result = engine.decide(
            [finding("Миома матки", size_mm=30)], study_type="УЗИ органов малого таза"
        )
        assert len(result.fired) + len(result.suppressed) == len(result.matches)


class TestDeterminism:
    """Один и тот же вход всегда даёт один и тот же выход."""

    def test_повторный_запуск_даёт_тот_же_результат(self, engine: DecisionEngine):
        kwargs = {
            "study_type": "УЗИ органов малого таза",
            "conclusion_text": "УЗ признаки миомы матки 30 мм",
        }
        findings = [finding("Миома матки", size_mm=30)]
        first = engine.decide(findings, **kwargs)
        second = engine.decide(findings, **kwargs)
        assert [m.explanation for m in first.matches] == [m.explanation for m in second.matches]

    def test_порядок_триггеров_стабилен(self, engine: DecisionEngine):
        a = engine.decide([], conclusion_text="норма")
        b = engine.decide([], conclusion_text="норма")
        assert [m.trigger.trigger_id for m in a.matches] == [
            m.trigger.trigger_id for m in b.matches
        ]


class TestEmptyInput:
    def test_пустой_список_находок(self, engine: DecisionEngine):
        result = engine.decide([])
        assert result.winner is None
        assert len(result.matches) == len(engine.triggers)

    def test_пустой_текст_заключения(self, engine: DecisionEngine):
        result = engine.decide([finding("Полип эндометрия")], conclusion_text="")
        assert isinstance(result.matches, list)

    def test_без_типа_исследования(self, engine: DecisionEngine):
        """Без подсказки о типе ищем по всем триггерам."""
        result = engine.decide([finding("Миома матки", size_mm=30)])
        assert result.winner is not None

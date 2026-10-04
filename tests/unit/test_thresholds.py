"""Числовые пороги матрицы: проверяются ВСЕ, а не один захардкоженный.

ИСТОРИЯ БАГА (её нельзя повторить)
==================================
Движок читал ровно один порог — `min_size_mm`. Все остальные числовые
пороги из config/routing_matrix.json игнорировались МОЛЧА: триггер
срабатывал вопреки собственному порогу.

На данных это был единственный FP из 204 записей разметки:
`demo_lower_limb_07` — в заключении «со стенозами до 30%», порог в
матрице 70 %, а триггер создавал клинический маршрут. Для жюри это
выглядит как «система режет лишних пациентов».

Тесты ниже закрывают три вещи:
  1. конкретный баг с процентами (порог соблюдается);
  2. цитата в отказе по порогу дословна (врач может проверить);
  3. ЛЮБОЙ порог из конфига поддержан — параметризованный тест по
     всей матрице. Это и есть защита от повторения класса бага:
     добавите в JSON новый порог — тест сразу скажет, поддержан ли он.
"""

import json
from pathlib import Path

import pytest

from app.services.decision import DecisionEngine, load_triggers, validate
from app.services.decision.engine import TriggerMatch
from app.services.decision.matrix import TriggerDef
from app.services.decision.thresholds import (
    constraint_of,
    metric_of,
    numeric_thresholds,
    supports_threshold,
)
from app.services.extraction.base import Finding
from app.settings import get_settings
from scripts.import_clinical_matrix import LEGACY_PATH

STUDY = "УЗДГ артерий нижних конечностей"
STENOSIS = "Значимый стеноз артерий нижних конечностей"

# Настоящий кусок демо-протокола, который и породил баг.
PROTOCOL_07 = (
    "ЗАКЛЮЧЕНИЕ\n"
    "Заключение: ПБА с обеих сторон проходима, со стенозами до 30%, "
    "магистральный кровоток на всём протяжении. "
    "ГБА с обеих сторон проходима.\n"
)


def trigger_of(trigger_id: str) -> TriggerDef:
    for trigger in load_triggers(LEGACY_PATH):
        if trigger.trigger_id == trigger_id:
            return trigger
    raise AssertionError(f"триггер {trigger_id} исчез из матрицы")


def decide_stenosis(conclusion: str, quote: str = "стеноз", **kw) -> TriggerMatch:
    """Прогнать триггер стеноза по одному протоколу и вернуть его матч."""
    engine = DecisionEngine([trigger_of("lower_limb_stenosis")])
    finding = Finding(finding=STENOSIS, quote=quote, **kw)
    result = engine.decide([finding], study_type=STUDY, conclusion_text=conclusion)
    assert result.matches, "движок обязан вернуть матч по каждому триггеру"
    return result.matches[0]


# --------------------------------------------------------------------------
# 1. Исходный баг: процентный порог
# --------------------------------------------------------------------------


class TestPercentThreshold:
    def test_процентный_порог_проверяется(self):
        """30 % при пороге 70 % — маршрут создавать нельзя.

        Именно этот случай давал ложное срабатывание на demo_lower_limb_07.
        """
        match = decide_stenosis(PROTOCOL_07)
        assert match.fired is False
        assert match.suppression_reason == "threshold_not_met"
        assert "70" in match.detail and "30" in match.detail

    def test_процентный_порог_пройден(self):
        match = decide_stenosis("ЗАКЛЮЧЕНИЕ\nЗаключение: стеноз правой бедренной артерии до 85%.\n")
        assert match.fired is True
        assert match.suppression_reason is None

    def test_реальный_протокол_даёт_отказ_с_цитатой(self):
        """Тот самый протокол из демо-набора, а не собранный в тесте."""
        path = Path(get_settings().routing_matrix_path).parent.parent / "data/demo/protocols"
        target = path / "demo_lower_limb_07.txt"
        if not target.exists():
            pytest.skip("демо-протоколы не в поставке — проверка на тестовом тексте")
        text = target.read_text(encoding="utf-8")
        match = decide_stenosis(text, quote="стеноз")
        assert match.fired is False
        assert match.suppression_reason == "threshold_not_met"
        assert match.quote in text, "цитата обязана дословно встречаться в протоколе"

    def test_в_отказе_есть_дословная_цитата(self):
        """Не только число, а фрагмент исходного текста.

        Требование кейса: врач должен видеть, откуда взялось значение.
        """
        match = decide_stenosis(PROTOCOL_07)
        assert match.quote, "отказ по порогу без цитаты недопустим"
        assert match.quote in PROTOCOL_07, f"цитата не из текста: {match.quote!r}"
        assert "30" in match.quote, "в цитате должно быть само значение"

    def test_в_отказе_по_размеру_тоже_есть_цитата(self):
        """Проверяем не только новую ветку, но и старую — цитата обязана быть везде."""
        engine = DecisionEngine([trigger_of("ovarian_cyst")])
        text = "ЗАКЛЮЧЕНИЕ\nЗаключение: образование яичника 12 мм.\n"
        finding = Finding(finding="Образование яичника", quote="образование яичника 12 мм")
        match = engine.decide(
            [finding],
            study_type=trigger_of("ovarian_cyst").source_study,
            conclusion_text=text,
        ).matches[0]
        assert match.fired is False
        assert match.quote in text

    def test_окклюзия_без_процента_считается_стенозом_100(self):
        """Окклюзия — это 100 % по определению.

        Иначе настоящий пациент с окклюзией бедренной артерии отсеивается
        порогом 70 % только потому, что врач не написал процент.
        """
        match = decide_stenosis("ЗАКЛЮЧЕНИЕ\nЗаключение: Окклюзия левой ЗББА.\n", quote="окклюзия")
        assert match.fired is True

    def test_процента_нет_и_окклюзии_нет_порог_не_выполнен(self):
        """Нет значения — порог не подтверждён, но решение объяснено."""
        match = decide_stenosis(
            "ЗАКЛЮЧЕНИЕ\nЗаключение: начальные проявления стенозирующего атеросклероза.\n"
        )
        assert match.fired is False
        assert match.suppression_reason == "threshold_not_met"
        assert "не указано" in match.detail
        assert match.quote, "и при отсутствии значения объяснение должно быть с цитатой"


# --------------------------------------------------------------------------
# 2. Ни один порог из конфига не должен игнорироваться
# --------------------------------------------------------------------------


def _config_thresholds() -> list[tuple[str, str, float]]:
    """Все (trigger_id, key, value) из реального routing_matrix.json."""
    path = Path(get_settings().routing_matrix_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("triggers", [])
    out: list[tuple[str, str, float]] = []
    for item in items:
        for key, value in numeric_thresholds(item.get("thresholds") or {}):
            out.append((item["trigger_id"], key, value))
    return out


CONFIGURED_THRESHOLDS = _config_thresholds()


class TestEveryConfiguredThresholdIsSupported:
    """ЗАЩИТА ОТ ПОВТОРЕНИЯ БАГА.

    Ключ из routing_matrix.json — это обещание врачу: «сработает, когда
    значение ≥ N». Если движок ключ не знает, обещание молча нарушается,
    и это повторит исходный баг на следующем добавленном пороге.
    """

    def test_в_матрице_есть_пороги_для_проверки(self):
        assert CONFIGURED_THRESHOLDS, "матрица пуста — тест ничего не проверяет"

    @pytest.mark.parametrize(
        ("trigger_id", "key", "value"),
        CONFIGURED_THRESHOLDS,
        ids=[f"{t}-{k}" for t, k, _ in CONFIGURED_THRESHOLDS],
    )
    def test_каждый_порог_из_конфига_поддержан(self, trigger_id: str, key: str, value: float):
        assert supports_threshold(key), (
            f"{trigger_id}: порог «{key}» есть в матрице, но движок его не проверяет — "
            "триггер будет срабатывать вопреки порогу (см. баг lower_limb_stenosis)"
        )

    @pytest.mark.parametrize(
        ("trigger_id", "key", "value"),
        CONFIGURED_THRESHOLDS,
        ids=[f"{t}-{k}" for t, k, _ in CONFIGURED_THRESHOLDS],
    )
    def test_валидация_не_ругается_на_пороги_из_конфига(
        self, trigger_id: str, key: str, value: float
    ):
        """validate() обязана быть чистой по «родным» порогам."""
        warnings = [w for w in validate(load_triggers()) if key in w]
        assert not warnings, f"порт в матрице со своими же порогами: {warnings}"

    def test_известные_пороги_покрыты_резолверами(self):
        """Все ключи, которые реально встречаются в матрице, должны быть известны."""
        for _trigger_id, key, _value in CONFIGURED_THRESHOLDS:
            assert metric_of(key), f"{key}: не удалось определить метрику"
            assert constraint_of(key) in {"min", "max"}

    def test_числовые_пороги_читаются_из_конфига(self):
        """threshold_value() не должен молчать о ключе, который движок умеет."""
        for trigger_id, key, value in CONFIGURED_THRESHOLDS:
            trigger = trigger_of(trigger_id)
            assert trigger.threshold_value(key) == pytest.approx(value)


# --------------------------------------------------------------------------
# 3. Незнакомый порог не должен ронять движок
# --------------------------------------------------------------------------


class TestUnknownThreshold:
    """Заказчик вправе добавить порог раньше, чем мы напишем резолвер."""

    @staticmethod
    def _engine_with(thresholds: dict) -> DecisionEngine:
        base = trigger_of("lower_limb_stenosis")
        return DecisionEngine(
            [
                TriggerDef(
                    trigger_id=base.trigger_id,
                    display_name=base.display_name,
                    source_study=base.source_study,
                    synonyms=base.synonyms,
                    negative_contexts=base.negative_contexts,
                    thresholds=thresholds,
                    priority=base.priority,
                    emergency_flag=base.emergency_flag,
                    version=base.version,
                )
            ]
        )

    def test_неизвестный_порог_не_ломает(self):
        """Движок не падает и НЕ пропускает проверку молча."""
        engine = self._engine_with({"min_stenosis_percent": 70, "min_hounsfield_kat": 30})
        text = "ЗАКЛЮЧЕНИЕ\nЗаключение: стеноз до 30%.\n"
        result = engine.decide(
            [Finding(finding=STENOSIS, quote="стеноз")],
            study_type=STUDY,
            conclusion_text=text,
        )
        match = result.matches[0]
        assert match.fired is False, "непроверяемый порог обязан блокировать срабатывание"
        assert match.suppression_reason == "threshold_not_met"
        assert "min_hounsfield_kat" in match.detail, "врач должен видеть, какой порог не учтён"
        assert match.quote in text

    def test_порог_без_префикса_считается_минимумом(self):
        """Ключ без min_/max_ трактуем как «не меньше» — безопасное умолчание."""
        assert constraint_of("size_mm") == "min"

    def test_строковое_значение_порога_не_считается_числовым(self):
        """Нечисловые значения в thresholds игнорируются, а не ломают разбор."""
        assert numeric_thresholds({"note": "подобрать", "min_size_mm": 10}) == [
            ("min_size_mm", 10.0)
        ]

    def test_пустой_список_порогов_не_блокирует(self):
        """Триггер без порогов работает как раньше — по находке и отрицанию."""
        engine = self._engine_with({})
        result = engine.decide(
            [Finding(finding=STENOSIS, quote="стеноз")],
            study_type=STUDY,
            conclusion_text="ЗАКЛЮЧЕНИЕ\nЗаключение: выраженный стеноз.\n",
        )
        assert result.matches[0].fired is True

    def test_триггеры_без_порогов_не_ломаются(self):
        """Полип эндометрия порогов не имеет — он обязан работать как раньше."""
        engine = DecisionEngine([trigger_of("endometrial_polyp")])
        result = engine.decide(
            [Finding(finding="Полип эндометрия", quote="полип эндометрия")],
            study_type="УЗИ органов малого таза",
            conclusion_text="УЗ-признаки полипа эндометрия",
        )
        assert result.matches[0].fired is True

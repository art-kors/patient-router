"""Границы текстовых условий и защита от потери проверки порога."""

from dataclasses import replace

import pytest

from app.services.decision import DecisionEngine
from app.services.decision.matrix import TriggerDef
from app.services.decision.threshold_text import parse_threshold_text
from app.services.decision.thresholds import ThresholdOutcome
from app.services.extraction.base import Finding


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("диаметр БПВ > 5 мм", {"gt_size_mm": 5}),
        ("размер >= 5,5 мм", {"min_size_mm": 5.5}),
        ("стеноз < 30 %", {"lt_stenosis_percent": 30}),
        ("стеноз <= 30 %", {"max_stenosis_percent": 30}),
        ("наличие стенозов до 30%", {"max_stenosis_percent": 30}),
        ("стеноз до 30 %", {"max_stenosis_percent": 30}),
        ("стеноз не менее 70 %", {"min_stenosis_percent": 70}),
        ("стеноз ≥ 100 %", {"min_stenosis_percent": 100}),
        ("диаметр БПВ > 5-6 мм", {}),
        ("диаметр БПВ > 5 мм / наличие варикозной трансформации", {}),
        ("наличие окклюзии (100% стеноз) или любой значимый процент стеноза", {}),
        ("наличие миомы, кист шейки матки", {}),
        ("наличие признаков аденомиоза", {}),
        ("стеноз > 101 %", {}),
        ("размер > 30 %", {}),
    ],
)
def test_консервативный_детерминированный_разбор(text, expected):
    result = parse_threshold_text(text)
    assert result == parse_threshold_text(text)
    assert result.thresholds == expected
    assert bool(result.reason) == (not bool(expected))


def _trigger(text):
    """Однозначное условие для проверки общей логики движка."""
    return TriggerDef(
        trigger_id="test",
        display_name="стеноз",
        source_study="",
        synonyms=("стеноз",),
        negative_contexts=("стеноз не выявлен",),
        threshold_text=text,
    )


def _decide(trigger, value, negative=False):
    """Передать процент в протоколе через обычную находку декодера."""
    text = f"стеноз {value} %"
    finding = Finding(finding="стеноз", quote=text, in_negative_context=negative)
    return DecisionEngine([trigger]).decide([finding], conclusion_text=text).matches[0]


@pytest.mark.parametrize(
    ("condition", "value", "fired"),
    [
        ("стеноз > 30 %", 30, False),
        ("стеноз > 30 %", 45, True),
        ("стеноз до 30 %", 30, True),
        ("стеноз до 30 %", 45, False),
        ("стеноз < 30 %", 30, False),
        ("стеноз не менее 30 %", 30, True),
    ],
)
def test_границы_проверяются_общим_движком(condition, value, fired):
    result = _decide(_trigger(condition), value)
    assert result.fired is fired
    assert "30" in result.detail
    assert str(value) in result.detail
    if fired:
        assert "сработало" in result.detail


def test_отрицание_сильнее_текстового_порога():
    assert (
        _decide(_trigger("стеноз > 30 %"), 45, negative=True).suppression_reason
        == "negative_context"
    )


def test_явный_числовой_порог_не_переписывается():
    trigger = replace(_trigger("стеноз > 30 %"), thresholds={"min_stenosis_percent": 70})
    assert not _decide(trigger, 45).fired


def test_саботаж_проверки_возвращает_ложное_срабатывание(monkeypatch):
    """Удаление общей проверки меняет отказ на опасное срабатывание."""
    trigger = _trigger("стеноз до 30 %")
    assert not _decide(trigger, 45).fired
    monkeypatch.setattr(
        "app.services.decision.engine.evaluate_thresholds",
        lambda *args: ThresholdOutcome(passed=True),
    )
    assert _decide(trigger, 45).fired

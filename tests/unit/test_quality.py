"""Проверки метрик и честного отсутствия данных без БД."""

import json

import pytest

from app.services.decision.engine import TriggerMatch
from app.services.decision.matrix import TriggerDef
from app.services.quality import (
    LabeledSample,
    QualityDataError,
    QualityMetrics,
    compute_metrics,
    confusion,
    coverage,
    errors,
    load_labeled_samples,
)


def trigger(trigger_id="polyp"):
    return TriggerDef(trigger_id, "Полип", "УЗИ")


def sample(label=True, trigger_id="polyp", split="gold", study_id="study-1"):
    return LabeledSample(study_id, trigger_id, label, split)


def prediction(fired=True, trigger_id="polyp"):
    return TriggerMatch(
        trigger(trigger_id), fired, quote="Обнаружен полип", applied_rule=f"{trigger_id}@v1"
    )


def test_recall_и_precision_на_идеальных_данных():
    metrics = compute_metrics([prediction()], [sample()])
    assert metrics.recall == metrics.precision == metrics.f1 == 1.0
    assert metrics.n_samples == 1


def test_ложное_срабатывание_попадает_в_fp():
    assert confusion([prediction()], [sample(False)]) == {"tp": 0, "fp": 1, "fn": 0, "tn": 0}


def test_пропуск_находки_попадает_в_fn():
    assert compute_metrics([prediction(False)], [sample()]).fn == 1
    assert compute_metrics([], [sample()]).fn == 1


def test_fpr_на_нормах():
    samples = [sample(False, trigger_id=str(i)) for i in range(20)]
    predictions = [prediction(i == 0, str(i)) for i in range(20)]
    metrics = compute_metrics(predictions, samples)
    assert (metrics.fp, metrics.tn, metrics.n_samples) == (1, 19, 20)
    assert metrics.fpr == 0.05


def test_деление_на_ноль_даёт_ноль_а_не_исключение():
    metrics = QualityMetrics()
    assert metrics.recall == metrics.precision == metrics.fpr == metrics.f1 == 0.0
    assert metrics.n_samples == 0


def test_split_фильтрует_выборку():
    samples = [sample(), sample(False, split="synthetic")]
    assert compute_metrics([prediction()], samples, "gold").tp == 1
    assert compute_metrics([prediction()], samples, "gold").fp == 0
    assert compute_metrics([prediction()], samples, "synthetic").fp == 1
    assert compute_metrics([prediction()], samples).n_samples == 2
    assert errors([prediction()], samples, "gold") == []


def test_для_ошибок_есть_цитата():
    result = errors([prediction()], [sample(False)])[0]
    assert result["quote"] == "Обнаружен полип"
    assert result["applied_rule"] == "polyp@v1"
    assert result["study_id"] == "study-1"
    assert result["trigger_id"] == "polyp"
    assert result["type"] == "fp"
    assert errors([prediction()], [sample(False)], limit=0) == []
    assert errors([prediction(False)], [sample()])[0]["type"] == "fn"


def test_coverage_считает_покрытие():
    assert coverage([trigger(), trigger("other")], [sample(), sample(False)]) == {
        "total": 2,
        "covered": 1,
        "uncovered": ["other"],
        "ratio": 0.5,
    }
    assert coverage([], [sample()])["ratio"] == 0.0


def test_загрузчик_читает_все_файлы(tmp_path):
    for name, trigger_id in (("gold", "a"), ("synthetic", "b")):
        (tmp_path / f"{name}.json").write_text(
            json.dumps([dict(study_id="s", trigger_id=trigger_id, label=False, split=name)])
        )
    assert len(load_labeled_samples(tmp_path)) == 2


@pytest.mark.parametrize("content", [None, "[]", "{}", "broken", '[{"label": "false"}]'])
def test_невалидная_или_пустая_разметка(tmp_path, content):
    if content is not None:
        (tmp_path / "labels.json").write_text(content)
    with pytest.raises(QualityDataError):
        load_labeled_samples(tmp_path)


@pytest.mark.parametrize("endpoint", ["metrics", "confusion", "errors", "coverage"])
async def test_api_без_разметки_возвращает_503(client, monkeypatch, tmp_path, endpoint):
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "labeled_data_dir", str(tmp_path))
    response = await client.get(f"/api/v1/quality/{endpoint}")
    assert response.status_code == 503
    assert "Нет размеченных данных" in response.json()["detail"]


async def test_api_суммирует_исследования_без_смешивания(client, monkeypatch):
    from app.api import quality as api
    from app.main import app

    async def evaluate(samples, split):
        return [([prediction()], [sample()]), ([prediction(False)], [sample(False)])]

    app.dependency_overrides[api.labeled_samples] = lambda: [sample()]
    monkeypatch.setattr(api, "_evaluate", evaluate)
    try:
        response = await client.get("/api/v1/quality/metrics?split=gold")
        assert response.status_code == 200
        assert response.json() == dict(
            tp=1,
            fp=0,
            fn=0,
            tn=1,
            recall=1.0,
            precision=1.0,
            fpr=0.0,
            f1=1.0,
            n_samples=2,
            split="gold",
        )
    finally:
        app.dependency_overrides.pop(api.labeled_samples)

"""Проверки метрик и честного отсутствия данных без БД."""

import json
from uuid import uuid4

import pytest

from app.services.decision.engine import TriggerMatch
from app.services.decision.matrix import TriggerDef
from app.services.quality import (
    LabeledSample,
    QualityDataError,
    QualityMetrics,
    QualityScopeError,
    StudyPredictions,
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


def study(fired=True, trigger_id="polyp", study_id="study-1"):
    return StudyPredictions(study_id, (prediction(fired, trigger_id),))


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


@pytest.mark.parametrize("endpoint", ["metrics", "confusion", "errors"])
async def test_api_без_разметки_возвращает_503(client, monkeypatch, tmp_path, endpoint):
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "labeled_data_dir", str(tmp_path))
    response = await client.get(f"/api/v1/quality/{endpoint}")
    assert response.status_code == 503
    assert "Нет размеченных данных" in response.json()["detail"]


# ── Регрессия: README обещал рабочий /coverage, а ручка отдавала 503 ─────────
# `data/labeled/` под .gitignore: разметка хакатона не публикуется, и в свежем
# клоне её нет. 503 там законен, но README вводил жюри в заблуждение. Теперь
# ручка отвечает всегда: общее число триггеров считается по матрице, а покрытие
# без разметки честно помечается как недоступное (null, а не ложный ноль).


async def test_coverage_без_разметки_отвечает_честно(client, monkeypatch, tmp_path):
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "labeled_data_dir", str(tmp_path))
    response = await client.get("/api/v1/quality/coverage")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["total"] > 0, "матрица всегда на месте — её размер известен без разметки"
    assert body["covered"] is None and body["ratio"] is None, "ложный ноль читался бы как оценка"
    assert body["labeled_samples"] == 0
    assert "Нет данных для оценки покрытия" in body["message"]


async def test_coverage_с_разметкой_считает_покрытие(client, monkeypatch, tmp_path):
    import json

    from app.api import quality as api
    from app.main import app
    from app.settings import get_settings

    triggers = api.load_triggers()
    (tmp_path / "gold.json").write_text(
        json.dumps(
            [
                {
                    "study_id": str(uuid4()),
                    "trigger_id": triggers[0].trigger_id,
                    "label": True,
                    "split": "gold",
                }
            ]
        )
    )
    monkeypatch.setattr(get_settings(), "labeled_data_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "study_index_path", str(tmp_path / "нет.json"))
    app.dependency_overrides[api.optional_samples] = lambda: api.labeled_samples()
    try:
        response = await client.get("/api/v1/quality/coverage")
    finally:
        app.dependency_overrides.pop(api.optional_samples)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["covered"] == 1
    assert body["ratio"] == 1 / len({t.trigger_id for t in triggers})
    assert body["labeled_samples"] == 1
    assert body["message"] is None


async def test_api_суммирует_исследования_без_смешивания(client, monkeypatch):
    from app.api import quality as api
    from app.main import app

    async def evaluate(samples, split):
        return [
            (study(True, study_id="study-1"), [sample(True, study_id="study-1")]),
            (study(False, study_id="study-2"), [sample(False, study_id="study-2")]),
        ]

    app.dependency_overrides[api.labeled_samples] = lambda: [sample(study_id="study-1")]
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


async def test_api_ошибка_области_даёт_503_а_не_мусорные_нули(client, monkeypatch):
    """Предсказания не того исследования не превращаются в «ничего не нашло»."""
    from app.api import quality as api
    from app.main import app

    async def evaluate(samples, split):
        # study-1 размечен, а движок прогоняли по study-2 — тихая подмена.
        return [(study(True, study_id="study-2"), [sample(True, study_id="study-1")])]

    app.dependency_overrides[api.labeled_samples] = lambda: [sample(study_id="study-1")]
    monkeypatch.setattr(api, "_evaluate", evaluate)
    try:
        for endpoint in ("metrics", "confusion", "errors"):
            response = await client.get(f"/api/v1/quality/{endpoint}")
            assert response.status_code == 503
            assert "study-2" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(api.labeled_samples)


def test_сопоставление_по_паре_исследование_и_триггер():
    """Один и тот же триггер у разных исследований не склеивается."""
    assert confusion(study(True, study_id="s1"), [sample(True, study_id="s1")])["tp"] == 1
    assert confusion(study(False, study_id="s1"), [sample(True, study_id="s1")])["fn"] == 1
    assert confusion(study(True, study_id="s1"), [sample(False, study_id="s1")])["fp"] == 1


def test_предсказания_разных_исследований_не_смешиваются():
    """Предсказания двух исследований в одной оценке — явная ошибка, не нули."""
    samples = [sample(True, study_id="s1"), sample(True, study_id="s2")]
    with pytest.raises(QualityScopeError) as both:
        compute_metrics([prediction()], samples)
    assert "s1" in str(both.value) and "s2" in str(both.value)

    with pytest.raises(QualityScopeError) as swapped:
        confusion(study(True, study_id="s1"), [sample(True, study_id="s2")])
    assert "Смешивать их нельзя" in str(swapped.value)


def test_разные_исследования_считаются_по_одному_и_складываются():
    """Правильный способ: группа на исследование, затем сумма матриц."""
    per_study = [
        confusion(study(True, study_id="s1"), [sample(True, study_id="s1")]),
        confusion(study(False, study_id="s2"), [sample(True, study_id="s2")]),
    ]
    total = QualityMetrics(
        **{key: sum(part[key] for part in per_study) for key in ("tp", "fp", "fn", "tn")}
    )
    assert (total.tp, total.fn, total.n_samples) == (1, 1, 2)
    assert total.recall == 0.5


def test_два_предсказания_по_одному_триггеру_дают_ошибку():
    doubled = StudyPredictions("s1", (prediction(True), prediction(False)))
    with pytest.raises(QualityScopeError, match="Два предсказания"):
        confusion(doubled, [sample(True, study_id="s1")])


def test_пустые_предсказания_дают_нули_а_не_ошибку():
    assert compute_metrics([], []) == QualityMetrics()
    assert confusion([], []) == {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    assert errors([], []) == []
    # Ничего не сработало — это честные FN, а не ошибка.
    assert compute_metrics([], [sample(True)]) == QualityMetrics(fn=1)


def test_предсказания_без_разметки_требуют_исследование():
    with pytest.raises(QualityScopeError, match="исследование"):
        compute_metrics([prediction()], [])
    # С явным study_id предсказания без разметки считаются как «ни одного образца».
    assert compute_metrics(study(), []) == QualityMetrics()
    with pytest.raises(ValueError, match="study_id"):
        StudyPredictions("  ", ())


def test_повторная_разметка_отвергается_загрузчиком(tmp_path):
    """Дубль (исследование, триггер) ловится на входе, а не в метрике."""
    (tmp_path / "gold.json").write_text(
        json.dumps([dict(study_id="s1", trigger_id="polyp", label=True, split="gold")])
    )
    (tmp_path / "dup.json").write_text(
        json.dumps([dict(study_id="s1", trigger_id="polyp", label=False, split="gold")])
    )
    with pytest.raises(QualityDataError, match="повторная разметка"):
        load_labeled_samples(tmp_path)


def test_split_сужает_оценку_до_одного_исследования():
    """Фильтр по split применяется до проверки области."""
    samples = [sample(True, study_id="s1"), sample(True, study_id="s2", split="synthetic")]
    assert confusion(study(True, study_id="s1"), samples, "gold")["tp"] == 1

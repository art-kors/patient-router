"""Замкнутый цикл с настоящим SQL и независимыми врачебными метками."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api import analysis, analytics
from app.clock import get_clock
from app.db import get_session
from app.models import AnalysisFeedback, AnalysisRun, Study
from app.services.analytics import aggregate
from app.services.decision.matrix import TriggerDef
from app.services.extraction import DictionaryExtractor, get_extractor, set_extractor
from app.services.quality import LabeledSample


@pytest.fixture
async def cycle(analytics_session, monkeypatch, model_clock):
    """Изолировать правила в памяти, не изменяя занятую матрицу проекта."""
    trigger = TriggerDef(
        trigger_id="test_polyp",
        display_name="Полип",
        source_study="УЗИ тест",
        synonyms=("полип",),
        thresholds={"min_size_mm": 10},
    )
    rules = [trigger]
    monkeypatch.setattr("app.services.decision.engine.load_triggers", lambda: rules)
    monkeypatch.setattr(analytics, "optional_samples", lambda: [])
    original = get_extractor()
    extractor = DictionaryExtractor()
    extractor._synonyms = [("полип", "Полип")]
    extractor._negatives = {"Полип": []}
    set_extractor(extractor)
    app = FastAPI()
    app.include_router(analysis.router)
    app.include_router(analytics.router)
    app.dependency_overrides[get_session] = lambda: analytics_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, analytics_session, rules
    finally:
        set_extractor(original)


async def parse(client, text, key=None):
    """Разобрать протокол через публичный API."""
    response = await client.post(
        "/api/v1/analyze",
        json={"text": text, "study_type": "УЗИ тест"},
        headers={"Idempotency-Key": key} if key else {},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def mark(client, identifier, label):
    """Сохранить независимую оценку врача."""
    response = await client.put(
        f"/api/v1/analytics/analyses/{identifier}/feedback/test_polyp", json={"label": label}
    )
    assert response.status_code == 200, response.text


async def test_полный_цикл_и_защита_от_игнорирования_врача(cycle, model_clock):
    """Без отметок врача проверка recall/precision после разметки обязана упасть."""
    client, session, rules = cycle
    protocols = [
        "Заключение: полип 12 мм.",
        "Заключение: полип 11 мм.",
        "Заключение: полип 8 мм.",
        "Заключение: полип 9 мм.",
    ]
    responses = [await parse(client, text) for text in protocols]
    before = (await client.get("/api/v1/analytics/metrics")).json()
    assert before["analyses"] == 4
    assert before["metrics"]["recall"] is None
    assert before["metrics"]["precision"] is None
    for response, label in zip(
        responses, ["confirmed", "false_positive", "missed", "missed"], strict=True
    ):
        await mark(client, response["analysis_id"], label)
    reviewed = (await client.get("/api/v1/analytics/metrics")).json()
    assert reviewed["metrics"]["recall"] == pytest.approx(1 / 3)
    assert reviewed["metrics"]["precision"] == 0.5
    assert reviewed["rejected_fraction"] == 0.5
    row = reviewed["by_trigger"][0]
    assert (row["confirmed"], row["rejected"], row["missed"]) == (1, 1, 2)
    assert row["occurrences"] == 4 and row["fired"] == 2
    # Тест чувствителен к саботажу: та же выборка без врача даёт другие результаты.
    runs = (await session.scalars(select(AnalysisRun))).all()
    sabotaged = aggregate(runs, [])
    assert sabotaged["metrics"] != reviewed["metrics"]
    rules[:] = [replace(rules[0], thresholds={"min_size_mm": 8}, version=2)]
    current = (await client.get("/api/v1/analytics/metrics?current=true")).json()
    assert current["metrics"]["recall"] == 1.0
    assert current["metrics"]["precision"] == 0.75
    assert current["by_study"][0]["recall"] == 1.0
    assert current["timeline"][0]["recall"] == 1.0
    historical = (await client.get("/api/v1/analytics/metrics")).json()
    assert historical["metrics"] == reviewed["metrics"]
    model_clock.advance(hours=24)
    new_responses = [await parse(client, text) for text in protocols]
    for response, label in zip(
        new_responses, ["confirmed", "false_positive", "confirmed", "confirmed"], strict=True
    ):
        await mark(client, response["analysis_id"], label)
    trend = (await client.get("/api/v1/analytics/metrics")).json()["timeline"]
    assert [day["recall"] for day in trend] == [pytest.approx(1 / 3), 1.0]
    assert [day["precision"] for day in trend] == [0.5, 0.75]
    print(
        "ЦИКЛ: N=4, до врача recall/precision=null; врач: TP=1 FP=1 FN=2, "
        "recall=0.333 precision=0.500; порог 10→8 мм: TP=3 FP=1 FN=0, "
        "recall=1.000 precision=0.750; отклонено 50% проверенных находок"
    )


async def test_идемпотентность_журнала_отметок_и_изменение_оценки(cycle):
    client, session, _ = cycle
    first = await parse(client, "Заключение: полип 12 мм.", "one-request")
    second = await parse(client, "Заключение: полип 12 мм.", "one-request")
    assert first["analysis_id"] == second["analysis_id"]
    assert await session.scalar(select(func.count()).select_from(AnalysisRun)) == 1
    for _ in range(2):
        await mark(client, first["analysis_id"], "false_positive")
    assert await session.scalar(select(func.count()).select_from(AnalysisFeedback)) == 1
    assert (await client.get("/api/v1/analytics/metrics")).json()["metrics"]["precision"] == 0
    await mark(client, first["analysis_id"], "confirmed")
    report = (await client.get("/api/v1/analytics/metrics")).json()
    assert report["metrics"]["precision"] == 1
    assert report["reviewed"] == 1
    bad = await client.post(
        "/api/v1/analyze", json={"text": "Другой текст"}, headers={"Idempotency-Key": "one-request"}
    )
    assert bad.status_code == 409


async def test_врач_важнее_сгенерированной_разметки(cycle, monkeypatch):
    client, session, _ = cycle
    text = "Заключение: полип 12 мм."
    response = await parse(client, text)
    identifier = uuid4()
    study = Study(
        id=identifier,
        patient_id=uuid4(),
        study_type="УЗИ тест",
        study_date=get_clock().now().date(),
        raw_text=text,
        created_at=get_clock().now(),
    )
    session.sync.add(study)
    await session.commit()
    monkeypatch.setattr(
        analytics,
        "optional_samples",
        lambda: [LabeledSample(str(identifier), "test_polyp", True, "synthetic")],
    )
    before = (await client.get("/api/v1/analytics/metrics")).json()
    assert before["generated_labels"] == 1
    assert before["metrics"]["precision"] == 1
    await mark(client, response["analysis_id"], "false_positive")
    after = (await client.get("/api/v1/analytics/metrics")).json()
    assert after["metrics"]["precision"] == 0
    assert after["metrics"]["tp"] == 0 and after["metrics"]["fp"] == 1


async def test_обезличивание_даже_произвольных_полей(cycle):
    client, session, _ = cycle
    response = await parse(client, "Иванов Иван, телефон 79991234567. Заключение: полип 12 мм.")
    run = await session.get(AnalysisRun, UUID(response["analysis_id"]))
    content = str(run.findings) + str(run.matches)
    assert "Иванов" not in content and "79991234567" not in content
    assert "quote" not in content and "detail" not in content
    assert len(run.text_hash) == 64 and float(run.duration_ms) > 0
    assert run.decoder_used == "rules"
    listed = (await client.get("/api/v1/analytics/analyses")).json()
    assert listed[0]["decoder_used"] == "rules"
    assert "Иванов" not in str(listed)


async def test_валидация_пары_и_отсутствующих_данных(cycle):
    client, _, _ = cycle
    empty = (await client.get("/api/v1/analytics/metrics")).json()
    assert empty["metrics"]["recall"] is None and empty["rejected_fraction"] is None
    response = await parse(client, "Заключение: полип 8 мм.")
    path = f"/api/v1/analytics/analyses/{response['analysis_id']}/feedback"
    assert (await client.put(path + "/test_polyp", json={"label": "confirmed"})).status_code == 422
    assert (await client.put(path + "/unknown", json={"label": "missed"})).status_code == 422
    assert (
        await client.put(
            f"/api/v1/analytics/analyses/{uuid4()}/feedback/test_polyp", json={"label": "missed"}
        )
    ).status_code == 404


async def test_загрузка_также_пишет_журнал(cycle):
    client, session, _ = cycle
    response = await client.post(
        "/api/v1/analyze/upload",
        files={"file": ("protocol.txt", "Заключение: полип 12 мм.".encode(), "text/plain")},
        data={"study_type": "УЗИ тест"},
    )
    assert response.status_code == 200
    assert await session.get(AnalysisRun, UUID(response.json()["analysis_id"])) is not None


async def test_неизменённые_правила_не_теряют_текстовые_признаки(cycle):
    """Переоценка неизменённого правила сохраняет исходную истину."""
    client, _, rules = cycle
    response = await parse(client, "Заключение: полип 12 мм.")
    await mark(client, response["analysis_id"], "confirmed")
    before = (await client.get("/api/v1/analytics/metrics")).json()
    same = (await client.get("/api/v1/analytics/metrics?current=true")).json()
    assert same["metrics"] == before["metrics"]
    assert same["reanalysis_required_pairs"] == 0
    rules[:] = [replace(rules[0], synonyms=("другая находка",), version=2)]
    changed = (await client.get("/api/v1/analytics/metrics?current=true")).json()
    assert changed["reanalysis_required_pairs"] == 1
    assert changed["metrics"] == before["metrics"]


async def test_синтетика_учитывается_без_записанного_разбора(cycle, monkeypatch):
    client, session, _ = cycle
    identifier = uuid4()
    session.sync.add(
        Study(
            id=identifier,
            patient_id=uuid4(),
            study_type="УЗИ тест",
            study_date=get_clock().now().date(),
            raw_text="Заключение: полип 12 мм.",
            created_at=get_clock().now(),
        )
    )
    await session.commit()
    monkeypatch.setattr(
        analytics,
        "optional_samples",
        lambda: [LabeledSample(str(identifier), "test_polyp", True, "synthetic")],
    )
    report = (await client.get("/api/v1/analytics/metrics")).json()
    assert report["analyses"] == 0 and report["generated_studies"] == 1
    assert report["metrics"]["precision"] == 1
    assert report["doctor_metrics"]["precision"] is None
    assert report["timeline"] == []

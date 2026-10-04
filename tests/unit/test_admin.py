"""Проверки API дашборда в изолированном приложении без внешней БД."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import admin, quality, ui
from app.services.admin import AdminService
from app.services.decision.engine import DecisionEngine
from app.services.decision.matrix import MATRIX_PATH, _build, load_triggers, validate
from app.services.decision.matrix_store import versions
from app.services.extraction import get_extractor, set_extractor
from app.services.quality import LabeledSample, StudyPredictions
from tests.unit.test_matrix_store import MemorySession


@pytest.fixture
async def dashboard(monkeypatch):
    session = MemorySession()
    original = get_extractor()
    from app.services.extraction import DictionaryExtractor

    set_extractor(DictionaryExtractor())

    def reload():
        from app.services.decision.engine import refresh_dictionary

        row = session.sync.execute(
            select(versions.c.triggers).order_by(versions.c.version.desc())
        ).scalar()
        triggers = [_build(item) for item in row if item.get("enabled", True)]
        refresh_dictionary(triggers)
        return triggers

    monkeypatch.setattr("app.services.admin.reload_engine", reload)
    monkeypatch.setattr(admin, "reload_engine", reload)
    app = FastAPI()
    app.include_router(admin.router)
    app.include_router(quality.router)
    app.include_router(ui.router)
    app.dependency_overrides[admin.service] = lambda: AdminService(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, app, session
    set_extractor(original)
    session.close()


async def test_редактирование_отключение_и_откат(dashboard):
    client, _, _ = dashboard
    rows = (await client.get("/api/v1/admin/triggers")).json()
    assert len(rows) == len(load_triggers(MATRIX_PATH))
    identifier = rows[0]["trigger_id"]
    path = f"/api/v1/admin/triggers/{identifier}"
    assert (await client.get(path)).json()["synonyms"]
    response = await client.put(path, json={"target_sla_days": 2, "author": "Врач"})
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert (await client.get(path)).json()["target_sla_days"] == 2
    assert (await client.delete(path)).status_code == 200
    assert (await client.get(path)).json()["enabled"] is False
    history = (await client.get("/api/v1/admin/versions")).json()
    assert [row["version"] for row in history] == [3, 2, 1]
    response = await client.post("/api/v1/admin/versions/1/rollback", json={"author": "Врач"})
    assert response.json()["version"] == 4
    restored = (await client.get(path)).json()
    assert restored["enabled"] is True
    assert restored["target_sla_days"] == rows[0]["target_sla_days"]


async def test_новый_триггер_сразу_распознаётся(dashboard):
    client, _, session = dashboard
    body = {
        "trigger_id": "demo",
        "display_name": "Новая находка",
        "synonyms": ["демонаходка"],
        "negative_contexts": ["демонаходки нет"],
        "priority": 3,
    }
    response = await client.post("/api/v1/admin/triggers", json=body)
    assert response.status_code == 201
    extraction = get_extractor().extract("Заключение: Демонаходка.")
    items = session.sync.execute(
        select(versions.c.triggers).order_by(versions.c.version.desc())
    ).scalar()
    decision = DecisionEngine([_build(item) for item in items]).decide(
        extraction.findings, conclusion_text=extraction.conclusion_text
    )
    assert decision.winner.trigger.trigger_id == "demo"
    assert decision.winner.quote == "Демонаходка"
    assert (await client.post("/api/v1/admin/triggers", json=body)).status_code == 409
    saved = (await client.get("/api/v1/admin/triggers/demo")).json()
    assert saved["enabled"] is True
    assert saved["target_sla_days"] == 14


@pytest.mark.parametrize(
    "patch",
    [
        {"priority": 0},
        {"synonyms": []},
        {"thresholds": {"unknown": 2}},
        {"emergency_flag": True, "priority": 2},
        {"enabled": None},
    ],
)
async def test_валидация_422_без_новой_версии(dashboard, patch):
    client, _, _ = dashboard
    response = await client.put(
        "/api/v1/admin/triggers/podozrenie_na_polip_endometriya_i_polip_sheyki_matki_pokazanie_k_operatsii",
        json=patch,
    )
    assert response.status_code == 422
    assert response.json()["detail"]
    assert (await client.get("/api/v1/admin/versions")).json() == []


async def test_404_и_предупреждения(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/api/v1/admin/triggers/missing")).status_code == 404
    assert (await client.put("/api/v1/admin/triggers/missing", json={})).status_code == 404
    assert (await client.post("/api/v1/admin/versions/999/rollback", json={})).status_code == 404
    assert (await client.get("/api/v1/admin/validate")).json()["warnings"] == validate(
        load_triggers(MATRIX_PATH)
    )


async def test_статика_и_защита_пути(dashboard):
    client, _, _ = dashboard
    for path in ("/", "/static/style.css", "/static/dashboard.js"):
        assert (await client.get(path)).status_code == 200
    assert (await client.get("/static/models.py")).status_code == 404


async def test_метрики_по_триггерам_и_фильтр_до_лимита(dashboard, monkeypatch):
    client, app, _ = dashboard
    triggers = load_triggers(MATRIX_PATH)
    labels = [LabeledSample("s1", trigger.trigger_id, True, "gold") for trigger in triggers[:2]]
    predictions = StudyPredictions("s1", tuple(DecisionEngine(triggers).decide([]).matches))
    app.dependency_overrides[quality.labeled_samples] = lambda: labels

    async def evaluate(samples, split):
        return [(predictions, labels)]

    monkeypatch.setattr(quality, "_evaluate", evaluate)
    monkeypatch.setattr(quality, "load_triggers", lambda: triggers)
    monkeypatch.setattr(quality, "_quality_triggers", lambda: triggers)
    rows = (await client.get("/api/v1/quality/metrics/by-trigger")).json()
    assert len(rows) == len(load_triggers(MATRIX_PATH))
    assert rows[0]["fn"] == 1 and rows[0]["recall"] == 0
    assert rows[2]["available"] is False
    response = await client.get(
        f"/api/v1/quality/errors?trigger_id={triggers[1].trigger_id}&limit=1"
    )
    assert len(response.json()) == 1
    assert response.json()[0]["trigger_id"] == triggers[1].trigger_id


async def test_снимки_метрик_сохраняются_с_версией_и_декодером(dashboard, monkeypatch):
    client, app, session = dashboard
    from app import db

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(db, "SessionFactory", Context)
    app.dependency_overrides[quality.labeled_samples] = lambda: []

    async def metrics(samples, split):
        return quality.MetricsOut(
            tp=1, fp=0, fn=0, tn=1, recall=1, precision=1, fpr=0, f1=1, n_samples=2, split=split
        )

    monkeypatch.setattr(quality, "metrics", metrics)
    first = await client.get("/api/v1/quality/metrics/timeline?split=gold")
    assert first.status_code == 200
    assert len(first.json()["points"]) == 1
    second = (await client.get("/api/v1/quality/metrics/timeline?split=gold")).json()
    assert len(second["points"]) == 2
    assert second["points"][0]["matrix_version"] == 1
    assert second["points"][0]["decoder"].startswith("DictionaryExtractor")
    assert second["points"][0]["metrics"]["recall"] == 1


async def test_отключённое_правило_считается_FN_а_не_ошибкой_разметки(dashboard, monkeypatch):
    """Старая разметка остаётся пригодной после мягкого отключения."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app import db

    client, app, session = dashboard
    identifier = "podozrenie_na_polip_endometriya_i_polip_sheyki_matki_pokazanie_k_operatsii"
    await client.delete(f"/api/v1/admin/triggers/{identifier}")
    stored = session.sync.execute(
        select(versions.c.triggers).order_by(versions.c.version.desc())
    ).scalar()
    active = [_build(item) for item in stored if item.get("enabled", True)]
    complete = [_build(item) for item in stored]
    monkeypatch.setattr(quality, "_quality_triggers", lambda: complete)
    monkeypatch.setattr(quality, "load_triggers", lambda: active)
    monkeypatch.setattr(quality, "DecisionEngine", lambda: DecisionEngine(active))
    study_id = uuid4()
    study = SimpleNamespace(
        id=study_id, raw_text="Заключение: полип эндометрия", study_type="УЗИ органов малого таза"
    )

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: [study])

    monkeypatch.setattr(db, "SessionFactory", Context)
    app.dependency_overrides[quality.labeled_samples] = lambda: [
        LabeledSample(str(study_id), identifier, True, "gold")
    ]
    response = await client.get("/api/v1/quality/metrics")
    assert response.status_code == 200
    assert response.json()["fn"] == 1
    rows = (await client.get("/api/v1/quality/metrics/by-trigger")).json()
    disabled = next(row for row in rows if row["trigger_id"] == identifier)
    assert disabled["enabled"] is False and disabled["fn"] == 1
    response = await client.post("/api/v1/admin/reload")
    assert response.status_code == 200
    assert response.json()["triggers"] == len(active)

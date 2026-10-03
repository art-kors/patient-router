"""Контракт API маршрутов без подключения к реальной БД."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.routes import router
from app.db import get_session
from app.main import create_app


@pytest.fixture
def routes_app():
    """Изолированное приложение с подменённой сессией."""
    app = create_app()
    session = AsyncMock()
    session.get.return_value = None

    async def fake_session():
        """Выдаёт сессию, которая не обращается к PostgreSQL."""
        yield session

    app.dependency_overrides[get_session] = fake_session
    return app


def test_маршруты_в_openapi(routes_app):
    paths = routes_app.openapi()["paths"]
    assert {"get", "post"} <= paths["/api/v1/routes"].keys()
    assert "/api/v1/routes/{route_id}" in paths
    assert "/api/v1/routes/{route_id}/tactics" in paths
    assert "/api/v1/routes/{route_id}/transition" in paths


def test_unfinished_объявлен_до_route_id(routes_app):
    paths = list(routes_app.openapi()["paths"])
    assert paths.index("/api/v1/routes/unfinished") < paths.index("/api/v1/routes/{route_id}")
    routes = [route.path for route in router.routes]
    assert routes.index("/api/v1/routes/unfinished") < routes.index("/api/v1/routes/{route_id}")


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [{}, {"tactics": ""}, {"tactics": None}])
async def test_невалидная_тактика_даёт_422(routes_app, payload):
    async with AsyncClient(
        transport=ASGITransport(app=routes_app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/v1/routes/{uuid4()}/tactics", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TACTICS_REQUIRED"


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["", "/timeline"])
async def test_неизвестный_маршрут_даёт_404(routes_app, suffix):
    async with AsyncClient(
        transport=ASGITransport(app=routes_app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/routes/{uuid4()}{suffix}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ROUTE_NOT_FOUND"


def test_схема_ответа_содержит_целевую_дату(routes_app):
    schema = routes_app.openapi()["components"]["schemas"]["RouteOut"]
    assert schema["properties"]["target_date"]["description"]
    assert {"type": "string", "format": "date"} in schema["properties"]["target_date"]["anyOf"]


def test_таймлайн_в_схеме(routes_app):
    schema = routes_app.openapi()
    response = schema["paths"]["/api/v1/routes/{route_id}/timeline"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/TimelineOut")
    properties = schema["components"]["schemas"]["TimelineOut"]["properties"]
    assert {"steps", "audit_logs", "route_id"} <= properties.keys()


@pytest.mark.anyio
async def test_неизвестное_исследование_даёт_404(routes_app):
    """Создание маршрута проверяет ссылки до обращения к сервисам."""
    async with AsyncClient(
        transport=ASGITransport(app=routes_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/routes",
            json={
                "patient_external_id": "нет-пациента",
                "protocol_id": str(uuid4()),
                "trigger_match_id": str(uuid4()),
            },
        )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "STUDY_NOT_FOUND"


@pytest.mark.anyio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_дубликат_маршрута_даёт_409(routes_app, monkeypatch, concurrent):
    """Проверяет существующий дубль и конфликт уникальности при гонке."""
    import sys
    from types import ModuleType, SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from app.models import Patient, Protocol, Study, TriggerDef, TriggerMatch

    patient_id, study_id, protocol_id, match_id = (uuid4() for _ in range(4))
    objects = {
        Patient: SimpleNamespace(id=patient_id),
        Protocol: SimpleNamespace(id=protocol_id, study_id=study_id),
        Study: SimpleNamespace(id=study_id, patient_id=patient_id),
        TriggerMatch: SimpleNamespace(id=match_id, protocol_id=protocol_id, trigger_def_id=1),
        TriggerDef: SimpleNamespace(specialty_id=1, target_sla_days=14),
    }
    session = AsyncMock()
    session.get.side_effect = lambda model, key: objects[model]
    session.scalar.side_effect = [objects[Patient], None if concurrent else uuid4()]

    async def fake_session():
        """Выдаёт связанные объекты без обращения к базе."""
        yield session

    routes_app.dependency_overrides[get_session] = fake_session
    routing = ModuleType("app.services.routing")
    service = SimpleNamespace(
        create_from_match=AsyncMock(
            side_effect=IntegrityError("вставка", {}, Exception("uq_route_trigger_match"))
        )
    )
    routing.RoutingService = lambda: service
    timers = ModuleType("app.services.timers")
    timers.TimerEngine = lambda: SimpleNamespace()
    monkeypatch.setitem(sys.modules, "app.services.routing", routing)
    monkeypatch.setitem(sys.modules, "app.services.timers", timers)
    async with AsyncClient(
        transport=ASGITransport(app=routes_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/routes",
            json={
                "patient_external_id": "пациент",
                "protocol_id": str(protocol_id),
                "trigger_match_id": str(match_id),
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ROUTE_ALREADY_EXISTS"
    if concurrent:
        session.rollback.assert_awaited_once()

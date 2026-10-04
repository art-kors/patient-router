"""HTTP-контракт отдельного роутера демо-МИС без настоящей БД."""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.mock_mis import get_mock_mis, router
from app.db import get_session
from app.services.mis import MisEventConflictError

pytestmark = pytest.mark.anyio
PREFIX = "/api/v1/mock/mis"


@pytest.fixture
async def mock_client():
    """Подключить роутер только в тестовом приложении."""
    app = FastAPI()
    app.include_router(router)
    service = Mock()
    service.catalog.return_value = []
    service.emit = AsyncMock(return_value={"route_created": False, "actions": []})
    service.queue = AsyncMock(return_value={"ready_count": 0})
    session = Mock(rollback=AsyncMock())
    app.dependency_overrides[get_mock_mis] = lambda: service
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, service, session


async def test_catalog_and_queue(mock_client):
    client, service, _ = mock_client
    assert (await client.get(PREFIX + "/patients")).json() == []
    assert (await client.get(PREFIX + "/queue?limit=5")).json() == {"ready_count": 0}
    assert service.queue.call_args.args[1] == 5
    assert (await client.get(PREFIX + "/queue?limit=0")).status_code == 422


async def test_emit_and_chain(mock_client):
    client, service, _ = mock_client
    response = await client.post(PREFIX + "/emit", json={"study_id": "demo_1"})
    assert response.status_code == 200
    assert response.json()["route_created"] is False
    assert service.emit.call_args.args[1] == "StudyProtocolSigned"
    response = await client.post(PREFIX + "/emit/VisitCompleted", json={"study_id": "demo_1"})
    assert response.status_code == 200
    assert service.emit.call_args.args[1] == "VisitCompleted"


@pytest.mark.parametrize(
    "body", [{}, {"study_id": ""}, {"study_id": "demo_1", "occurred_at": "2026-09-01T00:00:00"}]
)
async def test_validation(mock_client, body):
    client, service, _ = mock_client
    assert (await client.post(PREFIX + "/emit", json=body)).status_code == 422
    service.emit.assert_not_awaited()


@pytest.mark.parametrize(
    "error,status",
    [
        (LookupError("Нет исследования"), 404),
        (ValueError("Неизвестный тип"), 422),
        (FileNotFoundError("Нет протоколов"), 503),
        (MisEventConflictError("Повторный ID"), 409),
    ],
)
async def test_delivery_errors_roll_back(mock_client, error, status):
    client, service, session = mock_client
    service.emit.side_effect = error
    response = await client.post(PREFIX + "/emit", json={"study_id": "demo_1"})
    assert response.status_code == status
    session.rollback.assert_awaited_once()


async def test_missing_data_returns_503(mock_client):
    client, service, _ = mock_client
    service.catalog.side_effect = FileNotFoundError("Нет протоколов")
    assert (await client.get(PREFIX + "/patients")).status_code == 503


async def test_openapi_contains_all_routes(mock_client):
    client, _, _ = mock_client
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert set(paths) == {
        PREFIX + path for path in ("/patients", "/queue", "/emit", "/emit/{event_type}")
    }

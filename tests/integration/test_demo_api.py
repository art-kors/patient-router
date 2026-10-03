"""Проверки защиты: прокрутка времени и объяснимые сценарии без БД."""

from datetime import timedelta
from uuid import uuid4

import pytest

import app.clock
from app.api.demo import get_timer_engine
from app.clock import ModelClock, SystemClock
from app.services.timers import Effect

pytestmark = pytest.mark.anyio
PREFIX = "/api/v1/demo"


@pytest.fixture
def clock(monkeypatch):
    """Каждый показ начинается с чистых часов и отдельной очереди."""
    value = ModelClock()
    monkeypatch.setattr(app.clock, "get_clock", lambda: value)
    return value


async def test_эндпоинты_в_openapi(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    for path, method in [
        ("/clock", "get"),
        ("/clock/advance", "post"),
        ("/clock/set", "post"),
        ("/clock/reset", "post"),
        ("/timers", "get"),
        ("/scenarios", "get"),
    ]:
        assert method in paths[PREFIX + path]


async def test_clock_возвращает_время(client, clock):
    response = await client.get(PREFIX + "/clock")
    assert response.status_code == 200
    assert response.json() == {
        "now": clock.now().isoformat().replace("+00:00", "Z"),
        "is_mock": True,
        "source": "model",
    }


async def test_сценарии_список_из_7_штук_и_у_каждого_есть_описание(client):
    response = await client.get(PREFIX + "/scenarios")
    assert response.status_code == 200
    scenarios = response.json()
    assert len(scenarios) >= 7
    assert len({s["id"] for s in scenarios}) == len(scenarios)
    for scenario in scenarios:
        assert all(scenario[key] for key in ("id", "name", "description", "expected_advance"))


async def test_advance_на_system_clock_даёт_409(client, monkeypatch):
    monkeypatch.setattr(app.clock, "get_clock", lambda: SystemClock())
    response = await client.post(PREFIX + "/clock/advance", json={"hours": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLOCK_NOT_MOCK"
    assert "USE_MODEL_CLOCK=true" in response.json()["detail"]["message"]


@pytest.mark.parametrize("payload", [{"hours": 48}, {"days": 2}])
async def test_advance_на_model_clock_работает(client, clock, payload):
    before = clock.now()
    response = await client.post(PREFIX + "/clock/advance", json=payload)
    assert response.status_code == 200
    assert clock.now() == before + timedelta(days=2)
    assert response.json()["fired"] == []
    assert response.json()["routes_affected"] == 0
    assert response.json()["elapsed_ms"] >= 0


async def test_откат_времени_запрещён_409(client, clock):
    before = clock.now()
    response = await client.post(
        PREFIX + "/clock/set", json={"to": (before - timedelta(seconds=1)).isoformat()}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLOCK_IN_PAST"
    assert clock.now() == before


async def test_демо_роут_объявлен_раньше_параметризованных(client):
    paths = list((await client.get("/openapi.json")).json()["paths"])
    position = paths.index(PREFIX + "/clock")
    for index, path in enumerate(paths):
        if path.startswith(PREFIX + "/") and "{" in path:
            assert position < index
    assert position < paths.index(PREFIX + "/scenarios")


async def test_таймеры_исполняются_один_раз_и_фильтруются(client, clock):
    engine = get_timer_engine(clock)
    route_id = uuid4()
    effect = Effect("notify_reminder", route_id, "notification", "sms")
    engine.schedule(clock.now() + timedelta(hours=1), effect)
    engine.schedule(
        clock.now() + timedelta(days=3), Effect("escalate", uuid4(), "task", assignee_role="doctor")
    )
    pending = (await client.get(PREFIX + "/timers", params={"route_id": str(route_id)})).json()
    assert len(pending) == 1
    assert pending[0]["timer_type"] == "notify_reminder"
    response = await client.post(PREFIX + "/clock/advance", json={"days": 2})
    assert response.json()["routes_affected"] == 1
    assert response.json()["fired"] == [
        {
            "timer_type": "notify_reminder",
            "route_id": str(route_id),
            "kind": "notification",
            "channel": "sms",
            "assignee_role": None,
        }
    ]
    assert (await client.post(PREFIX + "/clock/advance", json={"hours": 0})).json()["fired"] == []


async def test_set_исполняет_таймеры_reset_очищает_очередь(client, clock):
    before = clock.now()
    engine = get_timer_engine(clock)
    engine.schedule(before + timedelta(hours=1), Effect("create_task", uuid4(), "task"))
    response = await client.post(
        PREFIX + "/clock/set", json={"to": (before + timedelta(days=1)).isoformat()}
    )
    assert response.status_code == 200
    assert len(response.json()["fired"]) == 1
    engine.schedule(clock.now() + timedelta(days=1), Effect("escalate", uuid4(), "task"))
    assert (await client.post(PREFIX + "/clock/reset")).status_code == 200
    assert clock.now() == before
    assert (await client.get(PREFIX + "/timers")).json() == []


@pytest.mark.parametrize("payload", [{}, {"hours": 1, "days": 1}, {"hours": -1}, {"days": "NaN"}])
async def test_неверный_интервал_не_меняет_время(client, clock, payload):
    before = clock.now()
    assert (await client.post(PREFIX + "/clock/advance", json=payload)).status_code == 422
    assert clock.now() == before

"""Проверки защиты: модельное время и каталог сценариев без БД.

Таймеры в боевой версии живут в таблице ``timer``, поэтому здесь проверяется
контракт модельного времени: источник часов, отказ прокрутки на системных
часах, запрет отката и порядок объявления роутов. Обращения к таймерам БД
не делают — подключение к базе в демо-ручках необязательное.
"""

from datetime import timedelta

import pytest

import app.clock
import app.services.timers
from app.clock import ModelClock, SystemClock

pytestmark = pytest.mark.anyio
PREFIX = "/api/v1/demo"


@pytest.fixture
def clock(monkeypatch):
    """Каждый показ начинается с чистых модельных часов на старте сценария."""
    value = ModelClock()
    monkeypatch.setattr(app.clock, "get_clock", lambda: value)
    monkeypatch.setattr(app.services.timers, "get_clock", lambda: value)
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
    assert len(scenarios) == 7
    assert len({s["id"] for s in scenarios}) == len(scenarios)
    for scenario in scenarios:
        assert all(scenario[key] for key in ("id", "name", "description", "expected_advance"))


async def test_advance_на_system_clock_даёт_409(client, monkeypatch):
    monkeypatch.setattr(app.clock, "get_clock", lambda: SystemClock())
    monkeypatch.setattr(app.services.timers, "get_clock", lambda: SystemClock())
    response = await client.post(PREFIX + "/clock/advance", json={"hours": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLOCK_NOT_MOCK"
    assert "USE_MODEL_CLOCK=true" in response.json()["detail"]["message"]


@pytest.mark.parametrize("payload", [{"hours": 48}, {"days": 2}])
async def test_advance_на_model_clock_сдвигает_время(client, clock, payload):
    before = clock.now()
    response = await client.post(PREFIX + "/clock/advance", json=payload)
    assert response.status_code == 200
    assert clock.now() == before + timedelta(days=2)
    body = response.json()
    assert body["from"] == before.isoformat().replace("+00:00", "Z")
    assert body["to"] == clock.now().isoformat().replace("+00:00", "Z")
    assert body["fired"] == []
    assert body["routes_affected"] == 0
    assert body["elapsed_ms"] >= 0


async def test_set_переводит_время_вперёд(client, clock):
    before = clock.now()
    target = before + timedelta(days=3)
    response = await client.post(PREFIX + "/clock/set", json={"to": target.isoformat()})
    assert response.status_code == 200
    assert clock.now() == target
    assert response.json()["routes_affected"] == 0


async def test_откат_времени_запрещён_409(client, clock):
    before = clock.now()
    response = await client.post(
        PREFIX + "/clock/set", json={"to": (before - timedelta(seconds=1)).isoformat()}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLOCK_IN_PAST"
    assert clock.now() == before


async def test_reset_возвращает_старт_сценария(client, clock):
    clock.advance(72)
    response = await client.post(PREFIX + "/clock/reset")
    assert response.status_code == 200
    assert response.json() == {
        "now": "2026-08-26T14:32:00Z",
        "is_mock": True,
        "source": "model",
    }
    assert clock.now().isoformat().replace("+00:00", "Z") == "2026-08-26T14:32:00Z"


@pytest.mark.parametrize("payload", [{}, {"hours": 1, "days": 1}, {"hours": -1}, {"days": "NaN"}])
async def test_неверный_интервал_не_меняет_время(client, clock, payload):
    before = clock.now()
    assert (await client.post(PREFIX + "/clock/advance", json=payload)).status_code == 422
    assert clock.now() == before


async def test_демо_роут_объявлен_раньше_параметризованных(client):
    paths = list((await client.get("/openapi.json")).json()["paths"])
    position = paths.index(PREFIX + "/clock")
    for index, path in enumerate(paths):
        if path.startswith(PREFIX + "/") and "{" in path:
            assert position < index
    assert position < paths.index(PREFIX + "/scenarios")

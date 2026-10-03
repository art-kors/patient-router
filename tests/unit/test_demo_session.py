"""Демо-ручки не должны отдавать 503 при живой базе.

На сквозном прогоне подряд шли ``GET /routes/{uuid}/timeline`` → 200 и
``GET /demo/timers`` → 503 DATABASE_UNAVAILABLE по одной и той же базе.
Причина была не в базе: ``optional_session`` открывал отдельное соединение и
мерял его жёстким двухсекундным таймаутом. Пока пул занят предыдущим
эндпоинтом, свежая сессия не укладывалась — и ручка, которую демонстратор
показывает жюри для выбора следующего шага, отдавала отказ.

Здесь проверяется контракт: при рабочей базе ``/demo/timers`` отвечает всегда
(даже когда соединение из пула достаётся медленно), 503 означает только
действительно отсутствующую базу, а часы и каталог сценариев работают без неё.
Настоящий PostgreSQL не нужен — сессия подменяется.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout

import app.clock
import app.services.timers
from app.clock import ModelClock
from app.db import get_session

pytestmark = pytest.mark.anyio
PREFIX = "/api/v1/demo"

# Сколько ждал прежний пробный таймаут: медленнее — и ручка обязана ответить.
OLD_PROBE_TIMEOUT = 2.0


def db_is_down() -> SQLAlchemyError:
    """Ошибка, которую asyncpg поднимает на недоступном хосте."""
    return OperationalError("SELECT 1", {}, OSError("Connection refused"))


def db_socket_is_gone() -> FileNotFoundError:
    """Что приходит на деле, когда unix-сокет PostgreSQL исчез.

    asyncpg не оборачивает ошибку ОС в ``SQLAlchemyError``, поэтому ручка
    обязана ловить и её — иначе вместо внятного 503 на защиту уходит стек.
    """
    return FileNotFoundError(2, "No such file or directory")


class FakeScalarResult:
    """Ответ ``session.scalars()`` в том виде, в каком его читает ручка."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Сессия из пула под нашим контролем: задержка, обрыв или ответ."""

    def __init__(self, rows=(), error=None, delay=0.0):
        self.rows = list(rows)
        self.error = error
        self.delay = delay
        self.connects = 0
        self.rollbacks = 0
        self.flushes = 0
        self.statements = []

    async def connect(self):
        """Ручка не должна открывать отдельное пробное соединение."""
        self.connects += 1

    async def rollback(self):
        self.rollbacks += 1

    async def flush(self):
        self.flushes += 1

    async def execute(self, statement):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return FakeScalarResult(self.rows)

    async def scalars(self, statement):
        self.statements.append(statement)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return FakeScalarResult(self.rows)

    def commands(self) -> list[str]:
        """Только DML/DDL: заодно отсекает служебный SELECT 1 проверки."""
        return [text for text in map(str, self.statements) if "SELECT 1" not in text]


@pytest.fixture
def clock(monkeypatch):
    """Чистые модельные часы на старте сценария — как на защите."""
    value = ModelClock(start=datetime(2026, 8, 26, 14, 32, tzinfo=UTC))
    monkeypatch.setattr(app.clock, "get_clock", lambda: value)
    monkeypatch.setattr(app.services.timers, "get_clock", lambda: value)
    return value


@pytest.fixture
def use_session(monkeypatch):
    """Подменить сессию из пула на все ручки приложения."""

    def install(session: FakeSession) -> FakeSession:
        async def dependency() -> AsyncIterator[FakeSession]:
            yield session

        from app.main import app

        monkeypatch.setitem(app.dependency_overrides, get_session, dependency)
        return session

    return install


def timer_row():
    """Строка таймера в том виде, в каком её отдаёт боевой SELECT."""
    return SimpleNamespace(
        id=uuid4(),
        route_id=uuid4(),
        timer_type="notify_initial",
        due_at=datetime(2026, 8, 27, 14, 32, tzinfo=UTC),
        channel="lk",
    )


# ── Регрессия: ложный 503 на живом пуле ─────────────────────────────────────


async def test_таймеры_отдают_200_когда_соединение_берётся_дольше_старого_таймаута(
    client, use_session
):
    """Занятый пул замедляет выдачу соединения — это не повод отказывать.

    Раньше здесь стоял ``asyncio.wait_for(session.connect(), 2.0)``: сессия
    не укладывалась, ``require_session`` превращал это в 503. Теперь ручка
    ждёт своего соединения и отвечает данными.
    """
    row = timer_row()
    session = use_session(FakeSession(rows=[row], delay=OLD_PROBE_TIMEOUT + 0.2))

    response = await client.get(PREFIX + "/timers")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(row.id),
            "route_id": str(row.route_id),
            "timer_type": "notify_initial",
            "due_at": "2026-08-27T14:32:00Z",
            "channel": "lk",
        }
    ]
    assert session.connects == 0, "отдельное пробное соединение больше не нужно"


async def test_таймеры_читаются_из_той_же_сессии_что_и_остальные_ручки(client, use_session):
    """Демо не заводит собственного обхода пула — тот же источник данных."""
    session = use_session(FakeSession(rows=[timer_row()]))

    await client.get(PREFIX + "/timers")

    assert len(session.statements) == 1, "одна выборка вместо пробы базы и запроса"
    assert "FROM timer" in str(session.statements[0])


# ── 503 остаётся для действительно отсутствующей базы ───────────────────────


@pytest.mark.parametrize("error", [db_is_down(), db_socket_is_gone()], ids=["driver", "socket"])
async def test_таймеры_дают_503_когда_базы_нет(client, use_session, error):
    """Отказ сохраняется, но означает ровно одно: PostgreSQL недоступен."""
    use_session(FakeSession(error=error))

    response = await client.get(PREFIX + "/timers")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"
    assert "docker compose up -d db" in response.json()["detail"]["message"]


async def test_таймеры_дают_503_а_не_500_когда_пул_исчерпан(client, use_session):
    """Исчерпание пула — тоже отсутствие базы для ручки, но не серверная ошибка."""
    use_session(FakeSession(error=PoolTimeout("QueuePool", {}, Exception("pool exhausted"))))

    response = await client.get(PREFIX + "/timers")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"


async def test_таймеры_не_отдают_503_просто_из_за_медленного_пула(client, use_session):
    """Конкретный сценарий из лога: таймеры читаются после маршрута."""
    timeline = FakeSession(rows=[timer_row()])
    use_session(timeline)
    assert (await client.get(PREFIX + "/timers")).status_code == 200

    session = use_session(FakeSession(rows=[timer_row()], delay=OLD_PROBE_TIMEOUT + 0.2))
    assert (await client.get(PREFIX + "/timers")).status_code == 200
    assert session.connects == 0


# ── Намеренное поведение: часы и каталог работают без базы ──────────────────


async def test_часы_работают_без_базы(client, use_session, clock):
    """Часы показывают время, даже когда базы нет вовсе."""
    session = use_session(FakeSession(error=db_is_down()))

    response = await client.get(PREFIX + "/clock")

    assert response.status_code == 200
    assert response.json()["source"] == "model"
    assert session.connects == 0


async def test_сценарии_работают_без_базы(client, use_session):
    """Каталог сценариев — статический, БД ему не нужна."""
    use_session(FakeSession(error=db_is_down()))

    response = await client.get(PREFIX + "/scenarios")

    assert response.status_code == 200
    assert len(response.json()) == 7


async def test_прокрутка_времени_работает_без_базы(client, use_session, clock):
    """Часы двигаются по слушателям, эффекты из таблицы timer — не требуются."""
    before = clock.now()
    use_session(FakeSession(error=db_is_down()))

    response = await client.post(PREFIX + "/clock/advance", json={"hours": 1})

    assert response.status_code == 200
    assert clock.now() == before.replace(hour=before.hour + 1)
    body = response.json()
    assert body["fired"] == []
    assert body["routes_affected"] == 0
    assert body["database"] == "unavailable"


async def test_прокрутка_времени_отмечает_доступность_базы(client, use_session, clock):
    """Демонстратор видит, применялись ли таймеры из БД."""
    use_session(FakeSession())

    body = (await client.post(PREFIX + "/clock/advance", json={"hours": 1})).json()

    assert body["database"] == "ok"


async def test_прокрутка_времени_выживает_при_исчерпанном_пуле(client, use_session, clock):
    """Даже израсходованный пул не валит показ часов."""
    use_session(FakeSession(error=PoolTimeout("QueuePool", {}, Exception("pool exhausted"))))

    response = await client.post(PREFIX + "/clock/advance", json={"hours": 1})

    assert response.status_code == 200
    assert response.json()["database"] == "unavailable"


async def test_переход_на_момент_сценария_работает_без_базы(client, use_session, clock):
    """``/clock/set`` — такой же часовой интерфейс, база ему не нужна."""
    use_session(FakeSession(error=db_is_down()))
    target = clock.now().replace(day=clock.now().day + 3)

    response = await client.post(PREFIX + "/clock/set", json={"to": target.isoformat()})

    assert response.status_code == 200
    assert clock.now() == target


async def test_сброс_демо_работает_без_базы(client, use_session, clock):
    """Сброс возвращает часы к старту и гасит таймеры только если база жива."""
    clock.advance(72)
    session = use_session(FakeSession(error=db_is_down()))

    response = await client.post(PREFIX + "/clock/reset")

    assert response.status_code == 200
    assert response.json()["now"] == "2026-08-26T14:32:00Z"
    assert not session.commands(), "при мёртвой базе UPDATE отправлять нечего"


async def test_сброс_демо_гасит_таймеры_при_живой_базе(client, use_session, clock):
    """При рабочей базе сброс по-прежнему гасит несработавшие таймеры."""
    clock.advance(72)
    session = use_session(FakeSession())

    assert (await client.post(PREFIX + "/clock/reset")).status_code == 200

    assert any("UPDATE timer" in command for command in session.commands())

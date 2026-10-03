"""Защитить сроки контактов и демонстрацию эскалаций без запущенной БД."""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.clock import SystemClock, set_clock
from app.models import Route, Timer, TimerType
from app.services.timers import Effect, TimerEngine, escalation_schedule, hospitalization_schedule


def test_расписание_содержит_6_таймеров():
    assert len(escalation_schedule(14)) == 6


def test_первый_таймер_через_минуту():
    assert escalation_schedule(14)[0] == (TimerType.NOTIFY_INITIAL, 1 / 60, "notify_initial")


def test_напоминание_через_24_часа():
    assert escalation_schedule(14)[1] == (TimerType.NOTIFY_REMINDER, 24, "notify_reminder")


def test_закрытие_через_30_дней():
    assert escalation_schedule(14)[-1] == (TimerType.CLOSE_ROUTE, 720, "close_route")


@pytest.mark.parametrize("sla", range(1, 7))
def test_при_коротком_sla_задача_раньше_срока(sla):
    schedule = escalation_schedule(sla)
    task_hours = next(hours for kind, hours, _ in schedule if kind == TimerType.CREATE_TASK)
    assert task_hours < sla * 24
    assert all(
        hours <= sla * 24 for kind, hours, _ in schedule if kind == TimerType.NOTIFY_REMINDER
    )
    assert [hours for _, hours, _ in schedule] == sorted(hours for _, hours, _ in schedule)
    assert schedule[-1][1] == 720


def test_больницы_отдельное_расписание():
    assert hospitalization_schedule() == [
        (TimerType.CREATE_TASK, 24, "create_task"),
        (TimerType.CREATE_TASK, 72, "create_task"),
        (TimerType.ESCALATE, 120, "escalate"),
    ]


@pytest.mark.parametrize(
    ("timer_type", "kind", "channel", "role"),
    [
        (TimerType.NOTIFY_INITIAL, "notification", "lk", None),
        (TimerType.NOTIFY_REMINDER, "notification", "lk", None),
        (TimerType.CREATE_TASK, "task", None, "coordinator"),
        (TimerType.ESCALATE, "escalate", None, "coordinator"),
        (TimerType.CLOSE_ROUTE, "close_route", None, None),
        (TimerType.SCHEDULE_FOLLOWUP, "followup", None, None),
    ],
)
async def test_эффекты_для_каждого_типа_таймера(model_clock, timer_type, kind, channel, role):
    timer = Timer(
        id=uuid4(), route_id=uuid4(), timer_type=timer_type, due_at=model_clock.now(), fired=False
    )
    session = AsyncMock()
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=timer.id))
    effect = await TimerEngine().fire(session, timer)
    assert effect == Effect(kind, channel, role, timer_type.value, timer.route_id, timer_type)
    assert timer.fired is True
    assert timer.fired_at == model_clock.now()
    session.flush.assert_awaited_once()


async def test_advance_на_SystemClock_бросает_CLOCK_NOT_MOCK():
    set_clock(SystemClock())
    try:
        with pytest.raises(ValueError, match="^CLOCK_NOT_MOCK$"):
            await TimerEngine().advance_clock(24)
    finally:
        set_clock(None)


async def test_advance_на_ModelClock_стреляет_по_таймерам(model_clock):
    start = model_clock.now()
    route_id = uuid4()

    class Listener:
        def __init__(self):
            self.pending = [
                Timer(
                    route_id=route_id,
                    timer_type=kind,
                    due_at=start + timedelta(hours=hours),
                    fired=False,
                )
                for kind, hours, _ in escalation_schedule(14)
            ]

        def collect_due(self, until):
            fired = []
            for timer in self.pending:
                if not timer.fired and timer.due_at <= until:
                    timer.fired = True
                    timer.fired_at = until
                    fired.append(timer)
            return fired

    model_clock.register(Listener())
    engine = TimerEngine()
    result = await engine.advance_clock(30 * 24)
    assert len(result["fired"]) == 6
    assert result["from"] == start
    assert result["to"] == start + timedelta(days=30)
    assert result["routes_affected"] == 1
    assert result["elapsed_ms"] >= 0
    assert (await engine.advance_clock(0))["fired"] == []


async def test_advance_с_сессией_исполняет_таймеры_БД(model_clock):
    session = AsyncMock()
    engine = TimerEngine(session)
    effect = Effect("task", None, "coordinator", "create_task", uuid4(), TimerType.CREATE_TASK)
    engine.fire_due = AsyncMock(return_value=[effect])
    result = await engine.advance_clock(24)
    engine.fire_due.assert_awaited_once_with(session, model_clock.now())
    assert result["fired"] == [effect]
    assert result["routes_affected"] == 1


@pytest.mark.parametrize("hours", [-1, float("inf"), float("nan")])
async def test_невалидный_сдвиг_не_меняет_часы(model_clock, hours):
    start = model_clock.now()
    with pytest.raises(ValueError, match="CLOCK_INVALID_ADVANCE"):
        await TimerEngine().advance_clock(hours)
    assert model_clock.now() == start


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("hospitalization", [False, True])
async def test_планирование_привязано_к_нужному_старту_и_пропускает_дубли(
    model_clock, hospitalization
):
    start = model_clock.now()
    route = Route(id=uuid4(), created_at=start - timedelta(days=10))
    session = AsyncMock()
    session.scalars.return_value = Mock(all=Mock(return_value=[]))
    engine = TimerEngine()
    if hospitalization:
        created = await engine.schedule_hospitalization(session, route)
        expected_start, first_hours = start, 24
    else:
        created = await engine.schedule_route(session, route, 14)
        expected_start, first_hours = route.created_at, 1 / 60
    statement = session.scalars.call_args.args[0]
    assert "ON CONFLICT ON CONSTRAINT uq_timer_route_type_due DO NOTHING" in sql(statement)
    params = statement.compile(dialect=postgresql.dialect()).params
    assert params["due_at_m0"] == expected_start + timedelta(hours=first_hours)
    assert created == []


async def test_due_использует_условие_индекса_и_порядок(model_clock):
    session = AsyncMock()
    session.scalars.return_value = Mock(all=Mock(return_value=[]))
    assert await TimerEngine().due(session) == []
    statement = session.scalars.call_args.args[0]
    query = sql(statement)
    assert "NOT timer.fired" in query
    assert "ORDER BY timer.due_at" in query
    assert "FOR UPDATE SKIP LOCKED" in query
    assert model_clock.now() in statement.compile().params.values()


@pytest.mark.parametrize("limited", [False, True])
async def test_отмена_гасит_только_несработавшие_таймеры(model_clock, limited):
    session = AsyncMock()
    session.execute.return_value = Mock(rowcount=3)
    before = model_clock.now() if limited else None
    assert await TimerEngine().cancel_for_route(session, uuid4(), before) == 3
    query = sql(session.execute.call_args.args[0])
    assert "NOT timer.fired" in query
    assert ("timer.due_at <=" in query) is limited


async def test_повторный_fire_не_возвращает_эффект(model_clock):
    session = AsyncMock()
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=None))
    timer = Timer(id=uuid4(), route_id=uuid4(), timer_type=TimerType.CREATE_TASK, fired=True)
    with pytest.raises(ValueError, match="TIMER_ALREADY_FIRED"):
        await TimerEngine().fire(session, timer)


async def test_fire_due_передаёт_единый_момент_всем_таймерам(model_clock):
    engine = TimerEngine()
    timers = [Mock(), Mock()]
    engine.due = AsyncMock(return_value=timers)
    engine.fire = AsyncMock(side_effect=["first", "second"])
    session = AsyncMock()
    assert await engine.fire_due(session) == ["first", "second"]
    engine.due.assert_awaited_once_with(session, model_clock.now())
    assert [call.args for call in engine.fire.await_args_list] == [
        (session, timer, model_clock.now()) for timer in timers
    ]

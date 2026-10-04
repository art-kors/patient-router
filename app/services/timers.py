"""Эскалации помогают не потерять пациента между этапами маршрута."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from time import perf_counter
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import ModelClock, get_clock
from app.models import Route, Timer, TimerType


def escalation_schedule(target_sla_days: int) -> list[tuple[TimerType, float, str]]:
    """Помочь пациенту записаться вовремя, ускоряя контакт при коротком SLA.

    Для SLA меньше недели интервалы контактов сжимаются относительно 14 дней:
    последнее напоминание попадает на целевой срок, звонок — раньше него.
    Первое уведомление и закрытие через 30 дней сохраняют сроки кейса.
    Третий элемент — код шаблона эффекта.
    """
    if target_sla_days <= 0:
        raise ValueError("SLA_MUST_BE_POSITIVE")
    scale = target_sla_days / 14 if target_sla_days < 7 else 1.0
    return [
        (TimerType.NOTIFY_INITIAL, 1 / 60, "notify_initial"),
        (TimerType.NOTIFY_REMINDER, 24 * scale, "notify_reminder"),
        (TimerType.NOTIFY_REMINDER, 72 * scale, "notify_reminder"),
        (TimerType.CREATE_TASK, 5 * 24 * scale, "create_task"),
        (TimerType.NOTIFY_REMINDER, 14 * 24 * scale, "notify_reminder"),
        (TimerType.CLOSE_ROUTE, 30 * 24.0, "close_route"),
    ]


def hospitalization_schedule() -> list[tuple[TimerType, float, str]]:
    """Не дать направлению в стационар остаться без назначенной даты."""
    return [
        (TimerType.CREATE_TASK, 24.0, "create_task"),
        (TimerType.CREATE_TASK, 72.0, "create_task"),
        (TimerType.ESCALATE, 5 * 24.0, "escalate"),
    ]


@dataclass(frozen=True)
class Effect:
    """Передать намерение обработчику без отправки сообщений внутри транзакции."""

    kind: str
    channel: str | None
    assignee_role: str | None
    template_code: str | None
    route_id: UUID
    timer_type: TimerType


class TimerEngine:
    """Согласовать эскалации с модельным временем и транзакцией вызывающего кода.

    Для прокрутки таймеров БД передайте сессию в конструктор. Без сессии
    advance_clock собирает эффекты зарегистрированных слушателей ModelClock.
    Коммит остаётся ответственностью вызывающего кода.
    """

    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    async def schedule_route(
        self, session: AsyncSession, route: Route, target_sla_days: int
    ) -> list[Timer]:
        """Привязать эскалации к старту маршрута, сохраняя повторный вызов безопасным."""
        schedule = escalation_schedule(target_sla_days)
        if route.created_at is None:
            route.created_at = get_clock().now()
        return await self._schedule(session, route, route.created_at, schedule)

    async def schedule_return(self, session: AsyncSession, route: Route) -> list[Timer]:
        """После несостоявшегося приёма начать новую цепочку с контакта через 45 минут."""
        await self.cancel_for_route(session, route.id)
        schedule = escalation_schedule(14)
        schedule[0] = (TimerType.NOTIFY_INITIAL, 0.75, "notify_initial")
        return await self._schedule(session, route, get_clock().now(), schedule)

    async def schedule_hospitalization(self, session: AsyncSession, route: Route) -> list[Timer]:
        """Дать стационару полный срок контроля независимо от возраста маршрута.

        Вызывается при направлении; началом отсчёта служит текущее время часов.
        """
        return await self._schedule(session, route, get_clock().now(), hospitalization_schedule())

    async def _schedule(
        self,
        session: AsyncSession,
        route: Route,
        start: datetime,
        schedule: list[tuple[TimerType, float, str]],
    ) -> list[Timer]:
        """Исключить дубли даже при конкурентном планировании одного маршрута."""
        await session.flush()
        values = [
            {
                "route_id": route.id,
                "timer_type": timer_type,
                "due_at": start + timedelta(hours=hours),
                "fired": False,
                "channel": "lk"
                if timer_type in (TimerType.NOTIFY_INITIAL, TimerType.NOTIFY_REMINDER)
                else None,
            }
            for timer_type, hours, _template in schedule
        ]
        statement = (
            insert(Timer)
            .values(values)
            .on_conflict_do_nothing(constraint="uq_timer_route_type_due")
            .returning(Timer)
        )
        return list((await session.scalars(statement)).all())

    async def due(self, session: AsyncSession, now: datetime | None = None) -> list[Timer]:
        """Выбрать просроченные контакты без конкуренции между обработчиками.

        Предикат NOT fired совпадает с частичным индексом idx_timer_due.
        Блокировки удерживаются до завершения транзакции вызывающим кодом.
        """
        now = now if now is not None else get_clock().now()
        await session.flush()
        statement = (
            select(Timer)
            .where(~Timer.fired, Timer.due_at <= now)
            .order_by(Timer.due_at, Timer.id)
            .with_for_update(skip_locked=True)
        )
        return list((await session.scalars(statement)).all())

    async def fire(
        self, session: AsyncSession, timer: Timer, now: datetime | None = None
    ) -> Effect:
        """Зафиксировать контакт ровно один раз перед передачей эффекта обработчику."""
        now = now if now is not None else get_clock().now()
        timer_type = TimerType(timer.timer_type)
        kinds = {
            TimerType.NOTIFY_INITIAL: "notification",
            TimerType.NOTIFY_REMINDER: "notification",
            TimerType.CREATE_TASK: "task",
            TimerType.ESCALATE: "escalate",
            TimerType.CLOSE_ROUTE: "close_route",
            TimerType.SCHEDULE_FOLLOWUP: "followup",
        }
        statement = (
            update(Timer)
            .where(Timer.id == timer.id, ~Timer.fired)
            .values(fired=True, fired_at=now)
            .returning(Timer.id)
            .execution_options(synchronize_session=False)
        )
        if (await session.execute(statement)).scalar_one_or_none() is None:
            raise ValueError("TIMER_ALREADY_FIRED")
        timer.fired = True
        timer.fired_at = now
        await session.flush()
        return Effect(
            kind=kinds[timer_type],
            channel=(timer.channel or "lk") if kinds[timer_type] == "notification" else None,
            assignee_role="coordinator"
            if timer_type in (TimerType.CREATE_TASK, TimerType.ESCALATE)
            else None,
            template_code=timer_type.value,
            route_id=timer.route_id,
            timer_type=timer_type,
        )

    async def fire_due(self, session: AsyncSession, now: datetime | None = None) -> list[Effect]:
        """Отработать весь накопившийся долг при одном скачке модельного времени."""
        now = now if now is not None else get_clock().now()
        return [await self.fire(session, timer, now) for timer in await self.due(session, now)]

    async def cancel_for_route(
        self, session: AsyncSession, route_id: UUID, before: datetime | None = None
    ) -> int:
        """Остановить ненужные контакты после отмены записи или закрытия маршрута.

        Погашение отмечается fired без fired_at: эффект не исполнялся.
        """
        await session.flush()
        statement = update(Timer).where(Timer.route_id == route_id, ~Timer.fired)
        if before is not None:
            statement = statement.where(Timer.due_at <= before)
        result = await session.execute(statement.values(fired=True, fired_at=None))
        return result.rowcount

    async def advance_clock(self, hours: float) -> dict:
        """Показать длительный сценарий за секунды с эффектами слушателей и БД."""
        clock = get_clock()
        if not isinstance(clock, ModelClock):
            raise ValueError("CLOCK_NOT_MOCK")
        if not isfinite(hours) or hours < 0:
            raise ValueError("CLOCK_INVALID_ADVANCE")
        started = perf_counter()
        previous = get_clock().now()
        fired = clock.advance(hours)
        current = get_clock().now()
        if self.session is not None:
            fired.extend(await self.fire_due(self.session, current))
        route_ids = {
            item.get("route_id") if isinstance(item, dict) else getattr(item, "route_id", None)
            for item in fired
        }
        route_ids.discard(None)
        return {
            "from": previous,
            "to": current,
            "fired": fired,
            "routes_affected": len(route_ids),
            "elapsed_ms": (perf_counter() - started) * 1000,
        }

"""Управление демо: показать недельные клинические сценарии за секунды.

Ручки работают поверх боевого ``TimerEngine``: таймеры лежат в таблице ``timer``,
поэтому ``/demo/clock/advance`` отдаёт реальные эффекты, а не очередь в памяти.
Модельное время живёт само по себе — для показа часов и каталога сценариев БД
не нужна, сессия подключается лениво и тихо отключается, если база недоступна.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.clock as clock_module
from app.clock import ModelClock
from app.db import SessionFactory
from app.models import Timer
from app.services.timers import TimerEngine

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])
START = datetime(2026, 8, 26, 14, 32, tzinfo=UTC)
DB_PROBE_TIMEOUT = 2.0


class ClockOut(BaseModel):
    """Источник времени нужен, чтобы объяснить доступность прокрутки."""

    now: datetime
    is_mock: bool
    source: Literal["model", "system"]


class AdvanceIn(BaseModel):
    """Одна единица интервала исключает неоднозначную прокрутку."""

    model_config = ConfigDict(extra="forbid")
    hours: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    days: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def one_interval(self):
        if (self.hours is None) == (self.days is None):
            raise ValueError("Укажите ровно одно поле: hours или days")
        return self


class SetIn(BaseModel):
    """Часовой пояс обязателен для однозначного момента демонстрации."""

    model_config = ConfigDict(extra="forbid")
    to: AwareDatetime


async def optional_session() -> AsyncIterator[AsyncSession | None]:
    """Сессия, если БД отвечает: показ часов не должен падать без базы.

    Соединение проверяется с коротким таймаутом один раз на запрос. Если базы
    нет — отдаём ``None``, и часы, и каталог сценариев продолжают работать.
    """
    session = SessionFactory()
    try:
        await asyncio.wait_for(session.connect(), DB_PROBE_TIMEOUT)
    except Exception:
        await session.close()
        yield None
        return
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def require_session(
    session: AsyncSession | None = Depends(optional_session),
) -> AsyncSession:
    """Для чтения таймеров БД обязательна — иначе показать нечего."""
    if session is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "Таймеры хранятся в БД — поднимите её (docker compose up -d db).",
            },
        )
    return session


def _model_clock() -> ModelClock:
    """Защитить системные часы от команд демонстратора."""
    clock = clock_module.get_clock()
    if not isinstance(clock, ModelClock):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CLOCK_NOT_MOCK",
                "message": "Для демо включите USE_MODEL_CLOCK=true.",
            },
        )
    return clock


@router.get("/clock", response_model=ClockOut, summary="Текущее модельное время")
def read_clock() -> ClockOut:
    """Показать жюри точку сценария и используемый источник времени."""
    clock = clock_module.get_clock()
    return ClockOut(
        now=clock.now(), is_mock=clock.is_mock(), source="model" if clock.is_mock() else "system"
    )


async def _advance(hours: float, session: AsyncSession | None) -> dict[str, Any]:
    """Собрать эффекты и длительность, чтобы подтвердить прокрутку недель за секунды.

    Двигатель сам отдаёт ``from``/``to``/``fired``/``routes_affected``/
    ``elapsed_ms``; коммит остаётся ответственностью вызывающего кода.
    """
    engine = TimerEngine(session)
    try:
        result = await engine.advance_clock(hours)
    except ValueError as exc:
        code = str(exc).split(":")[0]
        if code == "CLOCK_NOT_MOCK":
            raise HTTPException(
                409,
                detail={
                    "code": "CLOCK_NOT_MOCK",
                    "message": "Для демо включите USE_MODEL_CLOCK=true.",
                },
            ) from exc
        raise HTTPException(422, detail="Интервал выходит за допустимый диапазон времени") from exc
    except OverflowError as exc:
        raise HTTPException(422, detail="Интервал выходит за допустимый диапазон времени") from exc
    return {
        "from": result["from"],
        "to": result["to"],
        "fired": list(result["fired"]),
        "routes_affected": result["routes_affected"],
        "elapsed_ms": result["elapsed_ms"],
    }


@router.post("/clock/advance", summary="Прокрутить время и выполнить таймеры")
async def advance(
    payload: AdvanceIn,
    session: AsyncSession | None = Depends(optional_session),
) -> dict[str, Any]:
    """Запустить просроченные действия без реального ожидания недель."""
    _model_clock()
    hours = payload.hours if payload.hours is not None else payload.days * 24
    return await _advance(hours, session)


@router.post("/clock/set", summary="Перейти к моменту сценария")
async def set_time(
    payload: SetIn,
    session: AsyncSession | None = Depends(optional_session),
) -> dict[str, Any]:
    """Перейти вперёд с таймерами, исключив повтор эффектов при откате."""
    clock = _model_clock()
    current = clock.now()
    if payload.to < current:
        raise HTTPException(
            409, detail={"code": "CLOCK_IN_PAST", "message": "Откат модельного времени запрещён."}
        )
    return await _advance((payload.to - current).total_seconds() / 3600, session)


@router.post("/clock/reset", response_model=ClockOut, summary="Вернуться к старту демо")
async def reset_time(
    session: AsyncSession | None = Depends(optional_session),
) -> ClockOut:
    """Начать показ заново, погасив несработавшие таймеры предыдущего демо."""
    clock = _model_clock()
    clock.reset(START)
    if session is not None:
        await session.execute(update(Timer).where(~Timer.fired).values(fired=True, fired_at=None))
    return read_clock()


@router.get("/timers", summary="Посмотреть запланированные таймеры")
async def read_timers(
    session: AsyncSession = Depends(require_session),
    route_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Показать ближайшие действия, чтобы выбрать следующий шаг прокрутки.

    Список берётся из таблицы ``timer`` напрямую: ``TimerEngine.due`` отдаёт
    только просроченное, а демонстратору нужен весь горизонт маршрута.
    """
    statement = select(Timer).where(~Timer.fired).order_by(Timer.due_at, Timer.id)
    if route_id is not None:
        statement = statement.where(Timer.route_id == route_id)
    timers = (await session.scalars(statement)).all()
    return [
        {
            "id": str(timer.id),
            "route_id": str(timer.route_id),
            "timer_type": str(timer.timer_type),
            "due_at": timer.due_at,
            "channel": timer.channel,
        }
        for timer in timers
    ]


SCENARIOS = [
    {
        "id": "happy_path_gynecology",
        "name": "Полип эндометрия: полный путь",
        "description": (
            "Находка → запись к гинекологу → визит → госпитализация → операция → контроль."
        ),
        "expected_advance": "Несколько недель, до контрольного визита",
    },
    {
        "id": "no_booking",
        "name": "Пациент не записался",
        "description": (
            "Показать напоминания, задачи сотрудникам и эскалации за 30 дней за секунды."
        ),
        "expected_advance": "30 дней, по срокам таймеров",
    },
    {
        "id": "no_show",
        "name": "Неявка на приём",
        "description": "Показать возврат пациента в процесс записи через 30–60 минут после неявки.",
        "expected_advance": "30–60 минут",
    },
    {
        "id": "false_positive",
        "name": "Норма с объяснением",
        "description": (
            "Загрузить нормальный протокол: маршрута нет, причины подавления триггеров есть."
        ),
        "expected_advance": "0 часов",
    },
    {
        "id": "hospitalization_stall",
        "name": "Направление без даты госпитализации",
        "description": (
            "Показать контроль зависшего направления и эскалации через 24ч, 72ч и 5–7 дней."
        ),
        "expected_advance": "24 часа → 72 часа → 5–7 дней от направления",
    },
    {
        "id": "return_after_2_months",
        "name": "Возвращение через два месяца",
        "description": "Показать баннер незавершённого маршрута при повторном обращении пациента.",
        "expected_advance": "2 месяца",
    },
    {
        "id": "protocol_correction",
        "name": "Исправление протокола",
        "description": (
            "Повторно обработать исправленный протокол и показать отсутствие дублей маршрута."
        ),
        "expected_advance": "0 часов",
    },
]


@router.get("/scenarios", summary="Каталог сценариев защиты")
def read_scenarios() -> list[dict[str, str]]:
    """Дать демонстратору подсказки, какие возможности кейса показать жюри."""
    return SCENARIOS

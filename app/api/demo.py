"""Управление демо: показать недельные клинические сценарии за секунды."""

from datetime import UTC, datetime
from time import perf_counter
from typing import Literal
from uuid import UUID
from weakref import WeakKeyDictionary

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

import app.clock as clock_module
from app.clock import ModelClock
from app.services.timers import TimerEngine, advance_clock

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])
START = datetime(2026, 8, 26, 14, 32, tzinfo=UTC)
_engines: WeakKeyDictionary[ModelClock, TimerEngine] = WeakKeyDictionary()


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


def get_timer_engine(clock: ModelClock) -> TimerEngine:
    """Общая очередь для планирования и просмотра таймеров текущего демо."""
    if clock not in _engines:
        engine = TimerEngine()
        clock.register(engine)
        _engines[clock] = engine
    return _engines[clock]


@router.get("/clock", response_model=ClockOut, summary="Текущее модельное время")
def read_clock():
    """Показать жюри точку сценария и используемый источник времени."""
    clock = clock_module.get_clock()
    return ClockOut(
        now=clock.now(), is_mock=clock.is_mock(), source="model" if clock.is_mock() else "system"
    )


def _advance(clock: ModelClock, hours: float) -> dict:
    """Собрать эффекты и длительность, чтобы подтвердить прокрутку недель за секунды."""
    started = perf_counter()
    before = clock.now()
    get_timer_engine(clock)
    try:
        effects = advance_clock(clock, hours)
    except (OverflowError, ValueError) as exc:
        raise HTTPException(422, detail="Интервал выходит за допустимый диапазон времени") from exc
    return {
        "from": before,
        "to": clock.now(),
        "fired": effects,
        "routes_affected": len({effect.route_id for effect in effects}),
        "elapsed_ms": (perf_counter() - started) * 1000,
    }


@router.post("/clock/advance", summary="Прокрутить время и выполнить таймеры")
def advance(payload: AdvanceIn):
    """Запустить просроченные действия без реального ожидания недель."""
    clock = _model_clock()
    hours = payload.hours if payload.hours is not None else payload.days * 24
    return _advance(clock, hours)


@router.post("/clock/set", summary="Перейти к моменту сценария")
def set_time(payload: SetIn):
    """Перейти вперёд с таймерами, исключив повтор эффектов при откате."""
    clock = _model_clock()
    if payload.to < clock.now():
        raise HTTPException(
            409, detail={"code": "CLOCK_IN_PAST", "message": "Откат модельного времени запрещён."}
        )
    return _advance(clock, (payload.to - clock.now()).total_seconds() / 3600)


@router.post("/clock/reset", response_model=ClockOut, summary="Вернуться к старту демо")
def reset_time():
    """Начать показ заново, очистив слушателей и очередь предыдущего демо."""
    clock = _model_clock()
    clock.reset(START)
    _engines.pop(clock, None)
    return read_clock()


@router.get("/timers", summary="Посмотреть запланированные таймеры")
def read_timers(route_id: UUID | None = None):
    """Показать ближайшие действия, чтобы выбрать следующий шаг прокрутки."""
    clock = clock_module.get_clock()
    if not isinstance(clock, ModelClock):
        return []
    return [
        {"due_at": t.due_at, "timer_type": t.effect.timer_type, "route_id": t.effect.route_id}
        for t in get_timer_engine(clock).pending(route_id)
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

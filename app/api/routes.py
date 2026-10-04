"""Маршруты пациента, врачебная тактика и история клинических этапов."""

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scope_filter
from app.clock import get_clock
from app.db import get_session
from app.models import (
    AuditLog,
    Clinic,
    Patient,
    Protocol,
    Route,
    RouteStatus,
    RouteStep,
    Specialty,
    Study,
    Tactics,
    Timer,
    TimerType,
    TriggerDef,
    TriggerMatch,
)

router = APIRouter(prefix="/api/v1/routes", tags=["routes"])


class RouteOut(BaseModel):
    """Основные сведения о клиническом маршруте."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID = Field(description="Идентификатор маршрута")
    patient_id: UUID = Field(description="Идентификатор пациента")
    trigger_match_id: UUID | None = Field(description="Идентификатор срабатывания")
    specialty_id: int | None = Field(description="Профиль специалиста")
    clinic_id: int | None = Field(description="Клиника назначения")
    status: RouteStatus = Field(description="Текущий этап маршрута")
    target_date: date | None = Field(description="Целевая дата прохождения маршрута")
    created_at: datetime = Field(description="Время создания маршрута")
    closed_at: datetime | None = Field(description="Время закрытия маршрута")
    close_reason: str | None = Field(description="Причина закрытия маршрута")


class StepOut(BaseModel):
    """Этап маршрута и срок его выполнения."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID = Field(description="Идентификатор этапа")
    step_no: int = Field(description="Порядковый номер этапа")
    status: str = Field(description="Состояние на этапе")
    entered_at: datetime = Field(description="Время начала этапа")
    due_at: datetime | None = Field(description="Срок выполнения этапа")


class TimerOut(BaseModel):
    """Запланированное действие по маршруту."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID = Field(description="Идентификатор таймера")
    timer_type: TimerType = Field(description="Тип запланированного действия")
    due_at: datetime = Field(description="Время срабатывания")
    fired: bool = Field(description="Обработан ли таймер")
    fired_at: datetime | None = Field(description="Время фактического срабатывания")
    channel: str | None = Field(description="Канал связи")


class AuditOut(BaseModel):
    """Основание действия для врача."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID = Field(description="Идентификатор записи аудита")
    actor: str = Field(description="Автор действия")
    action: str = Field(description="Выполненное действие")
    basis: str | None = Field(description="Основание действия")
    details: dict = Field(description="Подробности действия")
    created_at: datetime = Field(description="Время действия")


class RouteDetail(RouteOut):
    """Карточка маршрута с таймерами и последними десятью этапами."""

    timers: list[TimerOut] = Field(description="Таймеры маршрута")
    steps: list[StepOut] = Field(description="Последние этапы в порядке прохождения")


class TimelineOut(BaseModel):
    """Полная история: где пациент остановился и почему."""

    route_id: UUID = Field(description="Идентификатор маршрута")
    steps: list[StepOut] = Field(description="Все этапы по возрастанию номера")
    audit_logs: list[AuditOut] = Field(description="Аудит действий в хронологическом порядке")


class RouteList(BaseModel):
    """Страница маршрутов с общим количеством."""

    items: list[RouteOut] = Field(description="Маршруты страницы")
    total: int = Field(description="Общее число подходящих маршрутов")
    limit: int = Field(description="Размер страницы")
    offset: int = Field(description="Смещение страницы")


class CreateRoute(BaseModel):
    """Ссылки на пациента и результат анализа."""

    patient_external_id: str = Field(min_length=1, description="Внешний идентификатор пациента")
    study_external_id: str | None = Field(default=None, description="Внешняя ссылка исследования")
    protocol_id: UUID = Field(description="Идентификатор протокола")
    trigger_match_id: UUID = Field(description="Идентификатор срабатывания триггера")


class TacticsIn(BaseModel):
    """Обязательное решение врача после приёма."""

    tactics: Tactics | None = Field(default=None, description="Выбранная врачом тактика")
    comment: str = Field(default="", description="Комментарий врача")
    create_referral: bool = Field(default=False, description="Создать направление в стационар")

    @field_validator("tactics", mode="before")
    @classmethod
    def empty_tactics(cls, value):
        """Пустой выбор обрабатывается проектной ошибкой обязательной тактики."""
        return None if isinstance(value, str) and not value.strip() else value


class TransitionIn(BaseModel):
    """Новый этап и клиническое основание перехода."""

    to_status: RouteStatus = Field(description="Следующий этап маршрута")
    basis: str = Field(min_length=1, description="Основание перехода")


def error(status: int, code: str, message: str, **details) -> HTTPException:
    """Формирует проектную ошибку с понятным описанием."""
    return HTTPException(status, detail={"code": code, "message": message, **details})


async def require_route(session: AsyncSession, route_id: UUID) -> Route:
    """Проверяет наличие маршрута до чтения истории или изменения состояния."""
    route = await session.get(Route, route_id)
    if route is None:
        raise error(404, "ROUTE_NOT_FOUND", "Маршрут не найден")
    return route


@router.post("", response_model=RouteOut, status_code=201, summary="Создать маршрут из анализа")
async def create_route(payload: CreateRoute, session: AsyncSession = Depends(get_session)):
    """Создаёт маршрут и таймеры в одной транзакции."""
    patient = await session.scalar(
        select(Patient).where(Patient.external_id == payload.patient_external_id)
    )
    protocol = await session.get(Protocol, payload.protocol_id)
    match = await session.get(TriggerMatch, payload.trigger_match_id)
    study = await session.get(Study, protocol.study_id) if protocol else None
    # У Study нет external_id: внешняя ссылка хранится в document_ref.
    if (
        patient is None
        or study is None
        or match is None
        or match.protocol_id != payload.protocol_id
        or study.patient_id != patient.id
        or (
            payload.study_external_id is not None
            and study.document_ref != payload.study_external_id
        )
    ):
        raise error(404, "STUDY_NOT_FOUND", "Пациент, исследование или срабатывание не найдены")
    existing = await session.scalar(select(Route.id).where(Route.trigger_match_id == match.id))
    if existing:
        raise error(409, "ROUTE_ALREADY_EXISTS", "Маршрут для срабатывания уже существует")
    from app.services.routing import RoutingService
    from app.services.timers import TimerEngine

    trigger = await session.get(TriggerDef, match.trigger_def_id)
    try:
        route = await RoutingService().create_from_match(session, patient.id, match, study.id)
        route.specialty_id = trigger.specialty_id
        await TimerEngine().schedule_route(session, route, trigger.target_sla_days)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if getattr(
            getattr(exc.orig, "diag", None), "constraint_name", None
        ) == "uq_route_trigger_match" or "uq_route_trigger_match" in str(exc.orig):
            raise error(
                409, "ROUTE_ALREADY_EXISTS", "Маршрут для срабатывания уже существует"
            ) from exc
        raise
    except ValueError as exc:
        raise error(409, "INVALID_TRANSITION", str(exc)) from exc
    return route


@router.get("", response_model=RouteList, summary="Список маршрутов")
async def list_routes(
    request: Request,
    status: RouteStatus | None = None,
    specialty: str | None = None,
    clinic: str | None = None,
    overdue: bool | None = None,
    patient_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """Фильтрует очередь по кодам профиля и клиники, статусу и срокам."""
    filters = scope_filter(request, Route.patient_id)
    if status is not None:
        filters.append(Route.status == status)
    if patient_id is not None:
        filters.append(Route.patient_id == patient_id)
    if specialty is not None:
        filters.append(
            Route.specialty_id.in_(select(Specialty.id).where(Specialty.code == specialty))
        )
    if clinic is not None:
        filters.append(Route.clinic_id.in_(select(Clinic.id).where(Clinic.code == clinic)))
    if overdue is not None:
        late = (Route.target_date < get_clock().now().date()) & Route.closed_at.is_(None)
        filters.append(late if overdue else ~func.coalesce(late, False))
    total = await session.scalar(select(func.count()).select_from(Route).where(*filters))
    items = (
        await session.scalars(
            select(Route)
            .where(*filters)
            .order_by(Route.created_at.desc(), Route.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return RouteList(items=items, total=total, limit=limit, offset=offset)


@router.get("/unfinished", response_model=list[RouteOut], summary="Незакрытые маршруты пациента")
async def unfinished(patient_id: UUID, session: AsyncSession = Depends(get_session)):
    """Источник баннера незавершённого клинического маршрута."""
    return (
        await session.scalars(
            select(Route)
            .where(Route.patient_id == patient_id, Route.closed_at.is_(None))
            .order_by(Route.created_at, Route.id)
        )
    ).all()


@router.get("/{route_id}", response_model=RouteDetail, summary="Карточка маршрута")
async def get_route(route_id: UUID, session: AsyncSession = Depends(get_session)):
    """Возвращает маршрут, таймеры и десять последних этапов."""
    route = await require_route(session, route_id)
    timers = (
        await session.scalars(
            select(Timer).where(Timer.route_id == route_id).order_by(Timer.due_at, Timer.id)
        )
    ).all()
    steps = (
        await session.scalars(
            select(RouteStep)
            .where(RouteStep.route_id == route_id)
            .order_by(RouteStep.step_no.desc())
            .limit(10)
        )
    ).all()
    return RouteDetail(
        **RouteOut.model_validate(route).model_dump(), timers=timers, steps=list(reversed(steps))
    )


@router.get("/{route_id}/timeline", response_model=TimelineOut, summary="Полная история маршрута")
async def timeline(route_id: UUID, session: AsyncSession = Depends(get_session)):
    """Показывает все этапы и основания действий врачу."""
    await require_route(session, route_id)
    steps = (
        await session.scalars(
            select(RouteStep).where(RouteStep.route_id == route_id).order_by(RouteStep.step_no)
        )
    ).all()
    logs = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.route_id == route_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    ).all()
    return TimelineOut(route_id=route_id, steps=steps, audit_logs=logs)


async def apply_change(session, route, operation):
    """Переводит ошибки автомата в проектные HTTP-ошибки."""
    from app.services.routing import (
        RoutingTransitionError,
        TacticsRequiredError,
        allowed_transitions,
    )

    try:
        result = await operation
        await session.commit()
        return result
    except TacticsRequiredError as exc:
        raise error(422, "TACTICS_REQUIRED", str(exc)) from exc
    except RoutingTransitionError as exc:
        raise error(
            409,
            "INVALID_TRANSITION",
            str(exc),
            allowed_transitions=sorted(allowed_transitions(route.status)),
        ) from exc


@router.post("/{route_id}/tactics", response_model=RouteOut, summary="Записать тактику врача")
async def tactics(route_id: UUID, payload: TacticsIn, session: AsyncSession = Depends(get_session)):
    """Не допускает завершения приёма без выбранной тактики."""
    if payload.tactics is None:
        raise error(422, "TACTICS_REQUIRED", "Необходимо выбрать врачебную тактику")
    route = await require_route(session, route_id)
    from app.services.routing import RoutingService
    from app.services.timers import TimerEngine

    if payload.create_referral and payload.tactics != Tactics.SURGERY_INDICATED:
        raise error(409, "INVALID_TRANSITION", "Направление требует тактики surgery_indicated")

    async def change():
        """Применяет решение врача и планирует контроль направления."""
        result = await RoutingService().apply_tactics(
            session, route_id, payload.tactics, payload.comment, "doctor"
        )
        if payload.create_referral:
            await TimerEngine().schedule_hospitalization(session, result)
        return result

    return await apply_change(session, route, change())


@router.post(
    "/{route_id}/transition", response_model=RouteOut, summary="Перевести маршрут на следующий этап"
)
async def transition(
    route_id: UUID, payload: TransitionIn, session: AsyncSession = Depends(get_session)
):
    """Выполняет переход по правилам конечного автомата."""
    route = await require_route(session, route_id)
    from app.services.routing import RoutingService

    return await apply_change(
        session,
        route,
        RoutingService().transition(session, route_id, payload.to_status, "doctor", payload.basis),
    )

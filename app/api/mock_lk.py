"""Фейковый кабинет для демонстрации; координатор подключает router к приложению.

Идентификатор пациента задаётся в URL. Авторизация в этом демонстрационном
кабинете отсутствует. Все данные читаются из существующих таблиц проекта.
"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import get_clock
from app.db import get_session
from app.models import (
    Appointment,
    AuditLog,
    Followup,
    Hospitalization,
    Notification,
    Patient,
    Route,
    RouteStatus,
    RouteStep,
    Task,
)
from app.services.routing import TERMINAL_STATUSES

router = APIRouter(prefix="/api/v1/mock/lk", tags=["mock-lk"])

# Формулировки описывают следующий шаг без внутренних кодов и медицинских сокращений.
STAGE_TEXT = {
    "created": ("По результатам УЗИ рекомендована консультация.", "Запишитесь к специалисту."),
    "notified": ("Вам отправлено приглашение на консультацию.", "Запишитесь к специалисту."),
    "awaiting_booking": ("Ожидаем вашу запись на консультацию.", "Выберите удобное время приёма."),
    "booked": ("Вы записаны на консультацию.", "Приходите на приём в назначенное время."),
    "visit_done": ("Консультация состоялась.", "Дождитесь рекомендаций врача."),
    "decision_pending": ("Врач уточняет дальнейшие рекомендации.", "Дождитесь решения врача."),
    "booking_required": ("Нужна новая запись на приём.", "Свяжитесь с клиникой для записи."),
    "no_show": ("Приём не состоялся.", "Свяжитесь с клиникой и выберите новую дату."),
    "referred": ("Врач рекомендовал лечение в стационаре.", "Согласуйте дату с координатором."),
    "hospitalization_scheduled": (
        "Дата поступления в стационар назначена.",
        "Подготовьтесь по рекомендациям врача.",
    ),
    "hospitalized": ("Вы поступили в стационар.", "Следуйте рекомендациям лечащего врача."),
    "operated": ("Операция выполнена.", "Следуйте рекомендациям по восстановлению."),
    "discharged": ("Вы выписаны из стационара.", "Запишитесь на контрольный приём."),
    "followup_scheduled": ("Запланирован контрольный приём.", "Пройдите контрольный приём."),
    "followup_done": ("Контрольный приём состоялся.", "Следуйте рекомендациям врача."),
    "route_not_realized": (
        "Рекомендованный приём не был подтверждён.",
        "Свяжитесь с клиникой для обсуждения дальнейших действий.",
    ),
    "closed_by_patient": (
        "Дальнейшие шаги отменены по вашему решению.",
        "При необходимости свяжитесь с клиникой.",
    ),
    "closed_no_operation": ("Врач завершил этот план лечения.", "Следуйте рекомендациям врача."),
    "cancelled": ("Этот план лечения отменён.", "Уточните актуальные рекомендации в клинике."),
}


class MessageOut(BaseModel):
    """Сообщение в представлении пациента."""

    id: UUID
    date: datetime
    text: str
    read: bool


class StepOut(BaseModel):
    """Пройденный этап понятными словами."""

    date: datetime
    text: str


class PatientRouteOut(BaseModel):
    """Текущий план действий без внутренних статусов."""

    route_id: UUID
    what_happened: str
    what_to_do: str
    next_step_at: datetime | date | None
    next_step_text: str
    history: list[StepOut]


class BannerOut(BaseModel):
    """Напоминание при возвращении в кабинет, независимо от возраста маршрута."""

    visible: bool
    title: str | None = None
    text: str | None = None
    route_ids: list[UUID] = []


class TaskOut(BaseModel):
    """Сообщение и параметры задачи координатора."""

    id: UUID
    patient_id: UUID
    route_id: UUID
    date: datetime
    text: str
    channel: str
    priority: int
    due_at: datetime
    status: str


async def require_patient(session: AsyncSession, patient_id: UUID) -> None:
    """Отличает неизвестного пациента от кабинета без сообщений."""
    if await session.get(Patient, patient_id) is None:
        raise HTTPException(404, detail="Пациент не найден")


def open_routes(patient_id: UUID):
    """Исключает закрытые маршруты и конечные состояния без даты закрытия."""
    return select(Route).where(
        Route.patient_id == patient_id,
        Route.closed_at.is_(None),
        Route.status.not_in(TERMINAL_STATUSES),
    )


@router.get("/coordinator/tasks", response_model=list[TaskOut])
async def coordinator_tasks(session: AsyncSession = Depends(get_session)):
    """Возвращает задачи и эскалации, адресованные координатору."""
    rows = (
        await session.execute(
            select(Task, Notification, Route.patient_id)
            .join(Notification, Notification.id == Task.id)
            .join(Route, Route.id == Task.route_id)
            .where(Task.assignee_role == "coordinator")
            .order_by(Task.priority, Task.due_at, Task.id)
        )
    ).all()
    return [
        TaskOut(
            id=task.id,
            patient_id=patient_id,
            route_id=task.route_id,
            date=task.created_at,
            text=message.body,
            channel=task.task_type,
            priority=task.priority,
            due_at=task.due_at,
            status=task.status,
        )
        for task, message, patient_id in rows
    ]


@router.get("/{patient_id}/messages", response_model=list[MessageOut])
async def messages(patient_id: UUID, session: AsyncSession = Depends(get_session)):
    """Показывает только доставленные сообщения кабинета, без служебных задач."""
    await require_patient(session, patient_id)
    items = (
        await session.scalars(
            select(Notification)
            .join(Route, Notification.route_id == Route.id)
            .where(
                Route.patient_id == patient_id,
                Notification.channel == "lk",
                Notification.delivery_status.in_(["sent", "delivered"]),
            )
            .order_by(Notification.sent_at, Notification.id)
        )
    ).all()
    logs = (
        await session.scalars(
            select(AuditLog)
            .join(Route, AuditLog.route_id == Route.id)
            .where(Route.patient_id == patient_id, AuditLog.action == "mock_lk_message_read")
        )
    ).all()
    read_ids = {log.details.get("message_id") for log in logs}
    return [
        MessageOut(id=item.id, date=item.sent_at, text=item.body, read=str(item.id) in read_ids)
        for item in items
    ]


@router.post("/{patient_id}/messages/{message_id}/read", response_model=MessageOut)
async def read_message(
    patient_id: UUID,
    message_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    """Сохраняет прочтение в аудите; повторное прочтение не создаёт новую запись."""
    await require_patient(session, patient_id)
    message = await session.scalar(
        select(Notification)
        .join(Route, Notification.route_id == Route.id)
        .where(
            Notification.id == message_id,
            Route.patient_id == patient_id,
            Notification.channel == "lk",
            Notification.delivery_status.in_(["sent", "delivered"]),
        )
        .with_for_update()
    )
    if message is None:
        raise HTTPException(404, detail="Сообщение не найдено")
    existing = await session.scalar(
        select(AuditLog).where(
            AuditLog.route_id == message.route_id,
            AuditLog.action == "mock_lk_message_read",
            AuditLog.details["message_id"].as_string() == str(message.id),
        )
    )
    if existing is None:
        session.add(
            AuditLog(
                id=uuid4(),
                route_id=message.route_id,
                actor="mock_lk_patient",
                action="mock_lk_message_read",
                details={"message_id": str(message.id)},
                created_at=get_clock().now(),
            )
        )
    await session.commit()
    return MessageOut(id=message.id, date=message.sent_at, text=message.body, read=True)


@router.get("/{patient_id}/banner", response_model=BannerOut)
async def banner(patient_id: UUID, session: AsyncSession = Depends(get_session)):
    """Напоминает о любом незавершённом маршруте, включая визит через два месяца."""
    await require_patient(session, patient_id)
    from app.services.banners import unfinished_banner

    return BannerOut(**await unfinished_banner(session, patient_id))


@router.get("/{patient_id}/route", response_model=PatientRouteOut | None)
async def patient_route(patient_id: UUID, session: AsyncSession = Depends(get_session)):
    """Показывает последний открытый маршрут, а при его отсутствии — последний завершённый."""
    await require_patient(session, patient_id)
    route = await session.scalar(
        open_routes(patient_id).order_by(Route.created_at.desc(), Route.id).limit(1)
    )
    if route is None:
        route = await session.scalar(
            select(Route)
            .where(Route.patient_id == patient_id)
            .order_by(Route.created_at.desc(), Route.id)
            .limit(1)
        )
    if route is None:
        return None
    status = RouteStatus(route.status).value
    happened, action = STAGE_TEXT[status]
    steps = (
        await session.scalars(
            select(RouteStep).where(RouteStep.route_id == route.id).order_by(RouteStep.step_no)
        )
    ).all()
    next_at = None
    if route.closed_at is None and status not in TERMINAL_STATUSES:
        if status == "booked":
            appointment = await session.scalar(
                select(Appointment)
                .where(
                    Appointment.route_id == route.id,
                    Appointment.visit_status == "booked",
                )
                .order_by(Appointment.starts_at.desc())
                .limit(1)
            )
            next_at = appointment.starts_at if appointment else None
        elif status == "hospitalization_scheduled":
            stay = await session.scalar(
                select(Hospitalization)
                .where(
                    Hospitalization.route_id == route.id,
                    Hospitalization.status == "scheduled",
                )
                .order_by(Hospitalization.created_at.desc())
                .limit(1)
            )
            next_at = stay.scheduled_date if stay else None
        elif status == "followup_scheduled":
            followup = await session.scalar(
                select(Followup)
                .where(
                    Followup.route_id == route.id,
                    Followup.status.in_(["scheduled", "confirmed"]),
                )
                .order_by(Followup.created_at.desc())
                .limit(1)
            )
            next_at = followup.target_date if followup else None
        else:
            next_at = steps[-1].due_at if steps and steps[-1].due_at else route.target_date
    return PatientRouteOut(
        route_id=route.id,
        what_happened=happened,
        what_to_do=action,
        next_step_at=next_at,
        next_step_text=(
            "Рекомендованный срок следующего шага."
            if next_at
            else "Следующих шагов нет."
            if status in TERMINAL_STATUSES or route.closed_at
            else "Дата пока не назначена. Уточните её в клинике."
        ),
        history=[
            StepOut(
                date=step.entered_at,
                text=STAGE_TEXT.get(step.status, ("План лечения обновлён.", ""))[0],
            )
            for step in steps
        ],
    )


class BannerResponseIn(BaseModel):
    """Ответ пациента на напоминание о незавершённом маршруте."""

    action: Literal["already_attended", "wants_booking"]


@router.post("/{patient_id}/banner/{route_id}/response")
async def respond_to_banner(
    patient_id: UUID,
    route_id: UUID,
    answer: BannerResponseIn,
    session: AsyncSession = Depends(get_session),
):
    """Записать ответ пациента, проверив принадлежность маршрута."""
    from app.services.routing import RoutingService, RoutingTransitionError
    from app.services.timers import TimerEngine

    await require_patient(session, patient_id)
    route = await session.get(Route, route_id)
    if route is None or route.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="Маршрут не найден")
    target = "closed_by_patient" if answer.action == "already_attended" else "booked"
    try:
        await RoutingService().transition(
            session, route_id, target, "patient", "Ответ пациента на баннер"
        )
    except RoutingTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await TimerEngine().cancel_for_route(session, route_id)
    await session.commit()
    return {"route_id": str(route_id), "status": target}

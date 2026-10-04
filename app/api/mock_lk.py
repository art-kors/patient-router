"""Кабинет с серверной проверкой доступа и данными из таблиц проекта."""

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clock import get_clock
from app.db import get_session
from app.models import (
    Appointment,
    AuditLog,
    Clinic,
    Doctor,
    Followup,
    Hospitalization,
    Notification,
    Patient,
    Route,
    RouteStatus,
    RouteStep,
    Specialty,
    Study,
    Task,
    TriggerMatch,
)
from app.services.routing import TERMINAL_STATUSES

router = APIRouter(prefix="/api/v1/mock/lk", tags=["mock-lk"])

# Формулировки описывают следующий шаг без внутренних кодов и медицинских сокращений.
STAGE_TEXT = {
    "created": ("По результатам УЗИ рекомендована консультация.", "Запишитесь к специалисту."),
    "notified": ("Вам отправлено приглашение на консультацию.", "Запишитесь к специалисту."),
    "awaiting_booking": ("Ждём записи на приём.", "Свяжитесь с клиникой для записи."),
    "booked": ("Вы записаны на консультацию.", "Приходите на приём в назначенное время."),
    "visit_done": ("Консультация состоялась.", "Дождитесь рекомендаций врача."),
    "decision_pending": ("Врач уточняет дальнейшие рекомендации.", "Дождитесь решения врача."),
    "booking_required": ("Нужна новая запись на приём.", "Свяжитесь с клиникой для записи."),
    "no_show": ("Приём не состоялся.", "Свяжитесь с клиникой и выберите новую дату."),
    "referred": (
        "Показана консультация по обращению в стационар.",
        "Согласуйте дату с координатором.",
    ),
    "hospitalization_scheduled": (
        "Дата поступления в стационар назначена.",
        "Подготовьтесь по рекомендациям врача.",
    ),
    "hospitalized": ("Вы поступили в стационар.", "Следуйте рекомендациям врача."),
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
    "closed_no_operation": (
        "Врач завершил этот план дальнейших действий.",
        "Следуйте рекомендациям врача.",
    ),
    "cancelled": (
        "Этот план дальнейших действий отменён.",
        "Уточните актуальные рекомендации в клинике.",
    ),
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
            text=patient_text(message.body),
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
        MessageOut(
            id=item.id,
            date=item.sent_at,
            text=patient_text(item.body),
            read=str(item.id) in read_ids,
        )
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
    return MessageOut(
        id=message.id, date=message.sent_at, text=patient_text(message.body), read=True
    )


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
                text=STAGE_TEXT.get(step.status, ("План дальнейших действий обновлён.", ""))[0],
            )
            for step in steps
        ],
    )


class BannerResponseIn(BaseModel):
    """Ответ пациента на напоминание о незавершённом маршруте."""

    action: Literal[
        "already_attended", "wants_booking", "confirmed_booking", "cannot_attend", "question"
    ]


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
    if answer.action in {"confirmed_booking", "cannot_attend", "question", "wants_booking"}:
        # Отклик не заменяет факт записи из МИС и не отменяет существующий приём.
        if route.closed_at or route.status in TERMINAL_STATUSES:
            raise HTTPException(409, "Этот маршрут завершён. Свяжитесь с клиникой.")
        if answer.action == "confirmed_booking":
            booked = await session.scalar(
                select(Appointment.id).where(
                    Appointment.route_id == route.id, Appointment.visit_status == "booked"
                )
            )
            if booked is None:
                raise HTTPException(409, "Подтверждённой записи пока нет. Свяжитесь с клиникой.")
        now = get_clock().now()
        session.add(
            AuditLog(
                route_id=route.id,
                actor="patient",
                action="patient_banner_response",
                details={"answer": answer.action},
                created_at=now,
            )
        )
        if answer.action != "confirmed_booking":
            session.add(
                Task(
                    route_id=route.id,
                    task_type="patient_question",
                    assignee_role="coordinator",
                    priority=2,
                    due_at=now,
                    status="open",
                    created_at=now,
                )
            )
        await session.commit()
        return {"message": "Ответ сохранён. При необходимости клиника свяжется с вами."}
    target = "closed_by_patient"
    try:
        await RoutingService().transition(
            session, route_id, target, "patient", "Ответ пациента на баннер"
        )
    except RoutingTransitionError as exc:
        raise HTTPException(
            status_code=409, detail="На этом этапе ответ недоступен. Свяжитесь с клиникой."
        ) from exc
    await TimerEngine().cancel_for_route(session, route_id)
    await session.commit()
    return {"route_id": str(route_id), "status": target}


def patient_text(value):
    """Скрывает недопустимые формулировки только в пациентском представлении."""
    text = re.sub(
        r"(?<![а-яё])(?:диагноз|лечени)[а-яё]*",
        "[формулировка скрыта]",
        str(value or ""),
        flags=re.I,
    )
    text = re.sub(r"(?:история|карточка) пациента", "мои исследования", text, flags=re.I)
    return text


async def cabinet_routes(session, patient_id):
    """Загружает все маршруты и связанные факты без ленивых запросов ORM."""
    return (
        await session.scalars(
            select(Route)
            .where(Route.patient_id == patient_id)
            .options(
                selectinload(Route.clinic),
                selectinload(Route.steps),
                selectinload(Route.appointments),
                selectinload(Route.followups),
                selectinload(Route.hospitalizations).selectinload(Hospitalization.surgeries),
                selectinload(Route.trigger_match).selectinload(TriggerMatch.finding),
                selectinload(Route.trigger_match).selectinload(TriggerMatch.protocol),
            )
            .order_by(Route.created_at.desc(), Route.id)
        )
    ).all()


async def route_view(session, route):
    """Представляет этап и фактические даты без служебных кодов."""
    specialty = await session.get(Specialty, route.specialty_id) if route.specialty_id else None
    match = route.trigger_match
    finding = match.finding if match else None
    completed = bool(route.closed_at or route.status in TERMINAL_STATUSES)
    happened, action = STAGE_TEXT.get(
        route.status, ("Рекомендации обновляются.", "Свяжитесь с клиникой.")
    )
    booked = [a for a in route.appointments if a.visit_status == "booked"]
    if route.status == "booked" and not booked:
        happened, action = (
            "Запись в медицинской системе пока не подтверждена.",
            "Свяжитесь с клиникой для записи.",
        )
    return {
        "route_id": str(route.id),
        "completed": completed,
        "finding": patient_text(finding.finding)
        if finding
        else "Рекомендация по результатам исследования",
        "specialty": patient_text(specialty.name)
        if specialty
        else "Уточните специалиста в клинике",
        "clinic": patient_text(route.clinic.name)
        if route.clinic
        else "Уточните клинику у координатора",
        "deadline": route.target_date,
        "stage": happened,
        "what_to_do": action,
        "what_next": {
            "created": "После записи клиника передаст дату и сведения о приёме.",
            "notified": "После записи клиника передаст дату и сведения о приёме.",
            "awaiting_booking": "После подтверждения клиникой запись появится в «Моих записях».",
            "booking_required": "После подтверждения новой записи здесь появится дата приёма.",
            "no_show": "После согласования новой даты клиника обновит вашу запись.",
            "booked": "На консультации врач обсудит результаты исследования и следующие шаги.",
            "visit_done": "Врач уточнит рекомендации, и они появятся здесь.",
            "decision_pending": "После решения врача здесь появятся дальнейшие рекомендации.",
            "referred": "Координатор согласует поступление; дату операции уточнит врач.",
            "hospitalization_scheduled": "При поступлении врач обсудит дальнейшие шаги.",
            "discharged": "После согласования контроля проверьте дату в разделе «Мои записи».",
            "followup_scheduled": "На контроле врач проверит результаты и обновит рекомендации.",
        }.get(route.status, "После следующего шага здесь появятся обновлённые рекомендации.")
        if not completed
        else "План завершён. При новых вопросах свяжитесь с клиникой.",
        "can_confirm": bool(booked) and not completed,
        "steps": [
            {
                "date": step.entered_at,
                "text": STAGE_TEXT.get(step.status, ("Рекомендации обновлены.", ""))[0],
            }
            for step in sorted(route.steps, key=lambda step: step.step_no)
        ],
    }


@router.get("/{patient_id}/cabinet")
async def cabinet(patient_id: UUID, session: AsyncSession = Depends(get_session)):
    """Выдаёт все разделы своего кабинета; права проверяет зависимость роутера."""
    await require_patient(session, patient_id)
    routes = await cabinet_routes(session, patient_id)
    views = [await route_view(session, route) for route in routes]
    studies = (
        await session.scalars(
            select(Study)
            .where(Study.patient_id == patient_id)
            .options(selectinload(Study.protocol), selectinload(Study.findings))
            .order_by(Study.study_date.desc(), Study.created_at.desc(), Study.id)
        )
    ).all()
    protocols = []
    for study in studies:
        linked = [
            views[i]
            for i, route in enumerate(routes)
            if route.trigger_match
            and route.trigger_match.protocol
            and route.trigger_match.protocol.study_id == study.id
        ]
        findings = [patient_text(f.finding) for f in study.findings if not f.in_negative_ctx]
        protocol = study.protocol
        protocols.append(
            {
                "date": study.study_date,
                "type": patient_text(study.study_type),
                "findings": findings,
                "meaning": "Протокол отменён. Уточните актуальные результаты в клинике."
                if protocol and protocol.is_cancelled
                else "Находки требуют внимания: показана консультация по рекомендациям ниже."
                if linked
                else "Автоматические рекомендации отсутствуют. Обсудите результаты с врачом."
                if protocol
                else (
                    "Протокол ещё обрабатывается. Если результат не появился, обратитесь в клинику."
                ),
                "recommendations": [
                    {"specialty": v["specialty"], "clinic": v["clinic"], "deadline": v["deadline"]}
                    for v in linked
                ],
            }
        )
    appointments = []
    for route in routes:
        specialty = await session.get(Specialty, route.specialty_id) if route.specialty_id else None
        who = patient_text(specialty.name) if specialty else "Уточните специалиста в клинике"
        where = patient_text(route.clinic.name) if route.clinic else "Уточните место в клинике"
        for appointment in route.appointments:
            doctor = (
                await session.get(Doctor, appointment.doctor_id) if appointment.doctor_id else None
            )
            clinic = (
                await session.get(Clinic, appointment.clinic_id) if appointment.clinic_id else None
            )
            appointments.append(
                {
                    "date": appointment.starts_at,
                    "type": "Приём",
                    "where": patient_text(clinic.name) if clinic else where,
                    "who": patient_text(doctor.full_name) if doctor else who,
                    "state": {
                        "booked": "Запись подтверждена",
                        "completed": "Приём состоялся",
                        "cancelled": "Запись отменена",
                        "no_show": "Приём не состоялся",
                    }.get(appointment.visit_status, "Уточните запись в клинике"),
                }
            )
        for followup in route.followups:
            appointments.append(
                {
                    "date": followup.target_date,
                    "type": "Контроль",
                    "where": where,
                    "who": who,
                    "state": {
                        "scheduled": "Рекомендованный срок контроля; запись ещё не подтверждена",
                        "confirmed": "Контроль подтверждён",
                        "completed": "Контроль пройден",
                        "missed": "Контроль пропущен",
                    }.get(followup.status, "Уточните запись в клинике"),
                }
            )
        for stay in route.hospitalizations:
            appointments.append(
                {
                    "date": stay.scheduled_date or stay.actual_date,
                    "type": "Обращение в стационар",
                    "where": where,
                    "who": who,
                    "state": {
                        "referred": "Нужно согласовать дату",
                        "scheduled": "Поступление запланировано; дату операции уточните у врача",
                        "admitted": "Поступление состоялось",
                        "discharged": "Вы выписаны",
                        "cancelled": "Поступление отменено",
                    }.get(stay.status, "Уточните дату в клинике"),
                }
            )
            for surgery in stay.surgeries:
                appointments.append(
                    {
                        "date": surgery.performed_at,
                        "type": "Операция",
                        "where": where,
                        "who": "Уточните врача в клинике",
                        "state": "Операция выполнена",
                    }
                )
    appointments.sort(key=lambda item: str(item["date"] or ""), reverse=True)
    return {"protocols": protocols, "routes": views, "appointments": appointments}

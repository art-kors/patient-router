"""Единая транзакционная точка обработки событий МИС."""

from dataclasses import asdict
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from app.clock import get_clock
from app.models import (
    Appointment,
    AuditLog,
    CloseReason,
    Finding,
    Followup,
    Hospitalization,
    MisEvent,
    Protocol,
    Route,
    RouteStatus,
    Study,
    Surgery,
    TriggerDef,
    TriggerMatch,
)
from app.services.decision.engine import DecisionEngine
from app.services.extraction import get_extractor
from app.services.routing import (
    RoutingService,
    RoutingTransitionError,
    TacticsRequiredError,
)
from app.services.timers import TimerEngine

# Актор боевых сервисов: все изменения приходят из МИС, а не от врача вручную.
ACTOR = "mis"

EVENT_TYPES = {
    "StudyProtocolSigned": (
        "Извлечь находки, проверить триггеры и создать маршрут; экстренные находки эскалировать."
    ),
    "StudyProtocolCorrected": (
        "Пересчитать триггеры без дублей; закрыть маршрут при отзыве триггера."
    ),
    "StudyProtocolCancelled": "Закрыть маршрут и отменить таймеры отменённого протокола.",
    "AppointmentBooked": "Записать приём и перевести маршрут в статус записи.",
    "AppointmentCancelled": "Отменить приём и вернуть маршрут к ожиданию записи.",
    "VisitCompleted": "Завершить приём и ожидать выбора тактики.",
    "VisitNoShow": "Зафиксировать неявку на приём.",
    "TacticsChosen": "Применить выбранную врачом тактику.",
    "HospitalizationScheduled": "Записать плановую госпитализацию.",
    "HospitalizationFactual": "Зафиксировать фактическую госпитализацию.",
    "SurgeryPerformed": "Записать выполненную операцию.",
    "Discharged": "Зафиксировать выписку и запланировать контрольный визит.",
}


def _uuid(value):
    return UUID(str(value)) if value else None


def _datetime(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def _date(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


class MisEventHandler:
    def __init__(self):
        self.routing = RoutingService()
        self.timers = TimerEngine()
        self.handlers = {name: getattr(self, f"_handle_{name}") for name in EVENT_TYPES}

    async def handle(self, session, event: dict) -> dict:
        """Обработать событие; вызывающий код фиксирует или откатывает транзакцию целиком."""
        event_id = event["event_id"]
        if await session.scalar(select(MisEvent).where(MisEvent.event_id == event_id)):
            return {"duplicate": True, "event_id": event_id}
        subject = event.get("subject", {})
        occurred = _datetime(event.get("occurred_at")) or get_clock().now()
        row = MisEvent(
            id=uuid4(),
            event_id=event_id,
            event_type=event["event_type"],
            occurred_at=occurred,
            payload=event.get("payload", {}),
            created_at=get_clock().now(),
        )
        # Вложенная транзакция защищает от конкурентной повторной доставки.
        # Связи проставляются после обработки: новое исследование ещё не существует.
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            if await session.scalar(select(MisEvent).where(MisEvent.event_id == event_id)):
                return {"duplicate": True, "event_id": event_id}
            raise
        # Сериализуем изменения клинических сущностей между разными событиями.
        # Блокировка PostgreSQL освобождается при завершении транзакции.
        await session.execute(text("SELECT pg_advisory_xact_lock(74120326)"))
        result = {
            "event_id": event_id,
            "status": "ignored",
            "duplicate": False,
            "route_id": None,
            "actions": [],
        }
        handler = self.handlers.get(event["event_type"])
        if handler:
            await handler(session, event, result)
        row.patient_id = _uuid(subject.get("patient_id"))
        row.study_id = _uuid(subject.get("study_id"))
        row.processed_at = get_clock().now()
        session.add(
            AuditLog(
                actor="mis",
                action="mis_event_processed",
                route_id=_uuid(result["route_id"]),
                study_id=row.study_id,
                details=result.copy(),
                created_at=get_clock().now(),
            )
        )
        await session.flush()
        return result

    async def _transition(self, session, route, target, result, reason=None):
        """Перевести маршрут боевым автоматом; невозможный переход не роняет обработку.

        Возвращает маршрут в новом статусе либо None, если автомат отклонил переход.
        """
        before = str(RouteStatus(route.status).value)
        after = str(RouteStatus(target).value)
        basis = f"Событие МИС: {reason if reason else after}"
        try:
            if reason:
                moved = await self.routing.close(session, route.id, reason, ACTOR, basis)
            else:
                moved = await self.routing.transition(session, route.id, target, ACTOR, basis)
            result["actions"].append(f"transition: {before}→{after}")
            return moved
        except (RoutingTransitionError, ValueError):
            result["actions"].append(f"transition_rejected: {before}→{after}")
            return None

    async def _route(self, session, event, result):
        subject = event.get("subject", {})
        route = None
        if subject.get("route_id"):
            route = await session.scalar(
                select(Route).where(Route.id == _uuid(subject["route_id"])).with_for_update()
            )
        if route is None:
            result["actions"].append("route_not_found")
            return None
        if subject.get("patient_id") and route.patient_id != _uuid(subject["patient_id"]):
            result["actions"].append("route_patient_mismatch")
            return None
        result.update(status="processed", route_id=str(route.id))
        return route

    async def _protocol_routes(self, session, study_id):
        return list(
            (
                await session.scalars(
                    select(Route)
                    .join(TriggerMatch)
                    .join(Protocol)
                    .where(Protocol.study_id == study_id)
                    .with_for_update()
                )
            ).all()
        )

    async def _analyze(self, session, event, result, corrected=False):
        subject, payload = event.get("subject", {}), event.get("payload", {})
        study_id, patient_id = _uuid(subject.get("study_id")), _uuid(subject.get("patient_id"))
        if not study_id or not patient_id:
            result["actions"].append("missing_study_or_patient")
            return
        study = await session.scalar(select(Study).where(Study.id == study_id).with_for_update())
        if study is not None and study.patient_id != patient_id:
            result["actions"].append("study_patient_mismatch")
            return
        raw = payload.get("text", payload.get("raw_text", study.raw_text if study else ""))
        extraction = get_extractor().extract(
            raw, study_type=payload.get("study_type", study.study_type if study else None)
        )
        decision = DecisionEngine().decide(
            extraction.findings,
            study_type=extraction.meta.study_type,
            conclusion_text=extraction.conclusion_text,
        )
        if study is None:
            study = Study(
                id=study_id,
                patient_id=patient_id,
                raw_text=raw,
                study_type=payload.get("study_type") or extraction.meta.study_type or "Не указан",
                study_date=_date(payload.get("study_date")) or get_clock().now().date(),
                created_at=get_clock().now(),
            )
            session.add(study)
            await session.flush()
        study.raw_text, study.status = raw, "processed"
        protocol = await session.scalar(select(Protocol).where(Protocol.study_id == study_id))
        if protocol is None:
            protocol = Protocol(id=uuid4(), study_id=study_id, created_at=get_clock().now())
            session.add(protocol)
        protocol.signed_at = _datetime(event.get("occurred_at")) or get_clock().now()
        protocol.is_corrected, protocol.is_cancelled = corrected, False
        protocol.facts = {"findings": [asdict(f) for f in extraction.findings]}
        await session.flush()
        # Сохраняем цитаты и обновляем срабатывания на том же протоколе.
        await session.execute(
            update(TriggerMatch)
            .where(TriggerMatch.protocol_id == protocol.id)
            .values(finding_id=None, fired=False, suppressed=False, suppression_reason=None)
        )
        await session.execute(delete(Finding).where(Finding.study_id == study_id))
        findings = []
        for fact in extraction.findings:
            values = asdict(fact)
            values["in_negative_ctx"] = values.pop("in_negative_context")
            finding = Finding(id=uuid4(), study_id=study_id, created_at=get_clock().now(), **values)
            session.add(finding)
            findings.append(finding)
        await session.flush()
        matches = {}
        for match in decision.matches:
            definition = await session.scalar(
                select(TriggerDef).where(TriggerDef.trigger_id == match.trigger.trigger_id)
            )
            if definition is None:
                t = match.trigger
                definition = TriggerDef(
                    trigger_id=t.trigger_id,
                    display_name=t.display_name,
                    source_study=t.source_study,
                    synonyms=list(t.synonyms),
                    negative_contexts=list(t.negative_contexts),
                    thresholds=t.thresholds,
                    potential_route=t.potential_route,
                    target_sla_days=t.target_sla_days,
                    department=t.department,
                    priority=t.priority,
                    emergency_flag=t.emergency_flag,
                    version=t.version,
                    created_at=get_clock().now(),
                    updated_at=get_clock().now(),
                )
                session.add(definition)
                await session.flush()
            stored = await session.scalar(
                select(TriggerMatch).where(
                    TriggerMatch.protocol_id == protocol.id,
                    TriggerMatch.trigger_def_id == definition.id,
                )
            )
            if stored is None:
                stored = TriggerMatch(
                    id=uuid4(),
                    protocol_id=protocol.id,
                    trigger_def_id=definition.id,
                    created_at=get_clock().now(),
                )
                session.add(stored)
            stored.fired, stored.suppressed = match.fired, match.suppressed
            stored.suppression_reason = match.suppression_reason if match.suppressed else None
            stored.applied_rule, stored.explanation = match.applied_rule, match.explanation
            stored.confidence = match.confidence
            stored.finding_id = next((f.id for f in findings if f.quote == match.quote), None)
            matches[match.trigger.trigger_id] = (stored, definition)
        await session.flush()
        result["status"] = "processed"
        result["actions"].append(f"extracted: {len(findings)} findings")
        result["actions"].extend(f"trigger_fired: {m.trigger.trigger_id}" for m in decision.fired)
        routes = await self._protocol_routes(session, study_id)
        fired_ids = {matches[m.trigger.trigger_id][0].id for m in decision.fired}
        for route in routes:
            if (
                corrected
                and (route.trigger_match_id not in fired_ids or decision.is_emergency)
                and not route.closed_at
            ):
                await self._transition(
                    session, route, "cancelled", result, CloseReason.TRIGGER_WITHDRAWN
                )
                await self.timers.cancel_for_route(session, route.id)
                result["route_id"] = str(route.id)
        if decision.is_emergency:
            result["is_emergency"] = True
            result["actions"].append("escalate_emergency")
            return
        if decision.winner:
            stored, definition = matches[decision.winner.trigger.trigger_id]
            route = next((r for r in routes if r.trigger_match_id == stored.id), None)
            if route is None:
                try:
                    route = await self.routing.create_from_match(
                        session, patient_id, stored, study_id
                    )
                except ValueError:
                    # Экстренное или подавленное срабатывание не допускает авто-маршрута.
                    result["actions"].append("route_not_created")
                    return
                route.specialty_id = definition.specialty_id
                result["actions"].append(f"route_created: {route.id}")
                if definition.target_sla_days:
                    await self.timers.schedule_route(session, route, definition.target_sla_days)
            result["route_id"] = str(route.id)

    async def _handle_StudyProtocolSigned(self, session, event, result):
        await self._analyze(session, event, result)

    async def _handle_StudyProtocolCorrected(self, session, event, result):
        await self._analyze(session, event, result, corrected=True)

    async def _handle_StudyProtocolCancelled(self, session, event, result):
        study_id = _uuid(event.get("subject", {}).get("study_id"))
        protocol = await session.scalar(select(Protocol).where(Protocol.study_id == study_id))
        if protocol:
            protocol.is_cancelled = True
            result["status"] = "processed"
            for route in await self._protocol_routes(session, study_id):
                result["route_id"] = str(route.id)
                await self._transition(
                    session, route, "cancelled", result, CloseReason.PROTOCOL_CANCELLED
                )
                await self.timers.cancel_for_route(session, route.id)

    async def _appointment(self, session, event, result, status, target):
        route = await self._route(session, event, result)
        if route is None:
            return
        payload = event.get("payload", {})
        query = select(Appointment).where(Appointment.route_id == route.id)
        if payload.get("appointment_id"):
            query = query.where(Appointment.id == _uuid(payload["appointment_id"]))
        elif payload.get("slot_ref"):
            query = query.where(Appointment.slot_ref == payload["slot_ref"])
        appointment = await session.scalar(query.order_by(Appointment.created_at.desc()).limit(1))
        if status == "booked" and appointment is None:
            appointment = Appointment(
                id=_uuid(payload.get("appointment_id")) or uuid4(),
                route_id=route.id,
                starts_at=_datetime(payload.get("starts_at"))
                or _datetime(event.get("occurred_at"))
                or get_clock().now(),
                doctor_id=payload.get("doctor_id"),
                clinic_id=payload.get("clinic_id"),
                slot_ref=payload.get("slot_ref"),
                is_online=payload.get("is_online", False),
                created_at=get_clock().now(),
            )
            session.add(appointment)
        if appointment:
            appointment.visit_status = status
        moved = await self._transition(session, route, target, result)
        if moved is not None and moved.status == RouteStatus.VISIT_DONE:
            await self._transition(session, moved, "decision_pending", result)

    async def _handle_AppointmentBooked(self, session, event, result):
        await self._appointment(session, event, result, "booked", "booked")

    async def _handle_AppointmentCancelled(self, session, event, result):
        await self._appointment(session, event, result, "cancelled", "awaiting_booking")

    async def _handle_VisitCompleted(self, session, event, result):
        await self._appointment(session, event, result, "completed", "visit_done")

    async def _handle_VisitNoShow(self, session, event, result):
        await self._appointment(session, event, result, "no_show", "no_show")

    async def _handle_TacticsChosen(self, session, event, result):
        route = await self._route(session, event, result)
        if route is None:
            return
        before = str(RouteStatus(route.status).value)
        payload = event.get("payload", {})
        try:
            await self.routing.apply_tactics(
                session,
                route.id,
                payload.get("tactics"),
                payload.get("comment", ""),
                ACTOR,
            )
            result["actions"].append(f"tactics_applied: {payload.get('tactics')}")
            result["status"] = "processed"
        except TacticsRequiredError as exc:
            result["actions"].append(f"tactics_rejected: {exc}")
        except RoutingTransitionError as exc:
            result["actions"].append(f"transition_rejected: {before}→{exc}")
        except (ValueError, KeyError):
            result["actions"].append(f"tactics_rejected: {before}")

    async def _hospitalization(self, session, event, result, status, target):
        route = await self._route(session, event, result)
        if route is None:
            return
        payload = event.get("payload", {})
        query = select(Hospitalization).where(Hospitalization.route_id == route.id)
        if payload.get("hospitalization_id"):
            query = query.where(Hospitalization.id == _uuid(payload["hospitalization_id"]))
        hosp = await session.scalar(query.order_by(Hospitalization.created_at.desc()).limit(1))
        if hosp is None:
            hosp = Hospitalization(
                id=_uuid(payload.get("hospitalization_id")) or uuid4(),
                route_id=route.id,
                created_at=get_clock().now(),
            )
            session.add(hosp)
        hosp.status = status
        hosp.ward = payload.get("ward", hosp.ward)
        if status == "scheduled":
            hosp.scheduled_date = _date(payload.get("scheduled_date")) or get_clock().now().date()
        if status == "admitted":
            hosp.actual_date = _date(payload.get("actual_date")) or get_clock().now().date()
        if target == "operated":
            session.add(
                Surgery(
                    hospitalization_id=hosp.id,
                    service_code=payload.get("service_code", "unknown"),
                    performed_at=_datetime(payload.get("performed_at"))
                    or _datetime(event.get("occurred_at"))
                    or get_clock().now(),
                )
            )
        if status == "discharged":
            session.add(
                Followup(
                    route_id=route.id,
                    target_date=_date(payload.get("followup_date"))
                    or get_clock().now().date() + timedelta(days=30),
                    status="scheduled",
                    created_at=get_clock().now(),
                )
            )
        await self._transition(session, route, target, result)

    async def _handle_HospitalizationScheduled(self, session, event, result):
        await self._hospitalization(
            session, event, result, "scheduled", "hospitalization_scheduled"
        )

    async def _handle_HospitalizationFactual(self, session, event, result):
        await self._hospitalization(session, event, result, "admitted", "hospitalized")

    async def _handle_SurgeryPerformed(self, session, event, result):
        await self._hospitalization(session, event, result, "admitted", "operated")

    async def _handle_Discharged(self, session, event, result):
        await self._hospitalization(session, event, result, "discharged", "discharged")

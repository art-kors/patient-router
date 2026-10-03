"""ORM-модели: 20 таблиц из заметки 07 «Модель данных»."""

from __future__ import annotations

import enum
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


# ============================================================
# Группа 5 · Справочники
# ============================================================


class Specialty(Base):
    __tablename__ = "specialty"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_surgical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    triggers: Mapped[list[TriggerDef]] = relationship(back_populates="specialty")


class Clinic(Base):
    __tablename__ = "clinic"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)

    routes: Mapped[list[Route]] = relationship(back_populates="clinic")


class Doctor(Base):
    __tablename__ = "doctor"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    specialty_id: Mapped[int | None] = mapped_column(ForeignKey("specialty.id"))
    clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinic.id"))


# ============================================================
# Группа 1 · Данные исследования
# ============================================================


class Patient(Base):
    __tablename__ = "patient"
    __table_args__ = (CheckConstraint("sex IN ('M','F')", name="patient_sex_check"),)

    id: Mapped[UUID] = _uuid()
    external_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    anonymized_hash: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False
    )
    age: Mapped[int | None] = mapped_column(Integer)
    sex: Mapped[str | None] = mapped_column(String(1))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    studies: Mapped[list[Study]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    routes: Mapped[list[Route]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )


class StudyStatus(str, enum.Enum):
    RECEIVED = "received"
    EXTRACTED = "extracted"
    PROCESSED = "processed"
    FAILED = "failed"


class Study(Base):
    __tablename__ = "study"
    __table_args__ = (
        Index("idx_study_date", "study_date"),
        Index("idx_study_type", "study_type"),
    )

    id: Mapped[UUID] = _uuid()
    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("patient.id", ondelete="CASCADE"), nullable=False
    )
    study_type: Mapped[str] = mapped_column(String(255), nullable=False)
    study_date: Mapped[date] = mapped_column(Date, nullable=False)
    document_ref: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=StudyStatus.RECEIVED.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    patient: Mapped[Patient] = relationship(back_populates="studies")
    findings: Mapped[list[Finding]] = relationship(
        back_populates="study", cascade="all, delete-orphan"
    )
    protocol: Mapped[Protocol | None] = relationship(
        back_populates="study", cascade="all, delete-orphan"
    )


class Protocol(Base):
    __tablename__ = "protocol"
    __table_args__ = (Index("idx_protocol_study", "study_id"),)

    id: Mapped[UUID] = _uuid()
    study_id: Mapped[UUID] = mapped_column(
        ForeignKey("study.id", ondelete="CASCADE"), nullable=False
    )
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    doctor_id: Mapped[str | None] = mapped_column(String(64))
    facts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    study: Mapped[Study] = relationship(back_populates="protocol")
    trigger_matches: Mapped[list[TriggerMatch]] = relationship(
        back_populates="protocol", cascade="all, delete-orphan"
    )


class Finding(Base):
    __tablename__ = "finding"
    __table_args__ = (
        Index("idx_finding_study", "study_id"),
        Index("idx_finding_name", "finding"),
    )

    id: Mapped[UUID] = _uuid()
    study_id: Mapped[UUID] = mapped_column(
        ForeignKey("study.id", ondelete="CASCADE"), nullable=False
    )
    organ: Mapped[str | None] = mapped_column(String(128))
    finding: Mapped[str] = mapped_column(String(255), nullable=False)
    laterality: Mapped[str | None] = mapped_column(String(32))
    size_mm: Mapped[float | None] = mapped_column(Numeric(6, 1))
    classification: Mapped[str | None] = mapped_column(String(32))
    # ★ цитата-доказательство — без неё нет объяснимости
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    in_negative_ctx: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source_section: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    study: Mapped[Study] = relationship(back_populates="findings")
    trigger_matches: Mapped[list[TriggerMatch]] = relationship(back_populates="finding")


# ============================================================
# Группа 2 · Правила и срабатывания
# ============================================================

SUPPRESSION_REASONS = (
    "negative_context",
    "threshold_not_met",
    "study_type_mismatch",
    "low_confidence",
)


class TriggerDef(Base):
    __tablename__ = "trigger_def"
    __table_args__ = (
        CheckConstraint("priority BETWEEN 1 AND 4", name="trigger_def_priority_check"),
        Index("idx_trigger_def_specialty", "specialty_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_study: Mapped[str] = mapped_column(String(255), nullable=False)
    synonyms: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    negative_contexts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    thresholds: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    specialty_id: Mapped[int | None] = mapped_column(ForeignKey("specialty.id"))
    potential_route: Mapped[str | None] = mapped_column(Text)
    target_sla_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    department: Mapped[str | None] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # ★ экстренная находка: авто-маршрут блокируется на уровне кода
    emergency_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    specialty: Mapped[Specialty | None] = relationship(back_populates="triggers")
    matches: Mapped[list[TriggerMatch]] = relationship(back_populates="trigger_def")


class TriggerMatch(Base):
    __tablename__ = "trigger_match"
    __table_args__ = (
        # ★ защита от дублей при исправлении протокола
        UniqueConstraint(
            "protocol_id", "trigger_def_id", name="uq_trigger_match_protocol_trigger"
        ),
        Index("idx_trigger_match_protocol", "protocol_id"),
        Index("idx_trigger_match_fired", "fired", postgresql_where=text("fired")),
    )

    id: Mapped[UUID] = _uuid()
    protocol_id: Mapped[UUID] = mapped_column(
        ForeignKey("protocol.id", ondelete="CASCADE"), nullable=False
    )
    trigger_def_id: Mapped[int] = mapped_column(
        ForeignKey("trigger_def.id"), nullable=False
    )
    finding_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("finding.id", ondelete="SET NULL")
    )
    fired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # ★ подавленный триггер объясняется: требование кейса «почему не сработало»
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suppression_reason: Mapped[str | None] = mapped_column(
        String(32),
        CheckConstraint(
            "suppression_reason IS NULL OR suppression_reason IN "
            "('negative_context','threshold_not_met','study_type_mismatch','low_confidence')",
            name="trigger_match_suppression_reason_check",
        ),
    )
    applied_rule: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    explanation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    protocol: Mapped[Protocol] = relationship(back_populates="trigger_matches")
    trigger_def: Mapped[TriggerDef] = relationship(back_populates="matches")
    finding: Mapped[Finding | None] = relationship(back_populates="trigger_matches")
    route: Mapped[Route | None] = relationship(back_populates="trigger_match")


# ============================================================
# Сквозное · События МИС
# ============================================================


class MisEvent(Base):
    __tablename__ = "mis_event"
    __table_args__ = (
        # ★ единственный механизм идемпотентности на входе
        Index("uq_mis_event_event_id", "event_id", unique=True),
        Index("idx_mis_event_type_time", "event_type", "occurred_at"),
        Index(
            "idx_mis_event_unprocessed",
            "processed_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = _uuid()
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    patient_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("patient.id", ondelete="SET NULL")
    )
    study_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("study.id", ondelete="SET NULL")
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ============================================================
# Группа 3 · Маршрут
# ============================================================


class RouteStatus(str, enum.Enum):
    CREATED = "created"
    NOTIFIED = "notified"
    AWAITING_BOOKING = "awaiting_booking"
    BOOKED = "booked"
    VISIT_DONE = "visit_done"
    DECISION_PENDING = "decision_pending"
    BOOKING_REQUIRED = "booking_required"
    NO_SHOW = "no_show"
    REFERRED = "referred"
    HOSPITALIZATION_SCHEDULED = "hospitalization_scheduled"
    HOSPITALIZED = "hospitalized"
    OPERATED = "operated"
    DISCHARGED = "discharged"
    FOLLOWUP_SCHEDULED = "followup_scheduled"
    FOLLOWUP_DONE = "followup_done"
    ROUTE_NOT_REALIZED = "route_not_realized"
    CLOSED_BY_PATIENT = "closed_by_patient"
    CLOSED_NO_OPERATION = "closed_no_operation"
    CANCELLED = "cancelled"


class CloseReason(str, enum.Enum):
    NOT_REALIZED = "not_realized"
    PATIENT_REFUSED = "patient_refused"
    NO_OPERATION = "no_operation"
    PROTOCOL_CANCELLED = "protocol_cancelled"
    TRIGGER_WITHDRAWN = "trigger_withdrawn"
    OTHER = "other"


class Tactics(str, enum.Enum):
    SURGERY_INDICATED = "surgery_indicated"
    ADDITIONAL_EXAM = "additional_exam"
    OBSERVATION = "observation"
    NO_SURGERY = "no_surgery"
    PATIENT_REFUSED = "patient_refused"
    REFER_OTHER_SPECIALTY = "refer_other_specialty"


class TimerType(str, enum.Enum):
    NOTIFY_INITIAL = "notify_initial"
    NOTIFY_REMINDER = "notify_reminder"
    CREATE_TASK = "create_task"
    ESCALATE = "escalate"
    CLOSE_ROUTE = "close_route"
    SCHEDULE_FOLLOWUP = "schedule_followup"


class Route(Base):
    __tablename__ = "route"
    __table_args__ = (
        # ★ один маршрут на одно срабатывание
        UniqueConstraint("trigger_match_id", name="uq_route_trigger_match"),
        Index("idx_route_patient", "patient_id"),
        Index("idx_route_status", "status"),
    )

    id: Mapped[UUID] = _uuid()
    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("patient.id", ondelete="CASCADE"), nullable=False
    )
    trigger_match_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("trigger_match.id", ondelete="SET NULL")
    )
    specialty_id: Mapped[int | None] = mapped_column(ForeignKey("specialty.id"))
    clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinic.id"))
    status: Mapped[str] = mapped_column(
        SAEnum(RouteStatus, name="route_status", native_enum=False, length=32),
        default=RouteStatus.CREATED.value,
        nullable=False,
    )
    target_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(
        SAEnum(CloseReason, name="close_reason", native_enum=False, length=32)
    )

    patient: Mapped[Patient] = relationship(back_populates="routes")
    trigger_match: Mapped[TriggerMatch | None] = relationship(back_populates="route")
    clinic: Mapped[Clinic | None] = relationship(back_populates="routes")
    steps: Mapped[list[RouteStep]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )
    timers: Mapped[list[Timer]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )
    hospitalizations: Mapped[list[Hospitalization]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )
    followups: Mapped[list[Followup]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )


class RouteStep(Base):
    __tablename__ = "route_step"
    __table_args__ = (
        UniqueConstraint("route_id", "step_no", name="uq_route_step"),
        Index("idx_route_step_route", "route_id", "step_no"),
    )

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    route: Mapped[Route] = relationship(back_populates="steps")


class Timer(Base):
    __tablename__ = "timer"
    __table_args__ = (
        # ★ защита от двойного срабатывания и дублей при пересоздании
        UniqueConstraint(
            "route_id", "timer_type", "due_at", name="uq_timer_route_type_due"
        ),
        Index("idx_timer_due", "due_at", postgresql_where=text("NOT fired")),
        Index("idx_timer_route", "route_id"),
    )

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    timer_type: Mapped[str] = mapped_column(
        SAEnum(TimerType, name="timer_type", native_enum=False, length=32),
        nullable=False,
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str | None] = mapped_column(String(32))

    route: Mapped[Route] = relationship(back_populates="timers")


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open','in_progress','done','postponed','cancelled')",
            name="task_status_check",
        ),
        Index("idx_task_queue", "assignee_role", "status", "due_at"),
    )

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    assignee_role: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    route: Mapped[Route] = relationship(back_populates="tasks")


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('lk','push','sms','task')", name="notification_channel_check"
        ),
        CheckConstraint(
            "delivery_status IN ('sent','delivered','failed','suppressed')",
            name="notification_delivery_status_check",
        ),
        Index("idx_notification_route", "route_id", "sent_at"),
    )

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    template_code: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivery_status: Mapped[str] = mapped_column(
        String(32), default="sent", nullable=False
    )
    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    route: Mapped[Route] = relationship(back_populates="notifications")


# ============================================================
# Группа 4 · Клинический контур
# ============================================================


class Appointment(Base):
    __tablename__ = "appointment"
    __table_args__ = (
        CheckConstraint(
            "visit_status IN ('booked','completed','cancelled','no_show')",
            name="appointment_visit_status_check",
        ),
        Index("idx_appointment_doctor_time", "doctor_id", "starts_at"),
    )

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctor.id"))
    clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinic.id"))
    slot_ref: Mapped[str | None] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    visit_status: Mapped[str] = mapped_column(
        String(32), default="booked", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    route: Mapped[Route] = relationship(back_populates="appointments")


class Hospitalization(Base):
    __tablename__ = "hospitalization"
    __table_args__ = (
        CheckConstraint(
            "status IN ('referred','scheduled','admitted','discharged','cancelled')",
            name="hospitalization_status_check",
        ),
        Index("idx_hosp_route", "route_id"),
    )

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_date: Mapped[date | None] = mapped_column(Date)
    actual_date: Mapped[date | None] = mapped_column(Date)
    ward: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="referred", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    route: Mapped[Route] = relationship(back_populates="hospitalizations")
    surgeries: Mapped[list[Surgery]] = relationship(
        back_populates="hospitalization", cascade="all, delete-orphan"
    )


class Surgery(Base):
    __tablename__ = "surgery"
    __table_args__ = (Index("idx_surgery_performed", "performed_at"),)

    id: Mapped[UUID] = _uuid()
    hospitalization_id: Mapped[UUID] = mapped_column(
        ForeignKey("hospitalization.id", ondelete="CASCADE"), nullable=False
    )
    # операция подтягивается из фактически оказанных услуг
    service_code: Mapped[str] = mapped_column(String(64), nullable=False)
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    hospitalization: Mapped[Hospitalization] = relationship(back_populates="surgeries")


class Followup(Base):
    __tablename__ = "followup"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','confirmed','completed','missed')",
            name="followup_status_check",
        ),
        Index("idx_followup_target", "target_date"),
    )

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    route: Mapped[Route] = relationship(back_populates="followups")


# ============================================================
# Сквозное · Журнал действий
# ============================================================


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("idx_audit_route_time", "route_id", "created_at"),)

    id: Mapped[UUID] = _uuid()
    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE")
    )
    study_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("study.id", ondelete="CASCADE")
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    basis: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    route: Mapped[Route | None] = relationship(back_populates="audit_logs")


__all__ = [
    "Base",
    "Appointment",
    "AuditLog",
    "Clinic",
    "CloseReason",
    "Doctor",
    "Finding",
    "Followup",
    "Hospitalization",
    "MisEvent",
    "Notification",
    "Patient",
    "Protocol",
    "Route",
    "RouteStatus",
    "RouteStep",
    "Specialty",
    "Study",
    "StudyStatus",
    "Surgery",
    "Tactics",
    "Task",
    "Timer",
    "TimerType",
    "TriggerDef",
    "TriggerMatch",
]

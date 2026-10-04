"""Маршрут сохраняет последовательность клинических решений и их основания."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import get_clock
from app.models import (
    AuditLog,
    CloseReason,
    Protocol,
    Route,
    RouteStatus,
    RouteStep,
    Tactics,
    Timer,
    TimerType,
    TriggerDef,
    TriggerMatch,
)
from app.services.decision.engine import TriggerMatch as DecisionMatch


class RoutingTransitionError(Exception):
    """Не позволяет нарушить последовательность этапов маршрута."""


class TacticsRequiredError(ValueError, RoutingTransitionError):
    """Не позволяет продолжить маршрут без решения врача."""


TERMINAL_STATUSES = frozenset(
    {
        "followup_done",
        "route_not_realized",
        "closed_by_patient",
        "closed_no_operation",
        "cancelled",
    }
)
# Тактика обязательна только там, где маршрут закрывает решение врача.
# Из decision_pending закрытие равно выбору тактики: «операция не показана» и
# «пациент отказался» — это apply_tactics, а не пустой переход. Остальные
# закрытия тактики не требуют по смыслу: control без тактики (followup_done),
# недостижимый маршрут, отмена протокола и отзыв триггера — решения системы
# или пациента, а не врачебная тактика. Их запрещать нельзя.
TACTICS_REQUIRED_FROM = frozenset({"decision_pending"})
TRANSITIONS = {
    "created": {"notified", "cancelled"},
    "notified": {"awaiting_booking", "cancelled", "route_not_realized", "closed_by_patient"},
    "awaiting_booking": {"booked", "route_not_realized", "closed_by_patient", "cancelled"},
    "booked": {"visit_done", "booking_required", "no_show", "cancelled"},
    "visit_done": {"decision_pending"},
    "decision_pending": {
        "referred",
        "followup_scheduled",
        "closed_no_operation",
        "closed_by_patient",
    },
    "booking_required": {
        "awaiting_booking",
        "booked",
        "cancelled",
        "route_not_realized",
        "closed_by_patient",
    },
    "no_show": {
        "booking_required",
        "booked",
        "route_not_realized",
        "closed_by_patient",
        "cancelled",
    },
    "referred": {"hospitalization_scheduled", "closed_by_patient", "cancelled"},
    "hospitalization_scheduled": {"hospitalized", "referred", "closed_by_patient", "cancelled"},
    "hospitalized": {"operated", "closed_no_operation"},
    "operated": {"discharged"},
    "discharged": {"followup_scheduled"},
    "followup_scheduled": {"followup_done", "closed_by_patient"},
    **{status: set() for status in TERMINAL_STATUSES},
}
_CLOSE_REASONS = {
    "route_not_realized": CloseReason.NOT_REALIZED,
    "closed_by_patient": CloseReason.PATIENT_REFUSED,
    "closed_no_operation": CloseReason.NO_OPERATION,
    "cancelled": CloseReason.PROTOCOL_CANCELLED,
    "followup_done": CloseReason.OTHER,
}


def allowed_transitions(status: RouteStatus | str) -> set[str]:
    """Даёт вызывающему коду правила выбора следующего этапа без изменения матрицы."""
    return set(TRANSITIONS[RouteStatus(status).value])


async def _has_recorded_tactics(session: AsyncSession, route_id: UUID) -> bool:
    """Проверить, что тактика врача уже записана в аудите маршрута.

    Отдельной колонки у маршрута нет: выбор тактики живёт в ``audit_log.details``,
    как и обещает ``docs/api.md``. Поэтому ищем по ключу, а не по тексту записи.
    """
    return (
        await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.route_id == route_id,
                AuditLog.details["tactics"].as_string().is_not(None),
            )
        )
        or 0
    ) > 0


class RoutingService:
    """Обеспечивает согласованность маршрута, истории и аудита в транзакции вызывающего кода."""

    async def create_from_match(
        self,
        session: AsyncSession,
        patient_id: UUID,
        trigger_match: TriggerMatch | DecisionMatch,
        study_id: UUID,
    ) -> Route:
        """Связывает срабатывание с маршрутом, чтобы находка получила срок обработки."""
        if isinstance(trigger_match, DecisionMatch):
            trigger = trigger_match.trigger
            if not trigger_match.fired or trigger_match.suppressed or trigger.emergency_flag:
                raise ValueError("Срабатывание не допускает автоматического маршрута")
            definition = (
                await session.execute(
                    select(TriggerDef).where(
                        TriggerDef.trigger_id == trigger.trigger_id,
                        TriggerDef.version == trigger.version,
                    )
                )
            ).scalar_one()
            protocol = (
                await session.execute(
                    select(Protocol).where(
                        Protocol.study_id == study_id,
                    )
                )
            ).scalar_one()
            match = TriggerMatch(
                protocol_id=protocol.id,
                trigger_def_id=definition.id,
                fired=True,
                suppressed=False,
                applied_rule=trigger_match.applied_rule,
                confidence=trigger_match.confidence,
                explanation=trigger_match.explanation,
            )
            session.add(match)
            await session.flush()
        else:
            match = trigger_match
            trigger = await session.get(TriggerDef, match.trigger_def_id)
            if trigger is None or not match.fired or match.suppressed or trigger.emergency_flag:
                raise ValueError("Срабатывание не допускает автоматического маршрута")
        now = get_clock().now()
        route = Route(
            patient_id=patient_id,
            trigger_match_id=match.id,
            status=RouteStatus.CREATED,
            target_date=(now + timedelta(days=trigger.target_sla_days)).date(),
            created_at=now,
        )
        session.add(route)
        await session.flush()
        session.add(
            AuditLog(
                route_id=route.id,
                study_id=study_id,
                actor="system",
                action="create_route",
                basis=match.applied_rule,
                created_at=now,
            )
        )
        await session.flush()
        return route

    async def transition(
        self,
        session: AsyncSession,
        route_id: UUID,
        to_status: RouteStatus | str,
        actor: str,
        basis: str,
        **fields,
    ) -> Route:
        """Сохраняет обоснованный переход вместе с историей, исключая обход клинических этапов."""
        # Блокировка маршрута сериализует переходы и защищает нумерацию шагов.
        route = (
            await session.execute(
                select(Route)
                .where(
                    Route.id == route_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if route is None:
            raise RoutingTransitionError("Маршрут не найден")
        if to_status not in allowed_transitions(route.status):
            raise RoutingTransitionError(f"Переход {route.status} → {to_status} невозможен")
        to_status = RouteStatus(to_status).value
        # Закрытие вместо решения врача: без записанной тактики оно невозможно.
        # Проверка внутри сервиса, а не в ручке, — иначе боевой путь через событие
        # МИС обошёл бы её и закрыл маршрут в обход требования кейса.
        if (
            str(RouteStatus(route.status).value) in TACTICS_REQUIRED_FROM
            and to_status in TERMINAL_STATUSES
            and not fields.get("tactics")
            and not await _has_recorded_tactics(session, route_id)
        ):
            raise TacticsRequiredError(
                "Закрыть маршрут без тактики нельзя: укажите решение врача "
                "в POST /api/v1/routes/{route_id}/tactics"
            )
        permitted_fields = {
            "target_date",
            "specialty_id",
            "clinic_id",
            "close_reason",
            "tactics",
            "comment",
        }
        if fields.keys() - permitted_fields:
            raise ValueError("Недопустимые поля перехода")
        if "close_reason" in fields:
            fields["close_reason"] = CloseReason(fields["close_reason"])
            if to_status not in TERMINAL_STATUSES:
                raise ValueError("Причина закрытия допустима только при завершении маршрута")
        now = get_clock().now()
        step_no = (
            await session.execute(
                select(func.max(RouteStep.step_no)).where(
                    RouteStep.route_id == route_id,
                )
            )
        ).scalar_one() or 0
        for name, value in fields.items():
            if name not in {"tactics", "comment"}:
                setattr(route, name, value)
        route.status = to_status
        if to_status in TERMINAL_STATUSES:
            route.closed_at = now
            route.close_reason = fields.get("close_reason", _CLOSE_REASONS[to_status])
        due_at = None
        if to_status == RouteStatus.DECISION_PENDING:
            # Через сутки отсутствие тактики должно попасть в очередь ответственного.
            due_at = now + timedelta(hours=24)
            session.add(
                Timer(
                    route_id=route_id, timer_type=TimerType.CREATE_TASK, due_at=due_at, fired=False
                )
            )
        session.add(
            RouteStep(
                route_id=route_id,
                step_no=step_no + 1,
                status=to_status,
                entered_at=now,
                due_at=due_at,
            )
        )
        session.add(
            AuditLog(
                route_id=route_id,
                actor=actor,
                action=f"transition:{to_status}",
                basis=basis,
                details={
                    key: str(value) if value is not None else None for key, value in fields.items()
                },
                created_at=now,
            )
        )
        await session.flush()
        return route

    async def close(
        self,
        session: AsyncSession,
        route_id: UUID,
        reason: CloseReason | str,
        actor: str,
        basis: str,
    ) -> Route:
        """Завершает маршрут с причиной, сохраняя ограничения текущего этапа."""
        reason = CloseReason(reason)
        status = {
            CloseReason.NOT_REALIZED: RouteStatus.ROUTE_NOT_REALIZED,
            CloseReason.PATIENT_REFUSED: RouteStatus.CLOSED_BY_PATIENT,
            CloseReason.NO_OPERATION: RouteStatus.CLOSED_NO_OPERATION,
        }.get(reason, RouteStatus.CANCELLED)
        return await self.transition(session, route_id, status, actor, basis, close_reason=reason)

    async def apply_tactics(
        self,
        session: AsyncSession,
        route_id: UUID,
        tactics: Tactics | None,
        comment: str,
        actor: str,
    ) -> Route:
        """Делает решение врача обязательным и сохраняет его объяснение в аудите."""
        if not tactics:
            raise TacticsRequiredError("Необходимо выбрать врачебную тактику")
        tactics = Tactics(tactics)
        status = RouteStatus.CLOSED_NO_OPERATION
        if tactics == Tactics.SURGERY_INDICATED:
            status = RouteStatus.REFERRED
        elif tactics == Tactics.PATIENT_REFUSED:
            status = RouteStatus.CLOSED_BY_PATIENT
        return await self.transition(
            session, route_id, status, actor, comment, tactics=tactics.value, comment=comment
        )

    async def get(self, session: AsyncSession, route_id: UUID) -> Route | None:
        """Позволяет найти маршрут для дальнейшего решения в общей транзакции."""
        return await session.get(Route, route_id)

    async def list(
        self,
        session: AsyncSession,
        status: RouteStatus | str | None = None,
        patient_id: UUID | None = None,
        overdue: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Route], int]:
        """Выделяет нужную очередь маршрутов и её размер для постраничного просмотра."""
        if limit < 0 or offset < 0:
            raise ValueError("Размер страницы и смещение не могут быть отрицательными")
        filters = []
        if status is not None:
            filters.append(Route.status == RouteStatus(status))
        if patient_id is not None:
            filters.append(Route.patient_id == patient_id)
        if overdue:
            filters.extend(
                (
                    Route.target_date < get_clock().now().date(),
                    Route.closed_at.is_(None),
                    Route.status.not_in(TERMINAL_STATUSES),
                )
            )
        total = (
            await session.execute(select(func.count()).select_from(Route).where(*filters))
        ).scalar_one()
        routes = (
            (
                await session.execute(
                    select(Route)
                    .where(*filters)
                    .order_by(
                        Route.created_at.desc(),
                        Route.id,
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(routes), total

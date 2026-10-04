"""Клиническое напоминание для пациента и формы нового приёма."""

from uuid import UUID

from sqlalchemy import or_, select

from app.clock import get_clock
from app.models import Protocol, Route, Study, TriggerDef, TriggerMatch
from app.services.routing import TERMINAL_STATUSES


async def unfinished_banner(session, patient_id: UUID) -> dict:
    """Сохранить видимость находки даже после исчерпания автоматических контактов."""
    routes = (
        await session.scalars(
            select(Route)
            .where(
                Route.patient_id == patient_id,
                or_(
                    (Route.closed_at.is_(None) & Route.status.not_in(TERMINAL_STATUSES)),
                    Route.status == "route_not_realized",
                ),
            )
            .order_by(Route.created_at, Route.id)
        )
    ).all()
    texts = []
    for route in routes:
        detail = await session.execute(
            select(Study.study_date, TriggerDef.display_name)
            .select_from(TriggerMatch)
            .join(Protocol, Protocol.id == TriggerMatch.protocol_id)
            .join(Study, Study.id == Protocol.study_id)
            .join(TriggerDef, TriggerDef.id == TriggerMatch.trigger_def_id)
            .where(TriggerMatch.id == route.trigger_match_id)
        )
        row = detail.first() if route.trigger_match_id else None
        detected, finding = row if row else (route.created_at.date(), "находка по результатам УЗИ")
        days = max(0, (get_clock().now().date() - detected).days)
        missing = (
            "Консультация профильного специалиста в системе не зафиксирована."
            if route.status
            in {
                "created",
                "notified",
                "awaiting_booking",
                "booked",
                "booking_required",
                "no_show",
                "route_not_realized",
            }
            else "Завершение рекомендованного лечения в системе не зафиксировано."
        )
        ending = (
            "день"
            if days % 10 == 1 and days % 100 != 11
            else ("дня" if days % 10 in {2, 3, 4} and days % 100 not in {12, 13, 14} else "дней")
        )
        description = (
            "УЗ-признаки полипа эндометрия"
            if finding.lower() == "полип эндометрия"
            else f"УЗ-признаки: {finding}"
        )
        texts.append(
            f"{detected:%d.%m.%Y} выявлены {description}. {missing} Прошло {days} {ending}."
        )
    return {
        "visible": bool(routes),
        "title": "Незавершённый клинический маршрут" if routes else None,
        "text": "\n".join(texts) if routes else None,
        "route_ids": [str(route.id) for route in routes],
    }

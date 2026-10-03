"""Проверки клинических ограничений без подключения к БД."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models import AuditLog, CloseReason, Route, RouteStatus, RouteStep, Tactics, Timer
from app.services.routing import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    RoutingService,
    RoutingTransitionError,
    TacticsRequiredError,
    allowed_transitions,
)


def session_for(route, step_no=0):
    """Изолирует правила перехода от хранения данных."""
    session = MagicMock()
    found = MagicMock()
    found.scalar_one_or_none.return_value = route
    number = MagicMock()
    number.scalar_one.return_value = step_no
    session.execute = AsyncMock(side_effect=[found, number])
    session.flush = AsyncMock()
    return session


def test_transition_matrix():
    assert len(RouteStatus) == 19
    assert set(TRANSITIONS) == {status.value for status in RouteStatus}
    for status in RouteStatus:
        targets = allowed_transitions(status)
        assert targets <= set(TRANSITIONS)
        assert bool(targets) == (status not in TERMINAL_STATUSES)


@pytest.mark.parametrize("status", ["created", "notified", "awaiting_booking", "booked"])
def test_early_cancellation(status):
    assert "cancelled" in allowed_transitions(status)


async def test_impossible_transition():
    route = Route(id=uuid4(), status="notified")
    session = session_for(route)
    with pytest.raises(RoutingTransitionError):
        await RoutingService().transition(session, route.id, "operated", "врач", "основание")
    assert route.status == "notified"
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
async def test_terminal_status_has_no_exit(status):
    route = Route(id=uuid4(), status=status)
    with pytest.raises(RoutingTransitionError):
        await RoutingService().transition(session_for(route), route.id, "created", "врач", "")
    assert allowed_transitions(status) == set()


@pytest.mark.parametrize("tactics", [None, ""])
async def test_tactics_required(tactics):
    session = MagicMock()
    with pytest.raises(TacticsRequiredError):
        await RoutingService().apply_tactics(session, uuid4(), tactics, "", "врач")
    session.execute.assert_not_called()


@pytest.mark.parametrize("tactics", list(Tactics))
async def test_apply_tactics(tactics, model_clock):
    route = Route(id=uuid4(), status="decision_pending")
    session = session_for(route, 3)
    result = await RoutingService().apply_tactics(
        session, route.id, tactics, "Решение врача", "врач"
    )
    expected = (
        "referred"
        if tactics == Tactics.SURGERY_INDICATED
        else "closed_by_patient"
        if tactics == Tactics.PATIENT_REFUSED
        else "closed_no_operation"
    )
    assert result.status == expected
    assert result.closed_at == (None if expected == "referred" else model_clock.now())
    added = [call.args[0] for call in session.add.call_args_list]
    step = next(item for item in added if isinstance(item, RouteStep))
    audit = next(item for item in added if isinstance(item, AuditLog))
    assert step.step_no == 4
    assert step.status == expected
    assert audit.action == f"transition:{expected}"
    assert audit.actor == "врач"
    assert audit.basis == "Решение врача"
    assert audit.details["tactics"] == tactics.value
    if expected == "closed_no_operation":
        assert result.close_reason == CloseReason.NO_OPERATION


async def test_decision_timer(model_clock):
    from datetime import timedelta

    route = Route(id=uuid4(), status="visit_done")
    session = session_for(route)
    await RoutingService().transition(session, route.id, "decision_pending", "врач", "Явка")
    timer = next(
        call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], Timer)
    )
    assert timer.due_at == model_clock.now() + timedelta(hours=24)
    assert timer.timer_type == "create_task"


async def test_close_respects_matrix(model_clock):
    route = Route(id=uuid4(), status="awaiting_booking")
    session = session_for(route)
    await RoutingService().close(session, route.id, CloseReason.PATIENT_REFUSED, "врач", "Отказ")
    assert route.status == "closed_by_patient"
    assert route.close_reason == CloseReason.PATIENT_REFUSED
    assert route.closed_at == model_clock.now()


async def test_create_from_match(model_clock):
    from datetime import timedelta

    from app.models import TriggerDef, TriggerMatch

    match = TriggerMatch(
        id=uuid4(), trigger_def_id=1, fired=True, suppressed=False, applied_rule="правило@v1"
    )
    trigger = TriggerDef(id=1, target_sla_days=7, emergency_flag=False)
    session = MagicMock()
    session.get = AsyncMock(return_value=trigger)
    session.flush = AsyncMock()
    patient_id, study_id = uuid4(), uuid4()
    route = await RoutingService().create_from_match(session, patient_id, match, study_id)
    assert route.status == "created"
    assert route.patient_id == patient_id
    assert route.trigger_match_id == match.id
    assert route.target_date == (model_clock.now() + timedelta(days=7)).date()
    assert route.specialty_id is None
    assert route.clinic_id is None
    audit = next(
        call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], AuditLog)
    )
    assert audit.study_id == study_id


async def test_emergency_match_cannot_create_route():
    from app.models import TriggerDef, TriggerMatch

    session = MagicMock()
    session.get = AsyncMock(return_value=TriggerDef(emergency_flag=True))
    match = TriggerMatch(trigger_def_id=1, fired=True, suppressed=False)
    with pytest.raises(ValueError):
        await RoutingService().create_from_match(session, uuid4(), match, uuid4())
    session.add.assert_not_called()


def test_allowed_transitions_returns_copy():
    allowed_transitions("created").clear()
    assert "notified" in allowed_transitions("created")

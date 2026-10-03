"""Проверки событий МИС без подключения к БД."""

import inspect
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Appointment,
    AuditLog,
    Finding,
    Followup,
    Hospitalization,
    MisEvent,
    Protocol,
    Route,
    RouteStatus,
    RouteStep,
    Study,
    Surgery,
    TriggerMatch,
)
from app.models import (
    TriggerDef as ModelTriggerDef,
)
from app.services.decision.engine import DecisionEngine
from app.services.decision.matrix import TriggerDef
from app.services.extraction.base import ExtractionResult
from app.services.extraction.base import Finding as ExtractedFinding
from app.services.mis import EVENT_TYPES, MisEventHandler
from app.services.routing import RoutingService, RoutingTransitionError
from app.services.timers import TimerEngine


@asynccontextmanager
async def nested():
    yield


class QueryResult:
    """Ответ БД в том виде, в каком его читают боевые сервисы."""

    def __init__(self, value=None, rows=(), rowcount=1):
        self._value = value
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def bind_session(session, objects):
    """Научить мок отвечать так же, как настоящая сессия для вызовов боевых сервисов.

    RoutingService и TimerEngine читают через execute(...).scalar_one_or_none(),
    session.get и session.scalars, а не через session.scalar.
    """

    def entity_of(query):
        descriptions = getattr(query, "column_descriptions", None)
        return descriptions[0]["entity"] if descriptions else None

    async def scalar(query):
        return objects.get(entity_of(query))

    async def scalars(query):
        entity = entity_of(query)
        if entity is None:
            return QueryResult(rows=[])
        return QueryResult(rows=[obj for obj in objects.values() if isinstance(obj, entity)])

    async def execute(query):
        entity = entity_of(query)
        if entity is Route:
            return QueryResult(value=objects.get(Route))
        if entity is RouteStep:
            step = objects.get(RouteStep)
            return QueryResult(value=step.step_no if step else 0)
        # update/delete/text: боевым сервисам нужен только rowcount.
        return QueryResult(rowcount=1)

    async def get(entity, pk):
        return objects.get(entity)

    session.scalar.side_effect = scalar
    session.scalars.side_effect = scalars
    session.execute.side_effect = execute
    session.get.side_effect = get
    return session


@pytest.fixture
def session():
    mock = MagicMock()
    mock.scalar = AsyncMock(return_value=None)
    mock.scalars = AsyncMock()
    mock.execute = AsyncMock()
    mock.get = AsyncMock()
    mock.flush = AsyncMock()
    mock.begin_nested.side_effect = nested
    return mock


def event(name, route=None, **payload):
    return {
        "event_id": str(uuid4()),
        "event_type": name,
        "subject": {"route_id": str(route.id)} if route else {},
        "payload": payload,
    }


async def test_все_12_типов_в_справочнике(client):
    response = await client.get("/api/v1/mis/event-types")
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 12
    assert {entry["event_type"] for entry in entries} == set(EVENT_TYPES)
    assert all(entry["description"] for entry in entries)


async def test_дубль_не_создаёт_дубль(session):
    handler = MisEventHandler()
    delivery = event("AppointmentBooked")
    session.scalar.return_value = MisEvent(event_id=delivery["event_id"])
    handler.handlers["AppointmentBooked"] = AsyncMock()
    assert await handler.handle(session, delivery) == {
        "duplicate": True,
        "event_id": delivery["event_id"],
    }
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    session.execute.assert_not_awaited()
    handler.handlers["AppointmentBooked"].assert_not_awaited()


async def test_конкурентный_дубль_не_обрабатывается(session):
    handler = MisEventHandler()
    delivery = event("AppointmentBooked")
    session.scalar.side_effect = [None, MisEvent(event_id=delivery["event_id"])]
    session.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    handler.handlers["AppointmentBooked"] = AsyncMock()
    result = await handler.handle(session, delivery)
    assert result["duplicate"] is True
    handler.handlers["AppointmentBooked"].assert_not_awaited()
    session.execute.assert_not_awaited()


def setup_analysis(session, monkeypatch, *, emergency=False, fired=True):
    trigger = TriggerDef(
        trigger_id="polyp",
        display_name="Полип",
        source_study="",
        synonyms=("полип",),
        emergency_flag=emergency,
    )
    extraction = ExtractionResult(
        findings=[ExtractedFinding(finding="полип", quote="полип")] if fired else []
    )
    monkeypatch.setattr(
        "app.services.mis.get_extractor", lambda: MagicMock(extract=lambda *a, **k: extraction)
    )
    monkeypatch.setattr("app.services.mis.DecisionEngine", lambda: DecisionEngine([trigger]))
    definition = ModelTriggerDef(id=1, trigger_id="polyp", specialty_id=None, target_sla_days=7)
    objects = {ModelTriggerDef: definition}

    def add(obj):
        objects[type(obj)] = obj

    session.add.side_effect = add
    bind_session(session, objects)
    handler = MisEventHandler()
    handler._protocol_routes = AsyncMock(return_value=[])
    delivery = event("StudyProtocolSigned", text="полип")
    delivery["subject"] = {"study_id": str(uuid4()), "patient_id": str(uuid4())}
    return handler, delivery, objects


async def test_emergency_не_создаёт_маршрут(session, monkeypatch, model_clock):
    handler, delivery, _ = setup_analysis(session, monkeypatch, emergency=True)
    handler.routing.create_from_match = AsyncMock()
    result = await handler.handle(session, delivery)
    assert result["is_emergency"] is True
    assert "escalate_emergency" in result["actions"]
    assert result["route_id"] is None
    handler.routing.create_from_match.assert_not_awaited()
    added = [call.args[0] for call in session.add.call_args_list]
    assert any(isinstance(row, Study) for row in added)
    assert any(isinstance(row, Protocol) for row in added)
    assert any(isinstance(row, Finding) for row in added)
    assert any(isinstance(row, TriggerMatch) for row in added)
    assert not any(isinstance(row, Route) for row in added)


async def test_невозможный_переход_не_роняет_обработку(session, model_clock):
    route = Route(id=uuid4(), status=RouteStatus.CREATED)
    bind_session(session, {Route: route})
    handler = MisEventHandler()
    # Боевой автомат отклоняет переход по статусу, а не по отсутствию маршрута:
    # запоминаем исключение, иначе «маршрут не найден» сошёл бы за невозможный переход.
    refusals = []
    transition = handler.routing.transition

    async def spy(session_, route_id, to_status, actor, basis, **fields):
        try:
            return await transition(session_, route_id, to_status, actor, basis, **fields)
        except RoutingTransitionError as exc:
            refusals.append(str(exc))
            raise

    handler.routing.transition = spy
    result = await handler.handle(session, event("VisitNoShow", route))
    assert result["status"] == "processed"
    assert "transition_rejected: created→no_show" in result["actions"]
    assert route.status == RouteStatus.CREATED
    assert refusals, "переход должен быть отклонён боевым автоматом, а не пропущен"
    assert "невозможен" in refusals[0], (
        f"отказ должен касаться матрицы переходов, а не поиска маршрута: {refusals[0]}"
    )
    rows = [call.args[0] for call in session.add.call_args_list]
    assert next(row for row in rows if isinstance(row, MisEvent)).processed_at == model_clock.now()
    assert next(row for row in rows if isinstance(row, AuditLog)).details == result


def test_типы_событий_покрыты_обработчиками():
    handler = MisEventHandler()
    assert set(handler.handlers) == set(EVENT_TYPES)
    assert all(callable(function) for function in handler.handlers.values())


async def test_сценарий_событий_по_цепочке_маршрута(session, model_clock):
    # Боевой автомат не допускает created→booked: маршрут сначала уведомляется.
    route = Route(id=uuid4(), status=RouteStatus.AWAITING_BOOKING)
    objects = {Route: route}
    # MisEvent в реестр не кладём: иначе проверка идемпотентности увидит прошлую доставку.
    session.add.side_effect = lambda obj: (
        None if isinstance(obj, MisEvent) else objects.update({type(obj): obj})
    )
    bind_session(session, objects)
    handler = MisEventHandler()
    chain = [
        ("AppointmentBooked", "booked", {}),
        ("VisitCompleted", "decision_pending", {}),
        ("TacticsChosen", "referred", {"tactics": "surgery_indicated"}),
        ("HospitalizationScheduled", "hospitalization_scheduled", {}),
        ("HospitalizationFactual", "hospitalized", {}),
        ("SurgeryPerformed", "operated", {"service_code": "operation"}),
        ("Discharged", "discharged", {}),
    ]
    for name, status, payload in chain:
        result = await handler.handle(session, event(name, route, **payload))
        assert result["status"] == "processed"
        assert route.status == status
        assert not any(action.startswith("transition_rejected") for action in result["actions"])
        if name == "VisitCompleted":
            assert "transition: booked→visit_done" in result["actions"]
            assert "transition: visit_done→decision_pending" in result["actions"]
    assert objects[Appointment].visit_status == "completed"
    assert objects[Hospitalization].status == "discharged"
    assert objects[Surgery].service_code == "operation"
    assert objects[Followup].target_date > model_clock.now().date()


async def test_исправление_переиспользует_срабатывание(session, monkeypatch, model_clock):
    handler, delivery, objects = setup_analysis(session, monkeypatch)
    handler.routing.create_from_match = AsyncMock(return_value=Route(id=uuid4(), status="created"))
    await handler.handle(session, delivery)
    stored = objects[TriggerMatch]
    route = Route(id=uuid4(), trigger_match_id=stored.id, status="created")
    handler._protocol_routes.return_value = [route]
    objects.pop(MisEvent, None)
    session.add.reset_mock()
    delivery["event_id"] = str(uuid4())
    delivery["event_type"] = "StudyProtocolCorrected"
    await handler.handle(session, delivery)
    assert objects[TriggerMatch] is stored
    assert not any(isinstance(call.args[0], TriggerMatch) for call in session.add.call_args_list)
    assert handler.routing.create_from_match.await_count == 1
    assert objects[Protocol].is_corrected


async def test_отзыв_триггера_закрывает_маршрут(session, monkeypatch, model_clock):
    handler, delivery, objects = setup_analysis(session, monkeypatch, fired=False)
    definition = objects[ModelTriggerDef]
    stored = TriggerMatch(id=uuid4(), protocol_id=uuid4(), trigger_def_id=definition.id, fired=True)
    objects[TriggerMatch] = stored
    route = Route(id=uuid4(), trigger_match_id=stored.id, status="created")
    handler._protocol_routes.return_value = [route]
    # Боевой close() читает маршрут заново через execute(), а не из переданного объекта.
    objects[Route] = route
    handler.timers.cancel_for_route = AsyncMock()
    delivery["event_type"] = "StudyProtocolCorrected"
    await handler.handle(session, delivery)
    assert route.status == "cancelled"
    assert route.close_reason == "trigger_withdrawn"
    handler.timers.cancel_for_route.assert_awaited_once_with(session, route.id)


async def test_неизвестное_событие_сохраняется_как_игнорированное(session, model_clock):
    result = await MisEventHandler().handle(session, event("Unknown"))
    assert result["status"] == "ignored"
    assert result["duplicate"] is False


async def test_api_повтор_возвращает_200(client, session):
    from app.db import get_session
    from app.main import app

    async def dependency():
        yield session

    session.commit = AsyncMock()
    delivery = event("Unknown")
    delivery.update(occurred_at="2026-10-04T09:00:00Z")
    app.dependency_overrides[get_session] = dependency
    try:
        response = await client.post("/api/v1/mis/events", json=delivery)
        assert response.status_code == 202
        session.scalar.return_value = MisEvent(event_id=delivery["event_id"])
        response = await client.post("/api/v1/mis/events", json=delivery)
        assert response.status_code == 200
        assert response.json()["duplicate"] is True
    finally:
        app.dependency_overrides.pop(get_session)


# ── Регрессия: обработчик МИС звал выдуманные методы боевых сервисов ───────────
# В моках сервисов такие вызовы проходили незаметно, а в рантайме событие МИС
# падало на первом же настоящем вызове. Тесты ниже сверяют исходник с боевым API.


def test_используются_реальные_методы_роутинга():
    assert hasattr(RoutingService, "create_from_match")
    assert not hasattr(RoutingService, "create")


def test_используются_реальные_методы_таймеров():
    assert hasattr(TimerEngine, "cancel_for_route")
    assert not hasattr(TimerEngine, "cancel")


def test_в_исходнике_нет_несуществующих_вызовов():
    from app.services import mis

    source = inspect.getsource(mis)
    assert "routing.create(" not in source
    assert "timers.cancel(" not in source


def test_обработчик_использует_существующие_api():
    """Каждый вызов self.routing.X / self.timers.Y обязан существовать в боевом классе."""
    from app.services import mis

    source = inspect.getsource(mis)
    calls = set(re.findall(r"self\.(routing|timers)\.(\w+)", source))
    assert calls, "обработчик не обращается к сервисам — проверка потеряла смысл"
    services = {"routing": RoutingService, "timers": TimerEngine}
    missing = [
        f"{service}.{method}"
        for service, method in sorted(calls)
        if not hasattr(services[service], method)
    ]
    assert not missing, f"вызовы методов, которых нет в боевых сервисах: {missing}"

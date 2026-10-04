"""Проверки своего кабинета на SQL: изоляция, несколько маршрутов и реальные записи."""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session

from app.api.mock_lk import STAGE_TEXT, router
from app.auth import authorize, verify
from app.auth import router as auth_router
from app.clock import get_clock
from app.db import get_session
from app.models import (
    Appointment,
    AuditLog,
    Base,
    Clinic,
    Doctor,
    Finding,
    Followup,
    Hospitalization,
    Notification,
    Patient,
    Protocol,
    Route,
    RouteStatus,
    Specialty,
    Study,
    Task,
    TriggerDef,
    TriggerMatch,
)
from tests.conftest import auth_headers

pytestmark = pytest.mark.anyio


class CabinetSession:
    """Асинхронный интерфейс к настоящей локальной базе для проверок запросов."""

    def __init__(self):
        self.engine = create_engine("sqlite://")
        metadata = MetaData()
        for table in Base.metadata.sorted_tables:
            copy = table.to_metadata(metadata)
            for column in copy.columns:
                if column.server_default is not None and (
                    "::" in str(column.server_default.arg) or column.name == "id"
                ):
                    column.server_default = None
        metadata.create_all(self.engine)
        self.sync = Session(self.engine, expire_on_commit=False)

    def add(self, row):
        self.sync.add(row)

    async def scalar(self, query):
        return self.sync.scalar(query)

    async def scalars(self, query):
        return self.sync.scalars(query)

    async def execute(self, query):
        return self.sync.execute(query)

    async def get(self, model, identifier):
        return self.sync.get(model, identifier)

    async def commit(self):
        self.sync.commit()


@pytest.fixture
async def cabinet_client(model_clock):
    """Роутер подключён с теми же правами, что и основное приложение."""
    session = CabinetSession()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router, dependencies=[Depends(authorize)])
    app.dependency_overrides[get_session] = lambda: session
    own, foreign = uuid4(), uuid4()
    for identifier in (own, foreign):
        session.add(
            Patient(id=identifier, external_id=str(identifier), anonymized_hash=identifier.hex)
        )
    await session.commit()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers("patient", [str(own)]),
    ) as client:
        yield client, session, own, foreign
    session.sync.close()
    session.engine.dispose()


def add_route(session, patient_id, status="awaiting_booking"):
    """Создаёт маршрут без побочных эффектов таймеров."""
    route = Route(
        id=uuid4(),
        patient_id=patient_id,
        status=status,
        created_at=get_clock().now(),
        target_date=get_clock().now().date() + timedelta(days=14),
    )
    session.add(route)
    session.sync.commit()
    return route


@pytest.mark.parametrize("suffix", ["cabinet", "messages", "route", "banner"])
async def test_чужой_кабинет_запрещён(cabinet_client, suffix):
    client, _, _, foreign = cabinet_client
    response = await client.get(f"/api/v1/mock/lk/{foreign}/{suffix}")
    assert response.status_code == 403


async def test_чужое_прочтение_и_ответ_запрещены(cabinet_client):
    client, session, own, foreign = cabinet_client
    route = add_route(session, foreign)
    for path, body in [
        (f"{foreign}/messages/{uuid4()}/read", {}),
        (f"{own}/banner/{route.id}/response", {"action": "question"}),
    ]:
        assert (await client.post("/api/v1/mock/lk/" + path, json=body)).status_code == 403
    assert not session.sync.scalars(select(AuditLog)).all()


async def test_вход_область_и_пустые_разделы(cabinet_client):
    client, _, own, _ = cabinet_client
    demo = (await client.get("/api/v1/auth/demo")).json()
    login = await client.post(
        "/api/v1/auth/login", json={"username": "patient", "password": demo["password"]}
    )
    assert login.status_code == 200
    assert login.json()["role"] == "patient"
    assert len(verify(login.json()["access_token"])["patient_ids"]) == 1
    me = (await client.get("/api/v1/auth/me")).json()
    assert me == {"role": "patient", "patient_ids": [str(own)]}
    response = await client.get(f"/api/v1/mock/lk/{own}/cabinet")
    assert response.json() == {"protocols": [], "routes": [], "appointments": []}
    client.headers.clear()
    assert (await client.get(f"/api/v1/mock/lk/{own}/cabinet")).status_code == 401


async def test_все_протоколы_маршруты_и_факты(cabinet_client):
    client, session, own, foreign = cabinet_client
    session.add(Specialty(id=1, code="gyn", name="Гинеколог"))
    session.add(Clinic(id=1, code="clinic", name="СМ-Клиника"))
    session.add(Doctor(id=1, full_name="Иванова Анна", specialty_id=1, clinic_id=1))
    session.add(
        TriggerDef(
            id=1,
            trigger_id="polyp",
            version=1,
            display_name="Полип",
            source_study="УЗИ",
            specialty_id=1,
            target_sla_days=14,
            priority=2,
        )
    )
    study = Study(
        id=uuid4(),
        patient_id=own,
        study_type="УЗИ малого таза",
        study_date=get_clock().now().date(),
        raw_text="Текст",
    )
    older = Study(
        id=uuid4(),
        patient_id=own,
        study_type="УЗИ",
        study_date=study.study_date - timedelta(days=30),
        raw_text="Текст",
    )
    session.add(study)
    session.add(older)
    protocol = Protocol(id=uuid4(), study_id=study.id, signed_at=get_clock().now())
    finding = Finding(id=uuid4(), study_id=study.id, finding="Полип", quote="Полип 12 мм")
    session.add(protocol)
    session.add(finding)
    match = TriggerMatch(
        id=uuid4(),
        protocol_id=protocol.id,
        finding_id=finding.id,
        trigger_def_id=1,
        fired=True,
        applied_rule="Требует внимания",
    )
    session.add(match)
    first = add_route(session, own)
    first.trigger_match_id = match.id
    first.specialty_id = 1
    first.clinic_id = 1
    second = add_route(session, own, "booked")
    add_route(session, own, "followup_done")
    add_route(session, foreign)
    session.add(
        Appointment(
            id=uuid4(),
            route_id=second.id,
            doctor_id=1,
            clinic_id=1,
            starts_at=get_clock().now() + timedelta(days=2),
            visit_status="cancelled",
        )
    )
    session.add(
        Followup(
            id=uuid4(),
            route_id=first.id,
            target_date=get_clock().now().date() + timedelta(days=30),
            status="scheduled",
        )
    )
    session.add(
        Hospitalization(
            id=uuid4(),
            route_id=first.id,
            scheduled_date=get_clock().now().date() + timedelta(days=4),
            status="scheduled",
        )
    )
    await session.commit()
    session.sync.expire_all()
    data = (await client.get(f"/api/v1/mock/lk/{own}/cabinet")).json()
    assert len(data["protocols"]) == 2
    assert data["protocols"][0]["findings"] == ["Полип"]
    assert data["protocols"][0]["recommendations"][0]["specialty"] == "Гинеколог"
    assert len(data["routes"]) == 3
    assert sum(r["completed"] for r in data["routes"]) == 1
    pending = next(r for r in data["routes"] if r["route_id"] == str(second.id))
    assert "не подтверждена" in pending["stage"]
    assert pending["can_confirm"] is False
    assert len(data["appointments"]) == 3
    assert any(
        a["state"] == "Запись отменена" and a["who"] == "Иванова Анна" for a in data["appointments"]
    )
    assert any("ещё не подтверждена" in a["state"] for a in data["appointments"])
    assert not any(a["type"] == "Операция" for a in data["appointments"])


async def test_прочтение_сохраняется_и_повтор_без_дубля(cabinet_client):
    client, session, own, _ = cabinet_client
    route = add_route(session, own)
    message = Notification(
        id=uuid4(),
        route_id=route.id,
        channel="lk",
        template_code="test",
        body="Требует внимания",
        sent_at=get_clock().now(),
        delivery_status="delivered",
    )
    session.add(message)
    await session.commit()
    base = f"/api/v1/mock/lk/{own}/messages"
    assert (await client.get(base)).json()[0]["read"] is False
    for _ in range(2):
        assert (await client.post(f"{base}/{message.id}/read", json={})).status_code == 200
    assert (await client.get(base)).json()[0]["read"] is True
    assert len(session.sync.scalars(select(AuditLog)).all()) == 1


@pytest.mark.parametrize("action", ["question", "cannot_attend", "wants_booking"])
async def test_отклик_не_создаёт_запись(cabinet_client, action):
    client, session, own, _ = cabinet_client
    route = add_route(session, own)
    response = await client.post(
        f"/api/v1/mock/lk/{own}/banner/{route.id}/response", json={"action": action}
    )
    assert response.status_code == 200
    assert route.status == "awaiting_booking"
    assert not session.sync.scalars(select(Appointment)).all()
    assert len(session.sync.scalars(select(Task)).all()) == 1
    assert session.sync.scalar(select(AuditLog)).details["answer"] == action


async def test_подтверждение_требует_фактической_записи(cabinet_client):
    client, session, own, _ = cabinet_client
    route = add_route(session, own, "booked")
    path = f"/api/v1/mock/lk/{own}/banner/{route.id}/response"
    assert (await client.post(path, json={"action": "confirmed_booking"})).status_code == 409
    session.add(
        Appointment(
            id=uuid4(),
            route_id=route.id,
            starts_at=get_clock().now() + timedelta(days=1),
            visit_status="booked",
        )
    )
    await session.commit()
    assert (await client.post(path, json={"action": "confirmed_booking"})).status_code == 200
    assert route.status == "booked"


@pytest.mark.parametrize("status", list(RouteStatus))
async def test_все_статусы_без_запрещённых_слов(status):
    import re

    text = " ".join(STAGE_TEXT[status])
    assert not re.search(r"диагноз|лечени|история пациента|карточка пациента", text, re.I)


async def test_чужое_сообщение_в_своём_url_не_раскрывается(cabinet_client):
    """Идентификатор сообщения не позволяет обойти принадлежность пациента."""
    client, session, own, foreign = cabinet_client
    route = add_route(session, foreign)
    message = Notification(
        id=uuid4(),
        route_id=route.id,
        channel="lk",
        template_code="test",
        body="Чужое сообщение",
        sent_at=get_clock().now(),
        delivery_status="delivered",
    )
    session.add(message)
    await session.commit()
    assert (await client.get(f"/api/v1/mock/lk/{own}/messages")).json() == []
    response = await client.post(f"/api/v1/mock/lk/{own}/messages/{message.id}/read", json={})
    assert response.status_code == 404
    assert not session.sync.scalars(select(AuditLog)).all()

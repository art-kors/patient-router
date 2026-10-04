"""Сценарии возвращения и неявки через HTTP на настоящей PostgreSQL."""

import os
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import demo, mis, mock_lk, routes
from app.db import get_session
from app.models import Base, Patient, Timer

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="Нужна тестовая PostgreSQL")


@pytest.fixture
async def scenario(model_clock):
    """Изолировать БД и подключить настоящие сервисы к HTTP-ручкам."""
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        patient_id = uuid4()
        session.add(
            Patient(
                id=patient_id, external_id=str(patient_id), anonymized_hash=str(patient_id), sex="F"
            )
        )
        await session.commit()
        app = FastAPI()
        for router in (mis.router, mock_lk.router, demo.router, routes.router):
            app.include_router(router)

        async def dependency():
            """Выдать реальную транзакционную сессию."""
            yield session

        app.dependency_overrides[get_session] = dependency
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, session, patient_id, model_clock
    await engine.dispose()


async def send(scenario, kind, route_id=None, **payload):
    """Доставить уникальное событие МИС и проверить HTTP-успех."""
    client, _, patient, clock = scenario
    subject = {"patient_id": str(patient)}
    if route_id:
        subject["route_id"] = str(route_id)
    if kind == "StudyProtocolSigned":
        subject["study_id"] = str(uuid4())
    event = {
        "event_id": str(uuid4()),
        "event_type": kind,
        "occurred_at": clock.now().isoformat(),
        "subject": subject,
        "payload": payload,
    }
    response = await client.post("/api/v1/mis/events", json=event)
    assert response.status_code == 202, response.text
    return response.json(), event


async def advance(scenario, hours):
    """Прокрутить часы с настоящим исполнением и доставкой таймеров."""
    response = await scenario[0].post("/api/v1/demo/clock/advance", json={"hours": hours})
    assert response.status_code == 200, response.text
    assert response.json()["database"] == "ok"
    return response.json()


async def create_route(scenario):
    """Создать маршрут из реального текста протокола, без подмены извлечения."""
    result, _ = await send(
        scenario,
        "StudyProtocolSigned",
        study_type="УЗИ органов малого таза",
        text="Заключение: УЗ-признаки полипа эндометрия.",
        study_date="2026-08-26",
    )
    assert result["route_id"], result
    await advance(scenario, 1 / 60)
    return result["route_id"]


async def status(scenario, route_id):
    """Прочитать статус через публичное API."""
    response = await scenario[0].get(f"/api/v1/routes/{route_id}")
    assert response.status_code == 200
    return response.json()["status"]


@pytest.mark.parametrize("kind", ["VisitStarted", "StudyProtocolSigned"])
async def test_врач_видит_находку_через_62_дня(scenario, kind):
    """Убрать баннер из ответа нового визита — этот тест обязан упасть."""
    route_id = await create_route(scenario)
    await advance(scenario, 62 * 24 - 1 / 60)
    result, event = await send(scenario, kind, text="Заключение: патологии не выявлено.")
    assert result["has_unfinished_routes"] is True
    banner = result["unfinished_routes_banner"]
    assert banner["visible"] is True
    assert route_id in banner["route_ids"]
    assert "26.08.2026" in banner["text"]
    assert "полип" in banner["text"].lower()
    assert "эндометр" in banner["text"].lower()
    assert "Прошло 62 дня" in banner["text"]
    assert "Консультация профильного специалиста в системе не зафиксирована" in banner["text"]
    duplicate = await scenario[0].post("/api/v1/mis/events", json=event)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["unfinished_routes_banner"] == banner


@pytest.mark.parametrize("action,target", [("already_attended", "closed_by_patient")])
async def test_ответ_пациента_после_62_дней(scenario, action, target):
    route_id = await create_route(scenario)
    await advance(scenario, 62 * 24)
    response = await scenario[0].post(
        f"/api/v1/mock/lk/{scenario[2]}/banner/{route_id}/response",
        json={"action": action},
    )
    assert response.status_code == 200, response.text
    assert await status(scenario, route_id) == target
    if action == "already_attended":
        result, _ = await send(scenario, "VisitStarted")
        assert result["has_unfinished_routes"] is False


async def no_show(scenario):
    """Довести созданный маршрут до записи и реальной неявки."""
    route_id = await create_route(scenario)
    await send(scenario, "AppointmentBooked", route_id, slot_ref="первый")
    assert await status(scenario, route_id) == "booked"
    await send(scenario, "VisitNoShow", route_id)
    assert await status(scenario, route_id) == "no_show"
    return route_id


async def test_неявка_напоминание_через_45_минут(scenario):
    route_id = await no_show(scenario)
    assert (await advance(scenario, 0.5))["notifications"] == 0
    assert (await advance(scenario, 0.25))["notifications"] == 1
    messages = await scenario[0].get(f"/api/v1/mock/lk/{scenario[2]}/messages")
    assert len(messages.json()) == 2
    assert any("перезаписаться" in message["text"] for message in messages.json())
    timers = (
        await scenario[1].scalars(select(Timer).where(Timer.route_id == route_id, ~Timer.fired))
    ).all()
    assert len(timers) == 5


async def test_перезапись_сбрасывает_эскалацию(scenario):
    route_id = await no_show(scenario)
    await send(scenario, "AppointmentBooked", route_id, slot_ref="повторный")
    assert await status(scenario, route_id) == "booked"
    assert (await advance(scenario, 31 * 24))["notifications"] == 0
    assert await status(scenario, route_id) == "booked"


async def test_без_перезаписи_полная_эскалация(scenario):
    route_id = await no_show(scenario)
    elapsed = 0
    for hours in (0.75, 24, 72, 120, 336, 720):
        result = await advance(scenario, hours - elapsed)
        assert result["notifications"] == 1
        elapsed = hours
    assert await status(scenario, route_id) == "route_not_realized"
    tasks = await scenario[0].get("/api/v1/mock/lk/coordinator/tasks")
    assert len(tasks.json()) == 1


async def test_отмена_записи_требует_повторной_записи(scenario):
    route_id = await create_route(scenario)
    await send(scenario, "AppointmentBooked", route_id)
    await send(scenario, "AppointmentCancelled", route_id)
    assert await status(scenario, route_id) == "booking_required"
    assert (await advance(scenario, 0.75))["notifications"] == 1

"""Контракт демонстрационного кабинета без PostgreSQL."""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.mock_lk import STAGE_TEXT, router
from app.db import get_session
from app.models import AuditLog, Notification, Patient, Route, RouteStatus, Task, TimerType
from app.services.notifications import send_effect
from app.services.timers import Effect


@pytest.fixture
def lk(model_clock):
    """Подключает новый роутер отдельно от основного приложения."""
    app = FastAPI()
    app.include_router(router)
    session = AsyncMock()
    session.add = Mock()
    patient_id = uuid4()
    session.get.return_value = Patient(id=patient_id)
    session.scalar.return_value = None
    session.scalars.return_value = Mock(all=Mock(return_value=[]))

    async def fake_session():
        """Предоставляет подменённую сессию."""
        yield session

    app.dependency_overrides[get_session] = fake_session
    return app, session, patient_id


async def request(lk, path, method="GET"):
    """Выполняет запрос к изолированному кабинету."""
    async with AsyncClient(transport=ASGITransport(app=lk[0]), base_url="http://test") as client:
        return await client.request(method, f"/api/v1/mock/lk/{path}")


@pytest.mark.anyio
async def test_доставка_видна_в_кабинете(lk, model_clock):
    _, session, patient_id = lk
    route = Route(id=uuid4(), patient_id=patient_id, clinic_id=None)
    session.get.return_value = route
    message = await send_effect(
        session,
        Effect(
            "notification",
            "lk",
            None,
            "notify_initial",
            route.id,
            TimerType.NOTIFY_INITIAL,
        ),
    )
    session.get.return_value = Patient(id=patient_id)
    session.scalars.side_effect = [
        Mock(all=Mock(return_value=[message])),
        Mock(all=Mock(return_value=[])),
    ]
    response = await request(lk, f"{patient_id}/messages")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(message.id),
            "date": model_clock.now().isoformat().replace("+00:00", "Z"),
            "text": message.body,
            "read": False,
        }
    ]
    statement = session.scalars.call_args_list[0].args[0]
    assert "lk" in statement.compile().params.values()
    assert patient_id in statement.compile().params.values()


@pytest.mark.anyio
async def test_прочтение_сохраняется_и_отображается(lk, model_clock):
    _, session, patient_id = lk
    message = Notification(
        id=uuid4(), route_id=uuid4(), sent_at=model_clock.now(), body="Приглашаем на приём."
    )
    session.scalar.side_effect = [message, None]
    response = await request(lk, f"{patient_id}/messages/{message.id}/read", "POST")
    assert response.status_code == 200
    assert response.json()["read"] is True
    log = session.add.call_args.args[0]
    assert isinstance(log, AuditLog)
    assert log.details == {"message_id": str(message.id)}
    session.commit.assert_awaited_once()
    session.scalars.side_effect = [
        Mock(all=Mock(return_value=[message])),
        Mock(all=Mock(return_value=[log])),
    ]
    response = await request(lk, f"{patient_id}/messages")
    assert response.json()[0]["read"] is True
    session.scalar.side_effect = [message, log]
    session.add.reset_mock()
    response = await request(lk, f"{patient_id}/messages/{message.id}/read", "POST")
    assert response.status_code == 200
    session.add.assert_not_called()


@pytest.mark.anyio
async def test_чужое_сообщение_нельзя_прочитать(lk):
    response = await request(lk, f"{lk[2]}/messages/{uuid4()}/read", "POST")
    assert response.status_code == 404
    statement = lk[1].scalar.call_args.args[0]
    assert lk[2] in statement.compile().params.values()
    lk[1].commit.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["messages", "route", "banner"])
async def test_неизвестный_пациент(lk, suffix):
    lk[1].get.return_value = None
    assert (await request(lk, f"{lk[2]}/{suffix}")).status_code == 404


@pytest.mark.anyio
async def test_баннер_при_возвращении_через_два_месяца(lk, model_clock):
    _, session, patient_id = lk
    route = Route(
        id=uuid4(), patient_id=patient_id, status="decision_pending", created_at=model_clock.now()
    )
    model_clock.advance(62 * 24)
    session.scalars.return_value = Mock(all=Mock(return_value=[route]))
    response = await request(lk, f"{patient_id}/banner")
    assert response.json()["title"] == "Незавершённый клинический маршрут"
    assert response.json()["route_ids"] == [str(route.id)]
    statement = session.scalars.call_args.args[0]
    assert "closed_at IS NULL" in str(statement)
    assert "NOT IN" in str(statement)
    assert route.created_at < model_clock.now() - timedelta(days=60)


@pytest.mark.anyio
async def test_нет_маршрута_нет_баннера(lk):
    assert (await request(lk, f"{lk[2]}/banner")).json() == {
        "visible": False,
        "title": None,
        "text": None,
        "route_ids": [],
    }
    assert (await request(lk, f"{lk[2]}/route")).json() is None


@pytest.mark.anyio
@pytest.mark.parametrize("status", list(RouteStatus))
async def test_этапы_описаны_простым_языком(lk, model_clock, status):
    _, session, patient_id = lk
    route = Route(
        id=uuid4(),
        patient_id=patient_id,
        status=status,
        closed_at=None,
        created_at=model_clock.now(),
        target_date=model_clock.now().date(),
    )
    session.scalar.return_value = route
    if status in {
        RouteStatus.BOOKED,
        RouteStatus.HOSPITALIZATION_SCHEDULED,
        RouteStatus.FOLLOWUP_SCHEDULED,
    }:
        session.scalar.side_effect = [route, None]
    response = await request(lk, f"{patient_id}/route")
    assert response.status_code == 200
    body = response.json()
    assert body["what_happened"] == STAGE_TEXT[status][0]
    assert body["what_to_do"] == STAGE_TEXT[status][1]
    assert "status" not in body


@pytest.mark.anyio
async def test_задачи_координатора(lk, model_clock):
    _, session, patient_id = lk
    task = Task(
        id=uuid4(),
        route_id=uuid4(),
        task_type="escalate",
        priority=1,
        created_at=model_clock.now(),
        due_at=model_clock.now(),
        status="open",
    )
    message = Notification(id=task.id, body="Уточните дату поступления.")
    session.execute.return_value = Mock(all=Mock(return_value=[(task, message, patient_id)]))
    response = await request(lk, "coordinator/tasks")
    assert response.status_code == 200
    assert response.json()[0]["channel"] == "escalate"
    assert response.json()[0]["text"] == message.body
    assert "coordinator" in session.execute.call_args.args[0].compile().params.values()

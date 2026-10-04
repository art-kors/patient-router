"""Проверки доставки и конфигурации без настоящей БД."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.models import Appointment, Clinic, Doctor, Notification, Route, Task, TimerType
from app.services.notifications import NotificationService, render_message, send_effect
from app.services.timers import Effect


@pytest.fixture
def session(model_clock):
    """Выдаёт маршрут и собирает записи, добавленные сервисом."""
    route = Route(id=uuid4(), patient_id=uuid4(), clinic_id=None)
    session = AsyncMock()
    session.add = Mock()
    session.get.return_value = route
    session.scalar.return_value = None
    return session


def make_effect(session, kind="notification", channel="lk", code="notify_initial"):
    """Создаёт эффект по контракту движка таймеров."""
    return Effect(
        kind, channel, "coordinator", code, session.get.return_value.id, TimerType.NOTIFY_INITIAL
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind,channel,code",
    [
        ("notification", "lk", "notify_initial"),
        ("task", None, "create_task"),
        ("escalate", None, "escalate"),
        ("close_route", None, "close_route"),
        ("followup", None, "schedule_followup"),
    ],
)
async def test_эффект_сохраняет_сообщение(session, model_clock, kind, channel, code):
    message = await send_effect(session, make_effect(session, kind, channel, code))
    assert isinstance(message, Notification)
    assert message.sent_at == model_clock.now()
    assert "{{" not in message.body
    assert message.delivery_status == "delivered"
    assert message.channel == ("task" if kind in {"task", "escalate"} else "lk")
    rows = [call.args[0] for call in session.add.call_args_list]
    if kind in {"task", "escalate"}:
        task = next(row for row in rows if isinstance(row, Task))
        assert task.id == message.id
        assert task.assignee_role == "coordinator"
        assert task.priority == (1 if kind == "escalate" else 3)
        assert task.task_type == kind
    else:
        assert len(rows) == 1
    session.commit.assert_not_awaited()
    session.flush.assert_awaited_once()


@pytest.mark.anyio
async def test_шаблон_меняется_из_окружения(session, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_TEMPLATES", '{"notify_initial": "{{пациент}}, ждём вас!"}')
    message = await send_effect(session, make_effect(session), {"пациент": "Анна"})
    assert message.body == "Анна, ждём вас!"


@pytest.mark.anyio
async def test_данные_приёма_и_подстановки(session, model_clock):
    route = session.get.return_value
    appointment = Appointment(doctor_id=1, clinic_id=2, starts_at=model_clock.now())
    session.scalar.return_value = appointment
    session.get.side_effect = [
        route,
        Clinic(name="Клиника на Лесной"),
        Doctor(full_name="Иванов И. И."),
    ]
    service = NotificationService(
        {"notify_initial": "{{пациент}} | {{дата_приёма}} | {{врач}} | {{учреждение}}"}
    )
    message = await service.send_effect(session, make_effect(session), {"пациент": "Анна"})
    assert message.body == "Анна | 26.08.2026 в 17:32 | Иванов И. И. | Клиника на Лесной"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind,channel,code",
    [
        ("bad", "lk", "notify_initial"),
        ("notification", "sms", "notify_initial"),
        ("notification", "lk", "missing"),
    ],
)
async def test_некорректный_эффект_не_отправляется(session, kind, channel, code):
    with pytest.raises(ValueError):
        await send_effect(session, make_effect(session, kind, channel, code))
    session.add.assert_not_called()


@pytest.mark.anyio
async def test_неизвестный_маршрут(session):
    effect = make_effect(session)
    session.get.return_value = None
    with pytest.raises(ValueError, match="Маршрут не найден"):
        await send_effect(session, effect)
    session.add.assert_not_called()


def test_неизвестная_подстановка_отклоняется():
    with pytest.raises(ValueError, match="Неизвестное поле"):
        render_message("{{ неизвестное }}", {})


def test_подстановка_не_исполняет_выражения():
    assert render_message("{{пациент}}", {"пациент": "{{врач}}"}) == "{{врач}}"

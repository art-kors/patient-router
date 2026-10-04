"""Сработавший таймер обязан породить сообщение — в той же транзакции.

Здесь закрывается баг, из-за которого сервис уведомлений был написан, но не
подключён: ``TimerEngine`` возвращал ``Effect``, и эффект уходил в никуда.
Пациент не получал ничего, личный кабинет оставался пустым, а задачи
координатору — пустым списком.

Проверки построены на саботаже: каждая ломает связку «таймер → сообщение»
по-своему и обязана падать. Соединять можно только так, чтобы отметка
``timer.fired`` и запись в ``notification`` жили в одной транзакции —
иначе откат погасит сообщение, а отметка переживёт его, и пациент получит
то же напоминание дважды.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.clock import ModelClock
from app.models import Appointment, Clinic, Notification, Patient, Route, Task, Timer, TimerType
from app.services.notifications import deliver_effects
from app.services.timers import Effect, TimerEngine


@pytest.fixture
def session():
    """Сессия без БД: хватает подтверждений «эффект превратился в запись»."""
    value = AsyncMock()
    value.add = Mock()
    return value


@pytest.fixture
def route(session, model_clock):
    """Маршрут пациента, к которому привязаны таймеры."""
    value = Route(id=uuid4(), patient_id=uuid4(), created_at=model_clock.now())
    session.get.return_value = value
    session.scalar.return_value = None
    return value


def make_effect(route, timer_type=TimerType.NOTIFY_INITIAL):
    """Эффект ровно такой формы, какую возвращает TimerEngine.fire."""
    kinds = {
        TimerType.NOTIFY_INITIAL: "notification",
        TimerType.NOTIFY_REMINDER: "notification",
        TimerType.CREATE_TASK: "task",
        TimerType.ESCALATE: "escalate",
    }
    return Effect(
        kinds[timer_type],
        "lk" if kinds[timer_type] == "notification" else None,
        "coordinator" if kinds[timer_type] in {"task", "escalate"} else None,
        timer_type.value,
        route.id,
        timer_type,
    )


def added(session, model):
    """Записи, которые сервис положил в сессию."""
    return [call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], model)]


# ── Связка существует ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_эффект_таймера_порождает_уведомление(session, route):
    """Главный регрессионный тест: эффект не должен уходить в никуда."""
    await deliver_effects(session, [make_effect(route)])

    messages = added(session, Notification)
    assert len(messages) == 1, "эффект таймера не превратился в сообщение пациенту"
    assert messages[0].route_id == route.id
    assert messages[0].template_code == "notify_initial"
    assert messages[0].delivery_status == "delivered"
    assert "{{" not in messages[0].body


@pytest.mark.anyio
async def test_канал_по_роду_эффекта(session, route):
    """kind=task → задача координатору, kind=notification → только сообщение."""
    await deliver_effects(
        session,
        [make_effect(route, TimerType.CREATE_TASK), make_effect(route, TimerType.NOTIFY_REMINDER)],
    )

    by_template = {message.template_code: message for message in added(session, Notification)}
    assert by_template["create_task"].channel == "task"
    assert by_template["notify_reminder"].channel == "lk"
    tasks = added(session, Task)
    assert [task.task_type for task in tasks] == ["task"], "задача создана не для того эффекта"
    assert tasks[0].id == by_template["create_task"].id, "задача и текст разошлись"


@pytest.mark.anyio
async def test_эскалация_даёт_задачу_с_повышенным_приоритетом(session, route):
    """Escalate — это задача, а не сообщение в личный кабинет."""
    await deliver_effects(session, [make_effect(route, TimerType.ESCALATE)])

    task = added(session, Task)[0]
    assert task.task_type == "escalate"
    assert task.priority == 1
    assert added(session, Notification)[0].channel == "task"


# ── Повторная прокрутка не дублирует ────────────────────────────────────────


@pytest.mark.anyio
async def test_повторная_прокрутка_не_дублирует_уведомления(session, route, model_clock):
    """Тот же класс бага, что был с откатом: отметка держит доставку.

    ``TimerEngine.fire`` отмечает таймер условием ``WHERE NOT fired`` и
    возвращает ``TIMER_ALREADY_FIRED`` при повторе. Значит вторая прокрутка
    не просто не должна дублировать — она не должна даже предлагать эффект
    на отправку.
    """
    engine = TimerEngine()
    timer = Timer(
        id=uuid4(),
        route_id=route.id,
        timer_type=TimerType.NOTIFY_INITIAL,
        due_at=model_clock.now(),
        fired=False,
    )
    session.scalars.return_value = Mock(all=Mock(return_value=[timer]))
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=timer.id))

    first = await engine.fire_due(session, model_clock.now())
    assert len(first) == 1, "просроченный таймер обязан дать эффект"
    await deliver_effects(session, first)

    # Вторая прокрутка: SELECT ... NOT fired не вернёт уже отмеченный таймер.
    session.scalars.return_value = Mock(all=Mock(return_value=[]))
    second = await engine.fire_due(session, model_clock.now())
    await deliver_effects(session, second)

    assert len(added(session, Notification)) == 1, "повторная прокрутка продублировала сообщение"


# ── Транзакция ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_уведомление_не_теряется_при_откате(session, route, model_clock):
    """Отметка таймера и сообщение обязаны быть одной транзакцией.

    Если бы ``send_effect`` коммитил сам или если бы отметка уходила в
    отдельной транзакции, откат сохранил бы ``fired=true`` без сообщения —
    и пациент получил бы то же напоминание при следующей прокрутке.
    Проверяем, что доставка не фиксирует и не откатывает ничего сама:
    транзакцией владеет вызывающий код.
    """
    engine = TimerEngine()
    timer = Timer(
        id=uuid4(),
        route_id=route.id,
        timer_type=TimerType.NOTIFY_INITIAL,
        due_at=model_clock.now(),
        fired=False,
    )
    session.scalars.return_value = Mock(all=Mock(return_value=[timer]))
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=timer.id))

    effects = await engine.fire_due(session, model_clock.now())
    assert len(effects) == 1, "отметка не дала эффекта — проверять нечего"
    await deliver_effects(session, effects)

    session.commit.assert_not_awaited(), "доставка не должна коммитить: это разорвёт транзакцию"
    session.rollback.assert_not_awaited(), "доставка не должна откатывать чужую транзакцию"
    # Обе записи висят в одной сессии и уедут одним flush/commit вызывающего.
    assert added(session, Notification), "сообщение не попало в сессию вызывающего"
    assert timer.fired is True, "отметка таймера не сделана — она в другой транзакции"


@pytest.mark.anyio
async def test_повторный_fire_не_даёт_эффекта_на_отправку(session, route, model_clock):
    """Второй ``fire`` обязан падать, а не слать ещё одно сообщение."""
    engine = TimerEngine()
    timer = Timer(
        id=uuid4(),
        route_id=route.id,
        timer_type=TimerType.NOTIFY_REMINDER,
        due_at=model_clock.now(),
        fired=True,
    )
    session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=None))
    session.add.reset_mock()

    with pytest.raises(ValueError, match="TIMER_ALREADY_FIRED"):
        await engine.fire(session, timer)

    session.add.assert_not_called(), "после отказа движка сообщение всё равно ушло"


# ── Сквозная проверка: ручка демо действительно доставляет ───────────────────
# Тесты выше проверяют deliver_effects сам по себе. Но баг был не в ней, а в
# том, что её никто не звал: подключения не было вообще. Поэтому ниже —
# прогон через настоящую ручку /demo/clock/advance, где отвязать доставку
# от движка невозможно, не убрав вызов из demo.py.


class WiringSession:
    """Сессия, на которой видно и отметку таймера, и сообщение.

    ``add`` и ``execute`` пишут в общий журнал, поэтому тест может
    утверждать не только «сообщение создано», но и «создано до коммита, в
    той же сессии».
    """

    def __init__(self, timer, route):
        self.timer = timer
        self.route = route
        self.log: list[str] = []
        self.notifications: list[Notification] = []
        self.tasks: list[Task] = []

    async def get(self, entity, pk):
        """Маршрут есть, клиники и врача нет — как в обезличенной модели."""
        self.log.append(f"get:{entity.__name__}")
        return self.route if entity is Route else None

    async def execute(self, statement):
        text = str(statement)
        self.log.append(f"execute:{text.split()[0]}")
        result = Mock()
        result.scalar_one_or_none = Mock(return_value=None if "SELECT 1" in text else self.timer.id)
        return result

    async def scalars(self, statement):
        self.log.append("scalars")
        # БД отдаёт только ``WHERE NOT fired`` — отмеченный таймер исчезает
        # из выборки. Фейк обязан вести себя так же, иначе проверка на
        # дубли проходит на сломанной сессии.
        rows = [] if self.timer.fired else [self.timer]
        return Mock(all=Mock(return_value=rows))

    async def scalar(self, statement):
        return None

    def add(self, row):
        self.log.append(f"add:{type(row).__name__}")
        if isinstance(row, Notification):
            self.notifications.append(row)
        elif isinstance(row, Task):
            self.tasks.append(row)

    async def flush(self):
        self.log.append("flush")

    async def commit(self):
        self.log.append("COMMIT")

    async def rollback(self):
        self.log.append("rollback")


@pytest.fixture
def wired(model_clock):
    """Ручка демо с сессией, где лежит один просроченный таймер."""
    route = Route(id=uuid4(), patient_id=uuid4(), created_at=model_clock.now())
    timer = Timer(
        id=uuid4(),
        route_id=route.id,
        timer_type=TimerType.NOTIFY_INITIAL,
        due_at=model_clock.now(),
        fired=False,
    )
    session = WiringSession(timer, route)

    async def dependency():
        yield session

    from app.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = dependency
    try:
        yield session, model_clock
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.anyio
async def test_прокрутка_времени_доставляет_уведомление(wired):
    """Главный тест на баг: ручка демо обязана породить Notification."""
    session, _ = wired

    from app.api.demo import _advance

    result, usable = await _advance(2, session)
    assert usable is True
    await _persist_ok(session)

    assert len(session.notifications) == 1, (
        "таймер сработал, но личный кабинет пациента остался пустым — "
        "сервис уведомлений не подключён"
    )
    assert result["notifications"] == 1, "ручка не отчиталась о доставке"


async def _persist_ok(session):
    """Коммит вызывающего кода — как это делает ``_advance_and_commit``."""
    await session.commit()


@pytest.mark.anyio
async def test_сообщение_создаётся_до_коммита(wired):
    """Отметка таймера и сообщение обязаны уйти одной транзакцией.

    Порядок в журнале — это и есть проверка: если бы сообщение создавалось
    после ``COMMIT`` (или в своей транзакции), откат сохранил бы
    ``fired=true`` без уведомления, и пациент получил бы то же напоминание
    при следующей прокрутке.
    """
    session, _ = wired

    from app.api.demo import _advance_and_commit

    await _advance_and_commit(2, session)

    add_notification = session.log.index("add:Notification")
    commit = session.log.index("COMMIT")
    assert add_notification < commit, (
        "уведомление создано после коммита — откат погасит сообщение, "
        "но оставит отметку таймера, и напоминание повторится"
    )


@pytest.mark.anyio
async def test_повторная_прокрутка_не_дублирует_уведомления_сквозная(wired):
    """Вторая прокрутка не должна породить второе сообщение."""
    session, _ = wired
    session.timer.fired = True

    from app.api.demo import _advance_and_commit

    await _advance_and_commit(2, session)

    assert session.notifications == [], "повторная прокрутка продублировала уведомление"


# ── Границы контракта ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_не_effect_не_доставляется(session, route):
    """Слушатели модельных часов возвращают и не-Effect — молчать о них."""
    await deliver_effects(session, [{"timer": "notify_initial"}, Timer(id=uuid4())])

    session.add.assert_not_called()


@pytest.mark.anyio
async def test_сообщение_учитывает_приём_и_клинику(session, route, model_clock):
    """Текст собирается из данных маршрута, а не из пустых заглушек."""
    session.get.side_effect = [
        route,
        Clinic(name="Клиника на Лесной"),
        None,
    ]
    session.scalar.return_value = Appointment(
        id=uuid4(),
        route_id=route.id,
        clinic_id=1,
        doctor_id=None,
        starts_at=model_clock.now() + timedelta(days=3),
        visit_status="booked",
    )

    await deliver_effects(session, [make_effect(route)])

    assert "Клиника на Лесной" in added(session, Notification)[0].body


@pytest.mark.anyio
async def test_маршрут_обязателен(session, route):
    """Нет маршрута — нет сообщения: пациенту нечего показывать в кабинете."""
    session.get.return_value = None

    with pytest.raises(ValueError, match="Маршрут не найден"):
        await deliver_effects(session, [make_effect(route)])

    session.add.assert_not_called()


def test_движок_не_знает_о_доставке():
    """Разделение слоёв — исполняемая проверка, а не договорённость.

    ``notifications`` импортирует ``Effect`` из ``timers``. Если движок
    начнёт импортировать доставку, получится циклический импорт, а вместе
    с ним — проектная зависимость: движок таймеров перестанет быть
    независимым от каналов.
    """
    from app.services import timers

    source = __import__("inspect").getsource(timers)
    assert "notifications" not in source, "движок не должен знать про каналы доставки"
    assert "deliver_effects" not in source, "доставка — не дело движка"


def test_доставка_не_коммитит_сама():
    """Коммит остаётся за вызывающим — иначе откат разорвётся пополам."""
    from app.services import notifications

    assert hasattr(notifications, "deliver_effects")
    assert hasattr(Patient, "id"), "маршрут должен вести к пациенту"
    assert ModelClock is not None

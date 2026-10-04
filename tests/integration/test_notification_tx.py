"""Уведомление и отметка таймера — одна транзакция. Проверка на живой PostgreSQL.

Мок сессии не может доказать главное: что ``UPDATE timer SET fired=true`` и
``INSERT notification`` попадут в базу вместе или не попадут вместе. Здесь
настоящая СУБД, поэтому откат проверяется по-настоящему: поднимаем
транзакцию, откатываем её, смотрим фактические строки.

Проверка честная в обе стороны. Если бы отметка и сообщение жили в разных
транзакциях, тест ``test_откат_гасит_и_отметку_и_сообщение`` упал бы, а
``test_откат_не_оставляет_сообщение_без_отметки`` — прошёл бы. Именно поэтому
здесь нет «зелёных» утверждений о том, чего тест не проверяет.

Запуск: TEST_DATABASE_URL=... pytest tests/integration/test_notification_tx.py
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Notification, Patient, Route, Timer, TimerType
from app.services.notifications import deliver_effects
from app.services.timers import TimerEngine

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
# Без маркера anyio: в проекте включён asyncio_mode=auto, иanyio-плагин
# открывал бы для фикстуры и теста разные event loop — соединение asyncpg
# переезжало бы из одного цикла в другой.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="нужен TEST_DATABASE_URL с живым PostgreSQL"),
]


@pytest.fixture
async def db():
    """Чистая схема на настоящем PostgreSQL, с готовыми миграциями."""
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def route_with_timer(db, model_clock):
    """Маршрут с одним просроченным таймером — как в боевой таблице."""
    value = Route(id=uuid4(), patient_id=uuid4(), created_at=model_clock.now())
    # Пациент нужен по-настоящему: у route есть внешний ключ, и мок его не подменяет.
    db.add(
        Patient(
            id=value.patient_id,
            external_id=f"ext-{uuid4()}",
            anonymized_hash=f"hash-{uuid4()}",
            age=54,
            sex="F",
        )
    )
    db.add(value)
    db.add(
        Timer(
            id=uuid4(),
            route_id=value.id,
            timer_type=TimerType.NOTIFY_INITIAL,
            due_at=model_clock.now(),
            fired=False,
        )
    )
    await db.commit()
    return value


async def count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def test_эффект_таймера_порождает_уведомление_в_базе(db, route_with_timer, model_clock):
    """Главный баг на живой БД: сработавший таймер обязан создать notification."""
    effects = await TimerEngine().fire_due(db, model_clock.now())
    assert len(effects) == 1
    delivered = await deliver_effects(db, effects)
    await db.commit()

    assert len(delivered) == 1
    assert await count(db, Notification) == 1
    stored = (await db.execute(select(Notification))).scalar_one()
    assert stored.route_id == route_with_timer.id
    assert stored.channel == "lk"
    assert stored.delivery_status == "delivered"


async def test_отметка_и_сообщение_сохраняются_вместе(db, route_with_timer, model_clock):
    """Коммит вызывающего фиксирует и отметку, и сообщение."""
    effects = await TimerEngine().fire_due(db, model_clock.now())
    await deliver_effects(db, effects)
    await db.commit()

    timer = (await db.execute(select(Timer))).scalar_one()
    assert timer.fired is True, "отметка таймера не ушла в базу"
    assert timer.fired_at is not None
    assert await count(db, Notification) == 1, "сообщение не ушло в базу"


async def test_откат_гасит_и_отметку_и_сообщение(db, route_with_timer, model_clock):
    """Откат убирает ОБА следа: рассинхрона быть не может.

    Это и есть проверка «уведомление не теряется при откате» в честной
    формулировке. Если бы только одно из двух уходило в отдельной
    транзакции, тест упал бы на ``assert 0 == 1``.
    """
    effects = await TimerEngine().fire_due(db, model_clock.now())
    await deliver_effects(db, effects)
    await db.rollback()

    assert await count(db, Notification) == 0, "сообщение пережило откат"
    assert await count(db, Timer) == 1, "откат не должен удалять таймер"
    timer = (await db.execute(select(Timer))).scalar_one()
    assert timer.fired is False, "отметка таймера пережила откат — дубль неизбежен"


async def test_откат_не_оставляет_сообщение_без_отметки(db, route_with_timer, model_clock):
    """Обратная сторона: отметки нет — значит, и сообщения быть не должно.

    Сообщение без отметки означало бы, что таймер выстрелит снова и пациент
    получит то же напоминание дважды.
    """
    effects = await TimerEngine().fire_due(db, model_clock.now())
    await deliver_effects(db, effects)
    await db.rollback()

    timer = (await db.execute(select(Timer))).scalar_one()
    notifications = await count(db, Notification)
    assert (timer.fired is False) == (notifications == 0), (
        "отметка и сообщение разошлись: таймер и уведомление в разных транзакциях"
    )


async def test_повторная_прокрутка_не_дублирует_уведомления_в_базе(
    db, route_with_timer, model_clock
):
    """Два прохода подряд — одно сообщение, а не два."""
    await deliver_effects(db, await TimerEngine().fire_due(db, model_clock.now()))
    await db.commit()

    second = await TimerEngine().fire_due(db, model_clock.now())
    assert second == [], "отмеченный таймер снова выстрелил"
    await deliver_effects(db, second)
    await db.commit()

    assert await count(db, Notification) == 1, "повторная прокрутка продублировала сообщение"


async def test_прокрутка_после_отката_даёт_ровно_одно_сообщение(db, route_with_timer, model_clock):
    """Сценарий бага с откатом: откат, затем прокрутка — пациент получил одно."""
    await deliver_effects(db, await TimerEngine().fire_due(db, model_clock.now()))
    await db.rollback()

    await deliver_effects(db, await TimerEngine().fire_due(db, model_clock.now()))
    await db.commit()

    assert await count(db, Notification) == 1, "откат привёл к двойному уведомлению"


async def test_канал_по_роду_эффекта_в_базе(db, route_with_timer, model_clock):
    """task → задача координатору, notification → сообщение в кабинет."""
    from app.models import Task

    # Гасим таймер из фикстуры: этот тест проверяетcreate_task, а неnotify_initial.
    await db.execute(
        update(Timer)
        .where(Timer.route_id == route_with_timer.id)
        .values(fired=True, fired_at=model_clock.now())
    )
    db.add(
        Timer(
            id=uuid4(),
            route_id=route_with_timer.id,
            timer_type=TimerType.CREATE_TASK,
            due_at=model_clock.now(),
            fired=False,
        )
    )
    await db.commit()

    effects = await TimerEngine().fire_due(db, model_clock.now())
    assert {effect.kind for effect in effects} == {"task"}, "сработал не тот таймер"
    await deliver_effects(db, effects)
    await db.commit()

    assert await count(db, Notification) == 1
    assert await count(db, Task) == 1
    task = (await db.execute(select(Task))).scalar_one()
    stored = (await db.execute(select(Notification))).scalar_one()
    assert task.id == stored.id, "задача и сообщение не связаны"
    assert task.task_type == "task"
    assert stored.channel == "task"


async def test_личный_кабинет_видит_сообщение(db, route_with_timer, model_clock):
    """То, ради чего всё делается: пациент видит сообщение в кабинете.

    Запрос идёт через настоящий эндпоинт с настоящей сессией — тот же путь,
    которым демонстратор смотрит кабинет на защите.
    """
    await deliver_effects(db, await TimerEngine().fire_due(db, model_clock.now()))
    await db.commit()

    from app.api.mock_lk import messages as lk_messages

    result = await lk_messages(route_with_timer.patient_id, db)
    assert len(result) == 1, "кабинет пациента пуст — ценность не доставлена"
    assert "{{" not in result[0].text


async def test_ручка_демо_доставляет_уведомление_в_базе(db, route_with_timer, model_clock):
    """Сквозная проверка подключения на живой БД.

    Всё, что выше, зовёт ``deliver_effects`` руками. Настоящий баг был в
    другом: ручка продвижения времени доставку не звала вовсе. Здесь
    проходит боевая ``_advance_and_commit`` — тот же путь, которым
    демонстратор крутит время на защите. Убрать доставку из ``demo.py`` —
    значит упасть здесь.
    """
    from app.api.demo import _advance_and_commit

    result = await _advance_and_commit(2, db)

    assert result["database"] == "ok"
    assert result["notifications"] == 1, "ручка откатила эффекты, не отправив ни одного"
    assert await count(db, Notification) == 1, "уведомление не доехало до базы"
    timer = (await db.execute(select(Timer))).scalar_one()
    assert timer.fired is True, "отметка таймера не зафиксирована"


async def test_ручка_демо_повторно_не_дублирует_в_базе(db, route_with_timer, model_clock):
    """Две прокрутки подряд — одно сообщение в базе."""
    from app.api.demo import _advance_and_commit

    await _advance_and_commit(2, db)
    await _advance_and_commit(2, db)

    assert await count(db, Notification) == 1, "повторная прокрутка продублировала уведомление"

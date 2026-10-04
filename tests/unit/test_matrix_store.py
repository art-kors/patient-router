"""Проверки версий и загрузки без внешнего сервера БД."""

from dataclasses import asdict

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.decision import matrix_store
from app.services.decision.matrix import MATRIX_PATH, MatrixError, load_triggers


class MemorySession:
    """Асинхронный адаптер SQLite в памяти для настоящих SQL-запросов слоя хранения."""

    def __init__(self):
        self.bind = create_engine("sqlite://", connect_args={"check_same_thread": False})
        self.sync = Session(self.bind)

    async def connection(self):
        owner = self

        class Connection:
            async def run_sync(self, callback):
                callback(owner.sync.connection())

        return Connection()

    async def execute(self, statement):
        return self.sync.execute(statement)

    async def commit(self):
        self.sync.commit()

    def close(self):
        self.sync.close()
        self.bind.dispose()


@pytest.fixture
def memory_session():
    session = MemorySession()
    yield session
    session.close()


@pytest.fixture
def initial_items():
    return [dict(asdict(trigger), enabled=True) for trigger in load_triggers(MATRIX_PATH)]


async def test_пустая_бд_использует_файл(memory_session, initial_items):
    store = matrix_store.MatrixStore(memory_session)
    await store.prepare()
    assert await store.current() == initial_items
    assert await store.history() == []


async def test_история_сохраняет_исходник_и_все_снимки(memory_session, initial_items):
    store = matrix_store.MatrixStore(memory_session)
    initial_items[0]["target_sla_days"] = 3
    result = await store.save(initial_items, "Врач", "Изменение SLA")
    assert result["version"] == 2
    assert result["warnings"]
    initial_items[0]["enabled"] = False
    await store.save(initial_items, "Врач", "Отключение")
    rows = await store.history()
    assert [r["version"] for r in rows] == [3, 2, 1]
    assert rows[0]["triggers"][0]["enabled"] is False
    assert rows[1]["triggers"][0]["enabled"] is True
    assert (
        rows[2]["triggers"][0]["target_sla_days"] == load_triggers(MATRIX_PATH)[0].target_sla_days
    )
    assert rows[0]["author"] == "Врач"
    assert rows[0]["created_at"] is not None
    assert rows[0]["triggers"][0]["version"] == 3


@pytest.mark.parametrize(
    "patch",
    [
        {"synonyms": []},
        {"priority": 0},
        {"target_sla_days": -1},
        {"thresholds": {"unknown": 1}},
        {"thresholds": {"min_size_mm": float("nan")}},
        {"emergency_flag": True, "priority": 2},
    ],
)
async def test_ошибка_не_создаёт_версию(memory_session, initial_items, patch):
    store = matrix_store.MatrixStore(memory_session)
    await store.prepare()
    initial_items[0].update(patch)
    with pytest.raises(MatrixError):
        await store.save(initial_items, "Врач", "Ошибка")
    assert await store.history() == []


def test_дубликаты_отвергаются(initial_items):
    with pytest.raises(MatrixError, match="уникальный"):
        matrix_store.check_items(initial_items + [initial_items[0]])


def test_загрузка_из_бд_и_отключение(monkeypatch, initial_items):
    initial_items[0]["enabled"] = False

    async def read():
        return initial_items

    monkeypatch.setattr(matrix_store, "_read_database", read)
    monkeypatch.setattr("app.services.decision.engine.refresh_dictionary", lambda triggers: None)
    loaded = load_triggers()
    assert len(loaded) == len(initial_items) - 1
    assert initial_items[0]["trigger_id"] not in {t.trigger_id for t in loaded}


def test_пустая_или_недоступная_бд_оставляет_файл(monkeypatch):
    async def empty():
        return None

    monkeypatch.setattr(matrix_store, "_read_database", empty)
    assert load_triggers() == load_triggers(MATRIX_PATH)

    async def offline():
        raise OSError("Нет соединения")

    monkeypatch.setattr(matrix_store, "_read_database", offline)
    assert load_triggers() == load_triggers(MATRIX_PATH)


def test_явный_путь_не_обращается_к_бд(monkeypatch):
    def unexpected():
        raise AssertionError("Обращение к БД не ожидалось")

    monkeypatch.setattr(matrix_store, "database_triggers", unexpected)
    assert len(load_triggers(MATRIX_PATH)) == 43


def test_повреждённый_снимок_не_маскируется_файлом(monkeypatch):
    async def broken():
        return [{"trigger_id": "broken"}]

    monkeypatch.setattr(matrix_store, "_read_database", broken)
    with pytest.raises(MatrixError):
        load_triggers()

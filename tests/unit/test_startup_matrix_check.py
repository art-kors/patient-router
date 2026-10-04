"""Матрица проверяется на старте, а не лениво на первом запросе.

Регрессия, которую ловит этот файл: матрица грузилась лениво, поэтому
стенд поднимался, /health отвечал 200, и только через минуту первый
же пациент упирался в «матрица маршрутизации не найдена». Для демо
перед жюри это выглядит как поломка на защите.

Теперь lifespan проверяет матрицу и не поднимает приложение, если её
нет или она битая. ASGITransport из httpx НЕ выполняет lifespan,
поэтому эти тесты зовут его напрямую.
"""

import asyncio
from typing import Any

import pytest

from app.main import lifespan
from app.services.decision import matrix as matrix_module
from app.services.decision.matrix import MatrixError


@pytest.fixture
def app_stub() -> Any:
    """Достаточно любого объекта: lifespan его не использует."""

    class Stub:
        pass

    return Stub()


def _run_lifespan(app: Any) -> bool:
    """Прогнать lifespan до конца. True — поднялись, исключение — нет."""

    async def run() -> bool:
        async with lifespan(app):
            return True

    return asyncio.run(run())


def test_старт_падает_если_матрицы_нет(app_stub, monkeypatch: pytest.MonkeyPatch):
    """Нет матрицы → не поднимаемся. Лучше явная ошибка, чем зелёный /health."""

    def boom(path=None):
        raise MatrixError("config/routing_matrix.json: некорректный JSON")

    monkeypatch.setattr(matrix_module, "load_triggers", boom)

    with pytest.raises(MatrixError, match="некорректный JSON"):
        _run_lifespan(app_stub)


def test_старт_падает_если_матрица_битая(app_stub, tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Битый JSON ловится на старте, а не через минуту после /health."""
    bad = tmp_path / "routing_matrix.json"
    bad.write_text("{не json", encoding="utf-8")
    monkeypatch.setattr(matrix_module, "MATRIX_PATH", bad)

    with pytest.raises(MatrixError, match="некорректный JSON"):
        _run_lifespan(app_stub)


def test_старт_проходит_с_валидной_матрицей(app_stub):
    """Рабочая матрица — стартуем без исключений (страховка от ложной тревоги)."""
    assert _run_lifespan(app_stub) is True


def test_реальная_матрица_проходит_валидацию():
    """Проверяем настоящий файл репозитория, а не подмену."""
    from app.services.decision import load_triggers, validate

    triggers = load_triggers()
    assert len(triggers) >= 10
    # validate() не бросает — это подсказки, а не отказ подниматься
    assert isinstance(validate(triggers), list)

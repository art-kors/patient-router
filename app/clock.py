"""Источник времени.

Весь код обязан брать время через ``Clock``. Прямой вызов ``datetime.now()``
в доменной логике ломает модельное время — требование кейса «шаги на 24 часа,
72 часа, 7, 14 и 30 дней должны отрабатывать за секунды».
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from time import perf_counter


class Clock(ABC):
    """Источник текущего времени."""

    @abstractmethod
    def now(self) -> datetime:
        """Текущее время (aware, UTC)."""

    def monotonic(self) -> float:
        """Монотонный счётчик для длительности: перевод модельного времени не влияет."""
        return perf_counter()

    def is_mock(self) -> bool:
        """Модельное ли время — для логирования и /demo/clock."""
        return False


class SystemClock(Clock):
    """Боевое время сервера."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class ModelClock(Clock):
    """Модельное время: двигается только по явной команде.

    ``advance`` возвращает список сработавших таймеров — это и есть
    требование кейса про демонстрацию недельных сценариев за секунды.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._current = start or datetime(2026, 8, 26, 14, 32, tzinfo=UTC)
        self._listeners: list = []

    def now(self) -> datetime:
        return self._current

    def is_mock(self) -> bool:
        return True

    def set(self, to: datetime) -> None:
        """Перевести часы на момент. Время назад двигать нельзя."""
        if to < self._current:
            raise ValueError(f"CLOCK_IN_PAST: {to.isoformat()} < {self._current.isoformat()}")
        self._current = to

    def advance(self, hours: float) -> list:
        """Сдвинуть время вперёд и собрать сработавшие таймеры."""
        target = self._current + timedelta(hours=hours)
        fired: list = []
        for listener in self._listeners:
            fired.extend(listener.collect_due(target))
        self._current = target
        return fired

    def register(self, listener) -> None:
        self._listeners.append(listener)

    def reset(self, start: datetime | None = None) -> None:
        self._current = start or datetime(2026, 8, 26, 14, 32, tzinfo=UTC)
        self._listeners.clear()


_clock: Clock | None = None


def get_clock() -> Clock:
    """Текущий источник времени (синглтон)."""
    global _clock
    if _clock is None:
        from app.settings import get_settings

        _clock = ModelClock() if get_settings().use_model_clock else SystemClock()
    return _clock


def set_clock(clock: Clock | None) -> None:
    """Подменить источник времени (для тестов и демо)."""
    global _clock
    _clock = clock

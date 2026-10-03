"""Таймеры модельного времени, чтобы показывать недели ожидания за секунды.

Очередь в памяти не требует БД. Клинический слой может планировать эффекты
и передавать обработчик, который применит их при достижении срока.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.clock import ModelClock


@dataclass(frozen=True)
class Effect:
    """Действие таймера для объяснения результата прокрутки демонстратору."""

    timer_type: str
    route_id: UUID
    kind: str
    channel: str | None = None
    assignee_role: str | None = None


@dataclass(frozen=True)
class ScheduledTimer:
    """Срок и эффект нужны для просмотра запланированных действий."""

    due_at: datetime
    effect: Effect


class TimerEngine:
    """Выполняет очередь по срокам, чтобы большой скачок не терял таймеры."""

    def __init__(self, apply: Callable[[Effect], None] | None = None) -> None:
        self._timers: list[ScheduledTimer] = []
        self._apply = apply

    def schedule(self, due_at: datetime, effect: Effect) -> None:
        """Добавить действие, которое должно сработать при прокрутке."""
        if due_at.utcoffset() is None:
            raise ValueError("Срок таймера должен содержать часовой пояс")
        self._timers.append(ScheduledTimer(due_at, effect))

    def pending(self, route_id: UUID | None = None) -> list[ScheduledTimer]:
        """Показать очередь, чтобы демонстратор видел следующий срок."""
        return sorted(
            [t for t in self._timers if route_id is None or t.effect.route_id == route_id],
            key=lambda t: t.due_at,
        )

    def collect_due(self, target: datetime) -> list[Effect]:
        """Включить также новые таймеры, созданные обработчиком до целевого срока."""
        fired = []
        while self._timers:
            timer = min(self._timers, key=lambda t: t.due_at)
            if timer.due_at > target:
                break
            if self._apply is not None:
                self._apply(timer.effect)
            self._timers.remove(timer)
            fired.append(timer.effect)
        return fired


def advance_clock(clock: ModelClock, hours: float) -> list[Effect]:
    """Использовать слушателей часов для единого запуска всех due-таймеров."""
    return clock.advance(hours)

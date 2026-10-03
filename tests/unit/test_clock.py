"""Тесты источника времени.

ModelClock — механизм, на котором держится требование кейса «шаги на 24 часа,
72 часа, 7, 14 и 30 дней должны отрабатывать на демонстрации за секунды».
Если он сломается, демонстрация вёрнется.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.clock import Clock, ModelClock, SystemClock, get_clock, set_clock

START = datetime(2026, 8, 26, 14, 32, tzinfo=UTC)


class TestSystemClock:
    def test_now_возвращает_aware_utc(self):
        now = SystemClock().now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_время_идёт_вперёд(self):
        clock = SystemClock()
        assert clock.now() <= clock.now()

    def test_не_модельное(self):
        assert SystemClock().is_mock() is False


class TestModelClock:
    def test_стартует_с_заданной_точки(self):
        assert ModelClock(start=START).now() == START

    def test_помечен_как_модельный(self):
        assert ModelClock(start=START).is_mock() is True

    def test_advance_сдвигает_время(self):
        clock = ModelClock(start=START)
        clock.advance(24)
        assert clock.now() == START + timedelta(hours=24)

    def test_advance_поддерживает_дробные_часы(self):
        clock = ModelClock(start=START)
        clock.advance(0.5)
        assert clock.now() == START + timedelta(minutes=30)

    def test_advance_тридцать_дней_мгновенно(self):
        """Требование кейса: 30 дней эскалаций прокручиваются за секунды."""
        clock = ModelClock(start=START)
        clock.advance(24 * 30)
        assert clock.now() == START + timedelta(days=30)

    def test_advance_возвращает_сработавшие_таймеры(self):
        clock = ModelClock(start=START)
        assert clock.advance(24) == []

    def test_advance_собирает_результаты_листенеров(self):
        clock = ModelClock(start=START)

        class Listener:
            def __init__(self) -> None:
                self.calls: list[datetime] = []

            def collect_due(self, until: datetime) -> list[str]:
                self.calls.append(until)
                return [f"timer@{until.isoformat()}"]

        listener = Listener()
        clock.register(listener)
        fired = clock.advance(24)

        assert len(fired) == 1
        assert listener.calls == [START + timedelta(hours=24)]

    def test_set_переводит_вперёд(self):
        clock = ModelClock(start=START)
        target = START + timedelta(days=7)
        clock.set(target)
        assert clock.now() == target

    def test_set_в_прошлое_запрещён(self):
        """Откат времени ломает расчёт дедлайнов — запрещаем."""
        clock = ModelClock(start=START)
        with pytest.raises(ValueError, match="CLOCK_IN_PAST"):
            clock.set(START - timedelta(hours=1))

    def test_reset_возвращает_старт(self):
        clock = ModelClock(start=START)
        clock.advance(100)
        clock.reset(START)
        assert clock.now() == START

    def test_reset_очищает_листенеры(self):
        clock = ModelClock(start=START)
        clock.register(object())
        clock.reset(START)
        assert clock.advance(24) == []


class TestSingleton:
    def teardown_method(self) -> None:
        set_clock(None)

    def test_get_clock_отдаёт_singleton(self):
        assert get_clock() is get_clock()

    def test_set_clock_подменяет_источник(self, model_clock: ModelClock):
        assert get_clock() is model_clock

    def test_set_clock_none_сбрасывает(self):
        set_clock(None)
        assert isinstance(get_clock(), Clock)

    def test_без_настройки_используется_системное_время(self, monkeypatch):
        """USE_MODEL_CLOCK не задан → боевое время, не модельное."""
        set_clock(None)
        monkeypatch.setenv("USE_MODEL_CLOCK", "false")
        from app.settings import get_settings

        get_settings.cache_clear()
        assert get_clock().is_mock() is False
        get_settings.cache_clear()


class TestClockContract:
    def test_реализации_наследуют_интерфейс(self):
        assert issubclass(SystemClock, Clock)
        assert issubclass(ModelClock, Clock)

    def test_нельзя_создать_абстрактный_clock(self):
        with pytest.raises(TypeError):
            Clock()  # type: ignore[abstract]

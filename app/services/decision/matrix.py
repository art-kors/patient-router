"""Загрузка матрицы маршрутизации из config/routing_matrix.json.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ
====================
Матрица — это ДАННЫЕ, а не код. Заказчик должен иметь возможность
добавить новый триггер или поменять порог, не трогая Python. Это прямое
требование кейса: «на защите могут попросить добавить новый триггер».

Поэтому здесь только чтение и валидация. Никакой логики маршрутизации.

ВАЛИДАЦИЯ
=========
Ошибка в матрице должна обнаруживаться при старте, а не в момент, когда
пациент ждёт уведомления. Поэтому load_triggers() бросает исключение
с понятным текстом, а validate() даёт предупреждения без падения.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.services.decision.thresholds import supports_threshold
from app.settings import get_settings

# Путь по умолчанию: config/routing_matrix.json рядом с приложением.
MATRIX_PATH = Path(get_settings().routing_matrix_path)


@dataclass(frozen=True)
class TriggerDef:
    """Описание одного триггера из матрицы.

    Загружается один раз при старте и только читается. Изменять
    матрицу на лету нельзя — это делает новый релиз или новая версия
    файла (поле version в объяснимости покажет, по какой редакции
    было принято решение).
    """

    trigger_id: str
    display_name: str
    source_study: str
    synonyms: tuple[str, ...] = ()
    negative_contexts: tuple[str, ...] = ()
    thresholds: dict = field(default_factory=dict)
    specialty: str = ""
    potential_route: str = ""
    target_sla_days: int = 14
    department: str = ""
    priority: int = 3
    emergency_flag: bool = False
    version: int = 1

    def matches(self, text: str) -> bool:
        """Есть ли в тексте хоть один синоним этой находки.

        Слово — подстрока, регистр игнорируется. Этого достаточно
        для большинства формулировок; полноценная морфология —
        задача настоящего декодера.
        """
        lowered = text.lower()
        return any(s.lower() in lowered for s in self.synonyms)

    def threshold_value(self, key: str) -> float | None:
        """Числовой порог по ключу, если он задан."""
        value = self.thresholds.get(key)
        return float(value) if isinstance(value, (int, float)) else None


class MatrixError(ValueError):
    """Матрица повреждена или отсутствует. Фатально при старте."""


def load_triggers(path: Path | None = None) -> list[TriggerDef]:
    """Прочитать матрицу. Бросает MatrixError, если файла нет или он битый.

    Намеренно строго: лучше не стартовать, чем маршрутизировать
    пациентов по неполной матрице.
    """
    if path is None:
        from app.services.decision.matrix_store import database_triggers

        stored = database_triggers()
        if stored is not None:
            return stored
    target = Path(path) if path else MATRIX_PATH
    if not target.exists():
        raise MatrixError(
            f"Матрица маршрутизации не найдена: {target}. "
            "Ожидается файл config/routing_matrix.json со списком триггеров."
        )

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MatrixError(f"{target}: некорректный JSON ({exc})") from exc
    except OSError as exc:
        raise MatrixError(f"{target}: не удалось прочитать ({exc})") from exc

    items = raw if isinstance(raw, list) else raw.get("triggers", [])
    if not items:
        raise MatrixError(f"{target}: список триггеров пуст")

    triggers: list[TriggerDef] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        missing = [k for k in ("trigger_id", "display_name") if not item.get(k)]
        if missing:
            raise MatrixError(f"{target}: триггер #{index} без полей {missing}")
        if item["trigger_id"] in seen:
            raise MatrixError(f"{target}: дубль trigger_id «{item['trigger_id']}»")
        seen.add(item["trigger_id"])
        triggers.append(_build(item))
    return triggers


def _build(item: dict) -> TriggerDef:
    """Собрать TriggerDef из словаря конфига."""
    return TriggerDef(
        trigger_id=item["trigger_id"],
        display_name=item["display_name"],
        source_study=item.get("source_study", ""),
        synonyms=tuple(item.get("synonyms") or ()),
        negative_contexts=tuple(item.get("negative_contexts") or ()),
        thresholds=dict(item.get("thresholds") or {}),
        specialty=item.get("specialty", ""),
        potential_route=item.get("potential_route", ""),
        target_sla_days=int(item.get("target_sla_days", 14)),
        department=item.get("department", ""),
        priority=int(item.get("priority", 3)),
        emergency_flag=bool(item.get("emergency_flag", False)),
        version=int(item.get("version", 1)),
    )


def validate(triggers: list[TriggerDef]) -> list[str]:
    """Проверить матрицу и вернуть список предупреждений.

    Не бросает исключений: это подсказка для врача, а не отказ.
    Типичные проблемы — синоним без отрицания (риск ложных
    срабатываний) и неизвестная специальность.
    """
    warnings: list[str] = []

    for trigger in triggers:
        if not trigger.synonyms:
            warnings.append(f"{trigger.trigger_id}: нет синонимов — триггер не сработает")

        # Каждый синоним должен быть покрыт отрицанием, иначе норма
        # («полипа не выявлено») даст ложное срабатывание.
        lowered_negatives = " ".join(trigger.negative_contexts).lower()
        for synonym in trigger.synonyms:
            if lowered_negatives and not any(
                part in lowered_negatives for part in synonym.lower().split()[:2]
            ):
                warnings.append(f"{trigger.trigger_id}: синоним «{synonym}» не покрыт отрицанием")
                break  # по одному предупреждению на триггер — достаточно

        if not trigger.thresholds and trigger.priority <= 2:
            warnings.append(
                f"{trigger.trigger_id}: приоритет {trigger.priority}, но пороги не заданы — "
                "возможны ложные срабатывания на нормах"
            )
        # Порог, который движок не умеет проверять, — это правило без
        # проверки. Именно так появился баг с min_stenosis_percent.
        for key in sorted(trigger.thresholds):
            if not supports_threshold(key):
                warnings.append(
                    f"{trigger.trigger_id}: порог «{key}» не поддерживается движком — "
                    "триггер никогда не сработает, добавьте поддержку в thresholds.py"
                )
        if trigger.emergency_flag and trigger.priority != 1:
            warnings.append(
                f"{trigger.trigger_id}: emergency_flag=true, но priority={trigger.priority} — "
                "ожидается 1"
            )

    return warnings

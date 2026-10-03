"""Слой решения: матрица маршрутизации и детерминированный движок.

Здесь нет и не должно быть ни LLM, ни эвристик. Только данные (матрица)
и правила, проверенные медицинским экспертом.
"""

from app.services.decision.engine import DecisionEngine, DecisionResult, TriggerMatch
from app.services.decision.matrix import (
    MATRIX_PATH,
    MatrixError,
    TriggerDef,
    load_triggers,
    validate,
)

__all__ = [
    "DecisionEngine",
    "DecisionResult",
    "MATRIX_PATH",
    "MatrixError",
    "TriggerDef",
    "TriggerMatch",
    "load_triggers",
    "validate",
]

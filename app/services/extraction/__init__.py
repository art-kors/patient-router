"""Извлечение фактов из протокола.

Здесь живёт контракт декодера (base.py) и рабочая заглушка
(dictionary_extractor.py). Сокомандник подключает свою реализацию,
переопределив get_extractor().
"""

from app.services.extraction.base import (
    ExtractionResult,
    Finding,
    ProtocolExtractor,
    ProtocolMeta,
    validate_extractor,
)
from app.services.extraction.dictionary_extractor import DictionaryExtractor

_extractor: ProtocolExtractor | None = None


def get_extractor() -> ProtocolExtractor:
    """Декодер, используемый приложением.

    ЗАМЕНИТЬ ЗДЕСЬ: когда сокомандник пришлёт свою реализацию,
    достаточно вернуть её отсюда. Больше нигде менять не нужно.

        def get_extector() -> ProtocolExtractor:
            return LlmExtractor()  # вместо DictionaryExtractor()
    """
    global _extractor
    if _extractor is None:
        _extractor = DictionaryExtractor()
        validate_extractor(_extractor)
    return _extractor


def set_extractor(extractor: ProtocolExtractor) -> None:
    """Подменить декодер (для тестов и сравнения реализаций)."""
    global _extractor
    validate_extractor(extractor)
    _extractor = extractor


__all__ = [
    "DictionaryExtractor",
    "ExtractionResult",
    "Finding",
    "ProtocolExtractor",
    "ProtocolMeta",
    "get_extractor",
    "set_extractor",
    "validate_extractor",
]

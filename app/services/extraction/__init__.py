"""Контракт извлечения и регистрация адаптера пакета doc_processing.

Словарный экстрактор доступен для сравнения и совместимых сценариев.
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
    """Создать декодер по настройкам без соединения с сервером модели."""
    global _extractor
    if _extractor is None:
        from app.settings import get_settings
        from doc_processing.adapter import TeammateExtractor
        from doc_processing.llm.client import OllamaClient

        settings = get_settings()
        client = None
        if settings.decoder_mode == "llm":
            client = OllamaClient(
                base_url=settings.ollama_url,
                model=settings.ollama_model,
                timeout=settings.ollama_timeout,
            )
        _extractor = TeammateExtractor(client=client, fallback=settings.decoder_fallback)
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

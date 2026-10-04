"""Правиловый разбор протоколов и извлечение через локальную модель."""

from .runner import ensure_ollama, process_report

__all__ = ["ensure_ollama", "process_report"]

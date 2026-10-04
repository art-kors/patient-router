"""Utilities for deterministic parsing and local LLM extraction."""

from .runner import ensure_ollama, process_report

__all__ = [
	"ensure_ollama",
	"process_report",
]

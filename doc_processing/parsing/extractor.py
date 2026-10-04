from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

BOILERPLATE_MARKERS = (
    "Данное заключение не является",
    "Уважаемые пациенты!",
    "Интерпретацию результатов исследования",
    "Исследование выполнено на УЗ-сканере",
)
RECOMMENDATION_PATTERN = re.compile(
    r"\b(?:Рекомендовано|Рекомендована|Рекомендации)\s*:\s*",
    flags=re.IGNORECASE,
)
CONCLUSION_PREFIX_PATTERN = re.compile(
    r"^\s*заключение\s*[:.\-]?\s*",
    flags=re.IGNORECASE,
)


class ParsedDocumentError(ValueError):
    """Ошибка формата структурированного протокола."""


def normalize_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def remove_boilerplate(text: str) -> str:
    positions = [
        position
        for marker in BOILERPLATE_MARKERS
        if (position := text.casefold().find(marker.casefold())) >= 0
    ]
    if positions:
        text = text[: min(positions)]
    return text.strip(" \t\r\n")


def make_report_id(relative_path: Path | str) -> str:
    path_text = Path(relative_path).as_posix()
    digest = hashlib.sha256(path_text.encode("utf-8")).hexdigest()
    return digest[:16]


def transform_document(
    payload: dict[str, Any],
    report_id: str,
) -> dict[str, Any]:
    """Преобразовать структурированный протокол в упорядоченные клинические блоки."""
    if not isinstance(payload, dict):
        raise ParsedDocumentError("Верхний уровень JSON должен быть объектом.")

    sections = payload.get("sections")
    if not isinstance(sections, list):
        raise ParsedDocumentError(
            "Ожидается JSON parser_humanized со списком sections. "
            "JSON только с таблицами не поддерживается."
        )

    study = payload.get("study")
    study_type = study.get("type") if isinstance(study, dict) else None
    blocks: list[dict[str, str]] = []
    recommendations: list[str] = []

    for section in sections:
        if not isinstance(section, dict):
            continue

        section_name = normalize_whitespace(section.get("name")) or "unlabeled"
        if section_name.casefold() == "general":
            continue

        lines = section.get("lines")
        if not isinstance(lines, list) or not any(normalize_whitespace(line) for line in lines):
            section_text = section.get("text", "")
            lines = [section_text] if section_text else []

        clinical_lines: list[str] = []
        for raw_line in lines:
            text = normalize_whitespace(raw_line)
            if not text:
                continue

            recommendation_match = RECOMMENDATION_PATTERN.search(text)
            if recommendation_match:
                recommendation = remove_boilerplate(text[recommendation_match.end() :])
                if recommendation:
                    recommendations.append(recommendation)
                text = text[: recommendation_match.start()]

            text = remove_boilerplate(text)
            if section_name.casefold() == "conclusion":
                text = CONCLUSION_PREFIX_PATTERN.sub("", text)
            text = normalize_whitespace(text)
            if text:
                clinical_lines.append(text)

        if not clinical_lines:
            continue

        blocks.append(
            {
                "block_id": f"b{len(blocks) + 1:03d}",
                "section": section_name,
                "source": (
                    "conclusion" if section_name.casefold() == "conclusion" else "description"
                ),
                "text": "\n".join(clinical_lines),
            }
        )

    return {
        "schema_version": "1.0",
        "report_id": report_id,
        "study_type": normalize_whitespace(study_type) or None,
        "blocks": blocks,
        "recommendations": recommendations,
    }

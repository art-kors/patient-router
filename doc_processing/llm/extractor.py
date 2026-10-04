from __future__ import annotations

import copy
import re
from typing import Any

_ALLOWED_STUDY_KEYS = {"type"}
_REMOVED_KEYS = {
    "file",
    "patient_name",
    "patient_age",
    "patient_sex",
    "doctor",
    "date",
    "report_id",
    "diagnostics",
    "findings",
    "negations",
    "conclusion_text",
}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return re.sub(r"\s+", " ", text)


def prepare_document_for_llm(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise TypeError("Входные данные модели должны быть объектом JSON.")

    prepared = copy.deepcopy(document)

    if isinstance(prepared.get("study"), dict):
        prepared["study"] = {
            key: value for key, value in prepared["study"].items() if key in _ALLOWED_STUDY_KEYS
        }

    for key in list(prepared.keys()):
        if key in _REMOVED_KEYS:
            prepared.pop(key, None)

    sections: list[dict[str, Any]] = []
    for section in prepared.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        section_name = _clean_text(section.get("name"))
        if not section_name or section_name.lower() == "general":
            continue

        raw_lines = section.get("lines") or [section.get("text")]
        cleaned_lines: list[str] = []
        for line in raw_lines:
            text = str(line).strip() if line is not None else ""
            if not text:
                continue
            lowered = text.lower()
            if "рекоменд" in lowered or "заключение" in lowered and "не является" in lowered:
                continue
            cleaned_lines.append(text)

        if not cleaned_lines:
            continue

        sections.append({"name": section_name, "lines": cleaned_lines})

    prepared["sections"] = sections
    return prepared


def normalize_findings_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"findings": []}

    findings = response.get("findings")
    if not isinstance(findings, list):
        return {"findings": []}

    normalized: list[dict[str, str]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        section = _clean_text(item.get("section") or item.get("organ") or "unknown")
        finding = _clean_text(item.get("finding") or item.get("term") or item.get("name") or "")
        quote = item.get("quote")
        if not isinstance(quote, str):
            quote = ""
        certainty = _clean_text(item.get("certainty") or item.get("status") or "confirmed").lower()
        laterality = _clean_text(item.get("laterality") or item.get("side") or "")

        if not finding and not quote:
            continue

        normalized.append(
            {
                "section": section or "unknown",
                "finding": finding or quote,
                "quote": quote,
                "certainty": certainty
                if certainty in {"confirmed", "suggested", "negated"}
                else "confirmed",
                "laterality": laterality,
            }
        )

    return {"findings": normalized}

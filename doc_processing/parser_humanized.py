from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from .parser import parse_docx

SECTION_HEADERS: dict[str, list[str]] = {
    "матка": [r"^\s*матка\b", r"^\s*матка\s*:"],
    "эндометрий": [r"\bэндометрий\b"],
    "шейка матки": [r"^\s*шейка матки\b", r"^\s*шейка матки\s*:"],
    "правый яичник": [r"^\s*правый яичник\b", r"^\s*правый яичник\s*:"],
    "левый яичник": [r"^\s*левый яичник\b", r"^\s*левый яичник\s*:"],
    "щитовидная железа": [r"^\s*щитовидная железа\b", r"^\s*щитовидная железа\s*:"],
    "правая доля щж": [r"^\s*правая доля\b", r"^\s*правая доля\s*щж\b"],
    "левая доля щж": [r"^\s*левая доля\b", r"^\s*левая доля\s*щж\b"],
    "перешеек": [r"^\s*перешеек\b", r"^\s*перешеек\s*:"],
    "печень": [r"^\s*печень\b", r"^\s*печень\s*:"],
    "желчный пузырь": [r"^\s*желчный пузырь\b", r"^\s*желчный пузырь\s*:"],
    "поджелудочная железа": [r"^\s*поджелудочная железа\b", r"^\s*поджелудочная железа\s*:"],
    "селезенка": [r"^\s*селезенка\b", r"^\s*селезенка\s*:"],
    "правая молочная железа": [r"^\s*правая молочная железа\b", r"^\s*правая молочная железа\s*:"],
    "левая молочная железа": [r"^\s*левая молочная железа\b", r"^\s*левая молочная железа\s*:"],
    "предстательная железа": [r"^\s*предстательная железа\b", r"^\s*предстательная железа\s*:"],
    "справа (сосуды нк)": [r"^\s*справа\s*:?\s*$", r"^\s*справа\s*:\s*"],
    "слева (сосуды нк)": [r"^\s*слева\s*:?\s*$", r"^\s*слева\s*:\s*"],
}

FINDING_TERMS: dict[str, list[str]] = {
    "киста": [r"\bкиста\b", r"\bкист(?:а|ы|ой|у)\b"],
    "полип": [r"\bполип\b", r"\bполип(?:а|ы|ов)\b"],
    "фиброаденома": [r"\bфиброаденом(?:а|ы|ой)\b"],
    "миома": [r"\bмиом(?:а|ы|ой|у)\b"],
    "узел": [r"\bузел(?:ы|ов|овой|ов)?\b", r"\bузлов\w*\s+образован\w+\b", r"\bобразован\w+\b"],
    "кальцинат": [r"\bкальцинат(?:ы|ов|а|ы)?\b", r"\bкальциноз\b"],
    "атеросклеротическая бляшка": [r"\bатеросклеротич\w*\s+бляшк\w*\b", r"\bасб\b"],
    "диффузные изменения": [r"\bдиффузн\w*\s+изменен\w*\b"],
    "мастопатия": [r"\bмастопат\w*\b"],
    "атеросклероз": [r"\baterосклероз\b", r"\bатеросклероз\b"],
    "свободная жидкость": [r"\bсвободн\w*\s+жидкост\w*\b", r"\bжидкость\b"],
    "лимфоузел увеличен": [r"\bлимфоузел\w*\s+увелич\w*\b"],
    "расширение протока": [r"\bрасширен\w*\s+проток\w*\b"],
    "микрокальциноз": [r"\bмикрокальциноз\b"],
    "очаговое образование": [r"\bочагов\w*\s+образован\w*\b", r"\bобразован\w*\s+в\s+пределах\b"],
}

NEGATION_PATTERNS: list[str] = [
    r"не\s+определя\w*",
    r"не\s+выявл\w*",
    r"не\s+визуализ\w*",
    r"не\s+расшир\w*",
    r"без\s+особенност\w*",
    r"не\s+измен\w*",
    r"отсутств\w*",
    r"без\s+\w+",
    r"в\s+пределах\s+норм\w*",
    r"не\s+выражен\w*",
]

STUDY_TYPE_MAP = {
    "ОМТ": "УЗИ органов малого таза",
    "Ж ОМТ": "УЗИ органов малого таза",
    "ЩИТОВИДНАЯ": "УЗИ щитовидной железы",
    "ЩИТОВИДКА": "УЗИ щитовидной железы",
    "МОЛОЧНАЯ": "УЗИ молочных желез",
    "МОЧЕВЫХ ПУТЕЙ": "УЗИ органов малого таза",
    "ПРЕДСТАТЕЛЬНАЯ": "УЗИ предстательной железы",
    "ПРОСТАТА": "УЗИ предстательной железы",
    "ВЕНЫ": "УЗИ вен нижних конечностей",
    "НИЖНИЕ КОНЕЧНОСТИ": "УЗИ вен нижних конечностей",
    "ЖП": "УЗИ органов брюшной полости",
    "БРЮШНОЙ ПОЛОСТИ": "УЗИ органов брюшной полости",
}


def normalize_space(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = normalize_space(value)
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def calculate_age(birth_date: str | None, study_date: str | None) -> int | None:
    birth = parse_date(birth_date)
    study = parse_date(study_date)
    if birth is None or study is None:
        return None
    age = study.year - birth.year
    if (study.month, study.day) < (birth.month, birth.day):
        age -= 1
    return max(0, age)


def study_type_from_text(text: str | None, file_path: Path | str | None = None) -> str:
    if text:
        upper = text.upper()
        for key, value in STUDY_TYPE_MAP.items():
            if key in upper:
                return value

    if file_path is not None:
        path = Path(file_path)
        parent_name = path.parent.name.upper()
        for key, value in STUDY_TYPE_MAP.items():
            if key in parent_name:
                return value

    return "УЗИ исследование"


def flatten_document(parsed: dict[str, Any]) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    flat_text = ""

    for table_index, table in enumerate(parsed.get("tables", [])):
        for row_index, row in enumerate(table.get("rows", [])):
            for column_index, cell in enumerate(row.get("cells", [])):
                text = normalize_space(cell.get("text", ""))
                if not text:
                    continue

                start = len(flat_text)
                flat_text += text + " "
                end = len(flat_text) - 1

                cells.append(
                    {
                        "table_index": table_index,
                        "row": row_index,
                        "column": column_index,
                        "text": text,
                        "char_start": start,
                        "char_end": end,
                    }
                )

    return {
        "flat_text": flat_text.strip(),
        "cells": cells,
    }


def extract_study(flat: dict[str, Any], file_path: str | Path) -> dict[str, Any]:
    file_path = Path(file_path)
    cells = flat["cells"]

    study: dict[str, Any] = {
        "type": "УЗИ исследование",
        "date": None,
        "doctor": None,
        "patient_name": None,
        "patient_age": None,
        "patient_sex": None,
    }

    found_title = None
    for cell in cells:
        text = cell["text"]
        if re.search(r"УЛЬТРАЗВУКОВОЕ|ИССЛЕДОВАНИЕ|УЗИ", text, re.I):
            found_title = text
            break

    study["type"] = study_type_from_text(found_title, file_path)

    for cell in cells:
        text = cell["text"]
        if re.search(r"Дата\s*приема", text, re.I):
            study["date"] = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            if study["date"]:
                study["date"] = study["date"].group(1)
            for sibling in cells:
                if sibling["row"] == cell["row"] and sibling["column"] == cell["column"] + 1:
                    value = sibling["text"]
                    if re.search(r"\d{2}\.\d{2}\.\d{4}", value):
                        study["date"] = re.search(r"(\d{2}\.\d{2}\.\d{4})", value).group(1)
                        break
        if re.search(r"Врач", text, re.I):
            for sibling in cells:
                if sibling["row"] == cell["row"] and sibling["column"] == cell["column"] + 1:
                    study["doctor"] = sibling["text"]
                    break
        if re.search(r"ФИО\s*пациент", text, re.I):
            for sibling in cells:
                if sibling["row"] == cell["row"] and sibling["column"] == cell["column"] + 1:
                    study["patient_name"] = sibling["text"]
                    break
        if re.search(r"Дата\s*рождения", text, re.I):
            for sibling in cells:
                if sibling["row"] == cell["row"] and sibling["column"] == cell["column"] + 1:
                    study["patient_age"] = calculate_age(sibling["text"], study["date"])
                    break
        if re.search(r"Возраст\s*на\s*момент\s*осмотра", text, re.I):
            for sibling in cells:
                if sibling["row"] == cell["row"] and sibling["column"] == cell["column"] + 1:
                    v = re.search(r"(\d+)", sibling["text"])
                    if v:
                        study["patient_age"] = int(v.group(1))
                    break

    if study["patient_age"] is None:
        for cell in cells:
            if re.search(r"Возраст", cell["text"], re.I):
                m = re.search(r"(\d+)", cell["text"])
                if m:
                    study["patient_age"] = int(m.group(1))
                    break

    if study["patient_name"]:
        name = study["patient_name"].strip()
        if re.search(r"[а-яА-Я]а$|[а-яА-Я]я$|[а-яА-Я]на$|[а-яА-Я]ва$", name):
            study["patient_sex"] = "F"
        else:
            study["patient_sex"] = "M"
    elif "молочная" in study["type"].lower() or "малого таза" in study["type"].lower():
        study["patient_sex"] = "F"
    elif "предстательной" in study["type"].lower():
        study["patient_sex"] = "M"

    return study


def match_section_header(text: str) -> str | None:
    cleaned = normalize_space(text)
    if not cleaned:
        return None

    for section_name, patterns in SECTION_HEADERS.items():
        for pattern in patterns:
            if re.search(pattern, cleaned, flags=re.IGNORECASE):
                return section_name
    return None


def is_service_marker(text: str) -> bool:
    return bool(
        re.search(
            r"^(?:ЗАКЛЮЧЕНИЕ|Рекомендовано|ORADS|TI-RADS|BI-RADS|"
            r"Данное заключение не является|ДАННОЕ ЗАКЛЮЧЕНИЕ НЕ ЯВЛЯЕТСЯ)",
            text,
            flags=re.IGNORECASE,
        )
    )


def split_sections(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {"general": []}
    current_name = "general"

    for cell in cells:
        text = normalize_space(cell["text"])
        if not text:
            continue

        header = match_section_header(text)
        if header:
            current_name = header
            grouped.setdefault(current_name, [])
            grouped[current_name].append(text)
            continue

        if is_service_marker(text):
            current_name = "conclusion"
            grouped.setdefault(current_name, [])
            grouped[current_name].append(text)
            continue

        grouped.setdefault(current_name, []).append(text)

    sections: list[dict[str, Any]] = []
    for name, texts in grouped.items():
        if not texts:
            continue
        sections.append(
            {
                "name": name,
                "present": True,
                "lines": texts,
                "text": " ".join(texts),
                "findings_count": 0,
            }
        )

    return sections


def laterality_from_text(text: str) -> str | None:
    lowered = text.lower()
    if "справа" in lowered or "правая" in lowered or "правый" in lowered:
        return "right"
    if "слева" in lowered or "левая" in lowered or "левый" in lowered:
        return "left"
    return None


def size_mm_from_text(text: str) -> float | None:
    patterns = [
        r"(\d+(?:[.,]\d+)?)\s*(?:x|х|\*|×)\s*(\d+(?:[.,]\d+)?)\s*(?:мм|mm)",
        r"(\d+(?:[.,]\d+)?)\s*(?:x|х|\*|×)\s*(\d+(?:[.,]\d+)?)\s*(?:см)",
        r"(\d+(?:[.,]\d+)?)\s*(?:мм|mm)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        if len(match.groups()) == 2:
            left = float(match.group(1).replace(",", "."))
            right = float(match.group(2).replace(",", "."))
            unit = re.search(r"(?:см|mm|мм)", text, flags=re.IGNORECASE)
            if unit and unit.group(0).lower() in {"см", "cm"}:
                return max(left, right) * 10
            return max(left, right)
        value = float(match.group(1).replace(",", "."))
        if re.search(r"см", text, flags=re.IGNORECASE):
            return value * 10
        return value
    return None


def classification_from_text(text: str) -> str | None:
    for label in ("ORADS", "TI-RADS", "BI-RADS"):
        match = re.search(rf"{label}\s*(\d+)", text, flags=re.IGNORECASE)
        if match:
            return f"{label} {match.group(1)}"
    return None


def _term_match(text: str) -> str | None:
    normalized = normalize_space(text)
    if not normalized:
        return None

    best = None
    best_score = -1
    for term, patterns in FINDING_TERMS.items():
        for pattern in patterns:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                score = len(term)
                if score > best_score:
                    best_score = score
                    best = term
                break
    return best


def add_unique(records: list[dict[str, Any]], item: dict[str, Any], dedupe_key: str) -> None:
    normalized_item = dict(item)
    for index, current in enumerate(records):
        if current.get("dedupe_key") == dedupe_key:
            if normalized_item["source"] == "заключение" and current["source"] != "заключение":
                records[index] = {**normalized_item, "dedupe_key": dedupe_key}
            return
    records.append({**normalized_item, "dedupe_key": dedupe_key})


def strip_internal_fields(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item.pop("dedupe_key", None)
        cleaned.append(item)
    return cleaned


def extract_findings(
    section_records: list[dict[str, Any]], conclusion_text: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    negations: list[dict[str, Any]] = []

    def process_text(value: str, source: str, section_name: str) -> None:
        text = normalize_space(value)
        if not text:
            return

        term = _term_match(text)
        if not term:
            return

        negation_reason = None
        for pattern in NEGATION_PATTERNS:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                negation_reason = normalize_space(match.group(0))
                break

        if negation_reason:
            add_unique(
                negations,
                {
                    "term": term,
                    "section": section_name,
                    "source": source,
                    "reason": negation_reason,
                    "quote": text,
                    "laterality": laterality_from_text(text),
                },
                f"neg:{term}",
            )
            return

        item = {
            "term": term,
            "section": section_name,
            "source": source,
            "laterality": laterality_from_text(text),
            "size_mm": size_mm_from_text(text),
            "classification": classification_from_text(text),
            "quote": text,
            "confidence": 0.95 if source == "заключение" else 0.88,
        }
        add_unique(
            findings,
            item,
            f"find:{term}",
        )

    for section in section_records:
        if section["name"] == "conclusion":
            continue
        for line in section.get("lines", [section.get("text", "")]):
            process_text(line, "описание", section["name"])

    for line in re.split(r"\s*\n\s*|\s*\.\s*", conclusion_text):
        text = normalize_space(line)
        if not text:
            continue
        process_text(text, "заключение", "conclusion")

    return findings, negations


def parse_conclusion(cells: list[dict[str, Any]]) -> str:
    fragments: list[str] = []
    in_conclusion = False

    for cell in cells:
        text = normalize_space(cell["text"])
        if not text:
            continue

        if re.search(r"^ЗАКЛЮЧЕНИЕ\b", text, flags=re.IGNORECASE):
            in_conclusion = True
            text = re.sub(r"^ЗАКЛЮЧЕНИЕ\s*[:.-]?\s*", "", text, flags=re.IGNORECASE)
            if text:
                fragments.append(text)
            continue

        if in_conclusion:
            if is_service_marker(text):
                break
            if re.search(
                r"^(?:Рекомендовано|ORADS|TI-RADS|BI-RADS|Данное заключение не является)",
                text,
                flags=re.IGNORECASE,
            ):
                break
            fragments.append(text)

    return " ".join(fragments).strip()


def parse_one(file_path: str | Path) -> dict[str, Any]:
    file_path = Path(file_path)
    parsed = parse_docx(file_path)
    flat = flatten_document(parsed)
    study = extract_study(flat, file_path)
    cells = flat["cells"]
    sections = split_sections(cells)
    conclusion_text = parse_conclusion(cells)
    findings, negations = extract_findings(sections, conclusion_text)
    findings = strip_internal_fields(findings)
    negations = strip_internal_fields(negations)

    diagnostics = {
        "has_study_title": bool(study["type"] and study["type"] != "УЗИ исследование"),
        "has_study_date": bool(study["date"]),
        "has_doctor": bool(study["doctor"]),
        "has_patient_age": bool(study["patient_age"] is not None),
        "has_patient_sex": bool(study["patient_sex"]),
        "sections_count": len(sections),
        "findings_count": len(findings),
        "negations_count": len(negations),
    }

    return {
        "file": file_path.name,
        "study": study,
        "sections": sections,
        "findings": findings,
        "negations": negations,
        "conclusion_text": conclusion_text,
        "diagnostics": diagnostics,
    }


def parse_directory_humanized(input_dir: str | Path, output_dir: str | Path) -> list[Path]:
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)

    if not input_dir.exists():
        raise FileNotFoundError(f"Каталог ввода не существует: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Путь ввода не является каталогом: {input_dir}")

    docx_files = sorted(input_dir.rglob("*.docx"))
    written: list[Path] = []

    for docx_path in docx_files:
        relative_path = docx_path.relative_to(input_dir)
        output_path = (output_dir / relative_path).with_suffix(".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        result = parse_one(docx_path)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        written.append(output_path)

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Структурированный разбор протоколов DOCX")
    parser.add_argument("--input-dir", default="protocols", help="Каталог с файлами DOCX")
    parser.add_argument(
        "--output-dir", default="output_humanized", help="Каталог для результатов JSON"
    )
    args = parser.parse_args()

    parse_directory_humanized(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()

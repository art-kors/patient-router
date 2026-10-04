from __future__ import annotations

from typing import Any


def build_findings_prompt(document: dict[str, Any]) -> str:
    sections = document.get("sections") or []
    study = document.get("study") or {}
    study_type = study.get("type") or "исследование"

    safe_sections = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name") or "section").strip()
        lines = section.get("lines") or [section.get("text")]
        cleaned_lines = []
        for line in lines:
            if line is None:
                continue
            text = str(line).strip()
            if text:
                cleaned_lines.append(text)
        if cleaned_lines:
            safe_sections.append({"name": name, "lines": cleaned_lines})

    prompt = f"""Вы — извлекатель клинических находок из протокола УЗИ.

Задача: найти только явные клинические находки, а не диагноз, рекомендации, фамилии или
административные данные.

Правила:
- Выход должен быть строго JSON со структурой {{"findings": [...]}}.
- Каждый элемент findings должен содержать минимум: section, finding, quote, certainty.
- section — название секции протокола, например: "матка", "щитовидная железа", "печень".
- finding — короткая и точная формулировка находки: "узел", "киста", "атеросклеротическая бляшка",
"диффузные изменения".
- quote — точная фраза из текста протокола, которую подтверждает находку.
- certainty — одно из: "confirmed", "suggested", "negated".
- Не добавляйте рекомендации, план лечения, итоговый диагноз и личные данные пациента.
- Если находок нет, верните {{"findings": []}}.
- Оставляйте только клинически значимые находки; пропускайте сведения о пациенте, имена, врача,
идентификаторы, даты, имена файлов и административный текст.

Тип исследования: {study_type}

Текст для анализа:
{safe_sections}
"""
    return prompt

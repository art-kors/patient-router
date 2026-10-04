"""Консервативный разбор однозначных числовых условий врача."""

import re
from dataclasses import dataclass, field

_NUMBER = r"\d+(?:[.,]\d+)?"
_CONDITION = re.compile(
    rf"(?:наличие\s+)?(?P<metric>размер|диаметр(?:\s+БПВ)?|стеноз(?:ы|ов)?)\s*"
    rf"(?P<operator>>=|<=|≥|≤|>|<|до|не менее|не более)\s*"
    rf"(?P<value>{_NUMBER})\s*(?P<unit>мм|%)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedCondition:
    """Пороги либо конкретная причина сохранения исходного текста."""

    thresholds: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def parse_threshold_text(text: str) -> ParsedCondition:
    """Разобрать всё условие целиком, не отбрасывая альтернативы и атрибуты.

    «До» означает включительную верхнюю границу. Диапазон после знака
    сравнения неоднозначен: выбрать одну из границ может только врач.
    """
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ParsedCondition(reason="Условие не задано")
    if re.search(rf"{_NUMBER}\s*[-–—]\s*{_NUMBER}\s*(?:мм|%)", normalized):
        return ParsedCondition(reason="Диапазон требует выбора границы и смысла сравнения врачом")
    if "/" in normalized or re.search(r"\bили\b|\bи\b|,\s*[а-я]", normalized, re.I):
        return ParsedCondition(
            reason="Составное условие: требуется определить связь и проверку всех частей"
        )
    match = _CONDITION.fullmatch(normalized)
    if not match:
        return ParsedCondition(
            reason="Нет однозначного числового сравнения; требуется проверка клинических признаков"
        )
    stenosis = match["metric"].lower().startswith("стеноз")
    if (stenosis and match["unit"] != "%") or (not stenosis and match["unit"].lower() != "мм"):
        return ParsedCondition(reason="Единица не соответствует измеряемой величине")
    value = float(match["value"].replace(",", "."))
    if stenosis and value > 100:
        return ParsedCondition(reason="Стеноз должен находиться в пределах от 0 до 100 %")
    prefix = {
        ">": "gt",
        "<": "lt",
        ">=": "min",
        "≥": "min",
        "<=": "max",
        "≤": "max",
        "до": "max",
        "не менее": "min",
        "не более": "max",
    }[match["operator"].lower()]
    metric = "stenosis_percent" if stenosis else "size_mm"
    return ParsedCondition({f"{prefix}_{metric}": value})

"""Адаптация результатов сокомандника к контракту приложения."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from app.services.extraction.base import ExtractionResult, Finding, ProtocolExtractor, ProtocolMeta
from app.services.extraction.dictionary_extractor import (
    DictionaryExtractor,
    _is_negative,
    _next_statement,
)
from doc_processing.llm.client import OllamaClient, OllamaExtractionError
from doc_processing.parser_humanized import (
    classification_from_text,
    extract_findings,
    match_section_header,
    size_mm_from_text,
)

logger = logging.getLogger(__name__)


def text_document(text: str, study_type: str | None) -> dict[str, Any]:
    """Собрать секции текстового протокола без изменения исходных строк."""
    sections = []
    current = {"name": "описание", "lines": []}
    sections.append(current)
    for line in text.splitlines():
        marker = re.match(r"\s*(?:заключение|диагноз)\b", line, re.IGNORECASE)
        header = "conclusion" if marker else match_section_header(line)
        if header and header != current["name"]:
            current = {"name": header, "lines": []}
            sections.append(current)
        current["lines"].append(line)
    return {"study": {"type": study_type}, "sections": sections}


class TeammateExtractor(ProtocolExtractor):
    """Правила сокомандника с совместимым словарным резервом; LLM — явный режим.

    Словарь сохраняет уже поддержанные формулировки и отрицания матрицы.
    При любой ошибке модели разрешённый откат запускает правила и сообщает
    причину в результате. При отключённом откате ошибка передаётся вызывающему коду.
    """

    def __init__(self, *, client: OllamaClient | None = None, fallback: bool = True) -> None:
        self.client = client
        self.fallback = fallback
        self.dictionary = DictionaryExtractor()
        self._negatives = self.dictionary._negatives

    @property
    def name(self) -> str:
        return "TeammateExtractor(LLM)" if self.client else "TeammateExtractor(правила + словарь)"

    def adapt_findings(
        self, text: str, records: list[dict[str, Any]], *, normalized_quotes: bool = False
    ) -> list[Finding]:
        """Отбросить записи без доказательства; смещения вычислить по исходному тексту.

        Только правиловый парсер сворачивает пробелы. Для него восстанавливаем
        дословный фрагмент с теми же словами. Цитаты модели ищем строго.
        """
        findings = []
        for item in records:
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote.strip():
                continue
            start = text.find(quote)
            if start < 0 and normalized_quotes:
                pattern = r"\s+".join(re.escape(part) for part in quote.split())
                match = re.search(pattern, text)
                if match:
                    start = match.start()
                    quote = match.group()
            if start < 0:
                continue
            end = start + len(quote)
            if text[start:end] != quote:
                continue
            term = item.get("finding") or item.get("term")
            if not isinstance(term, str) or not term.strip():
                continue
            # Имена матрицы применяем только при явном синониме в доказательстве.
            names = [
                name for synonym, name in self.dictionary._synonyms if synonym in quote.lower()
            ]
            name = names[0] if names else term
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            if line_end < 0:
                line_end = len(text)
            negative = item.get("certainty") == "negated" or bool(item.get("reason"))
            negative |= _is_negative(
                text,
                line_start,
                line_end,
                name,
                self._negatives.get(name, []),
                _next_statement(text, line_end),
            )
            side = item.get("laterality") or None
            findings.append(
                Finding(
                    finding=name,
                    quote=quote,
                    char_start=start,
                    char_end=end,
                    organ=item.get("section"),
                    source_section=item.get("source") or item.get("section"),
                    laterality={"right": "справа", "left": "слева"}.get(side, side),
                    size_mm=size_mm_from_text(quote),
                    classification=classification_from_text(quote),
                    in_negative_context=negative,
                    confidence=0.7 if item.get("certainty") == "suggested" else 0.95,
                )
            )
        return findings

    def extract(self, text: str, *, study_type: str | None = None) -> ExtractionResult:
        """Извлечь находки, сохранив контракт, исходный текст и явный режим."""
        if not text.strip():
            return ExtractionResult(
                raw_text=text,
                meta=ProtocolMeta(study_type=study_type),
                extractor_name=self.name,
                conclusion_extracted=False,
            )
        document = text_document(text, study_type)
        conclusion = "\n".join(
            line
            for section in document["sections"]
            if section["name"] == "conclusion"
            for line in section["lines"]
        )
        if self.client:
            try:
                response = self.client.extract_findings(document)
                if not isinstance(response, dict) or not isinstance(response.get("findings"), list):
                    raise OllamaExtractionError("Ответ модели не содержит списка находок")
                records = response["findings"]
                findings = self.adapt_findings(text, records)
                if not findings:
                    raise OllamaExtractionError(
                        "Ответ модели пуст или не содержит находок с дословной цитатой"
                    )
            except Exception as exc:
                reason = f"Ошибка извлечения моделью ({type(exc).__name__}): {exc}"
                if not self.fallback:
                    raise OllamaExtractionError(f"Откат на правила отключён. {reason}") from exc
                logger.warning("Откат на правила: %s", reason)
                result = TeammateExtractor().extract(text, study_type=study_type)
                return replace(result, decoder_used="rules", llm_error=reason)
        else:
            positive, negative = extract_findings(document["sections"], conclusion)
            adapted = self.adapt_findings(text, positive + negative, normalized_quotes=True)
            result = self.dictionary.extract(text, study_type=study_type)
            findings = list(result.findings)
            seen = {finding.finding for finding in findings}
            for finding in adapted:
                if finding.finding not in seen:
                    findings.append(finding)
                    seen.add(finding.finding)
            # Словарь сохраняет свою область поиска и признак найденного заключения.
            return replace(result, findings=findings, extractor_name=self.name)
        return ExtractionResult(
            meta=ProtocolMeta(study_type=study_type),
            findings=findings,
            conclusion_text=conclusion or text,
            conclusion_extracted=bool(conclusion),
            raw_text=text,
            extractor_name=self.name,
            decoder_used="llm",
        )

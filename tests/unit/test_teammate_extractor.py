"""Контракт адаптера, доказательства и смещения на синтетическом корпусе."""

from pathlib import Path

import pytest

from app.services.decision import DecisionEngine
from app.services.extraction import Finding, ProtocolExtractor, get_extractor
from doc_processing.adapter import TeammateExtractor, text_document
from doc_processing.llm.client import OllamaNotAvailableError
from doc_processing.llm.extractor import normalize_findings_response
from doc_processing.parser_humanized import extract_findings

PROTOCOLS = sorted(Path("data/demo/protocols").glob("*.txt"))


class ResponseClient:
    """Предсказуемый ответ модели без обращения к серверу."""

    def __init__(self, records):
        self.records = records

    def extract_findings(self, document):
        return normalize_findings_response({"findings": self.records})


def test_зарегистрирован_адаптер():
    assert isinstance(get_extractor(), TeammateExtractor)
    assert isinstance(get_extractor(), ProtocolExtractor)


@pytest.mark.parametrize("path", PROTOCOLS, ids=lambda path: path.stem)
def test_дословные_смещения_на_корпусе(path):
    text = path.read_text(encoding="utf-8")
    extractor = TeammateExtractor()
    result = extractor.extract(text)
    for finding in result.findings:
        assert isinstance(finding, Finding)
        assert finding.quote.strip()
        assert 0 <= finding.char_start < finding.char_end <= len(text)
        assert text[finding.char_start : finding.char_end] == finding.quote
    # Проверяем и собственные записи сокомандника, даже если их перекрыл резерв.
    document = text_document(text, None)
    conclusion = "\n".join(
        line
        for section in document["sections"]
        if section["name"] == "conclusion"
        for line in section["lines"]
    )
    positive, negative = extract_findings(document["sections"], conclusion)
    adapted = extractor.adapt_findings(text, positive + negative, normalized_quotes=True)
    for finding in adapted:
        assert text[finding.char_start : finding.char_end] == finding.quote


def test_проверка_корпуса_не_пустая():
    assert len(PROTOCOLS) == 89
    extractor = TeammateExtractor()
    assert sum(len(extractor.extract(path.read_text()).findings) for path in PROTOCOLS) > 0
    records, _ = extract_findings([{"name": "матка", "lines": ["Миома матки 12 мм"]}], "")
    assert extractor.adapt_findings("Миома матки 12 мм", records)


@pytest.mark.parametrize(
    "record",
    [
        {"finding": "миома матки"},
        {"finding": "миома матки", "quote": ""},
        {"finding": "миома матки", "quote": "   "},
        {"finding": "миома матки", "quote": "несуществующая цитата"},
        {"finding": "миома матки", "text": "Миома матки"},
        {"finding": "миома матки", "quote": 123},
    ],
)
def test_ллм_без_дословной_цитаты_отбрасывается(record):
    extractor = TeammateExtractor(client=ResponseClient([record]))
    result = extractor.extract("ЗАКЛЮЧЕНИЕ: Миома матки")
    assert result.decoder_used == "rules"
    assert result.llm_error
    assert all(f.quote.strip() for f in result.findings)


def test_ллм_пробелы_и_повторная_цитата():
    text = "Миома  матки\nЗАКЛЮЧЕНИЕ: Миома  матки"
    extractor = TeammateExtractor(
        client=ResponseClient(
            [
                {"finding": "миома матки", "quote": "Миома  матки"},
                {"finding": "миома матки", "quote": "Миома матки"},
            ]
        )
    )
    findings = extractor.extract(text).findings
    assert len(findings) == 1
    assert findings[0].quote == "Миома  матки"
    assert text[findings[0].char_start : findings[0].char_end] == findings[0].quote


@pytest.mark.parametrize("certainty", ["confirmed", "negated"])
def test_отрицание_не_даёт_триггера(certainty):
    text = "ЗАКЛЮЧЕНИЕ: Полип эндометрия: полипа не выявлено."
    extractor = TeammateExtractor(
        client=ResponseClient(
            [
                {"finding": "полип эндометрия", "quote": text, "certainty": certainty},
            ]
        )
    )
    result = extractor.extract(text, study_type="УЗИ органов малого таза")
    assert len(result.findings) == 1
    assert result.findings[0].in_negative_context
    decision = DecisionEngine().decide(
        result.findings, study_type=result.meta.study_type, conclusion_text=result.conclusion_text
    )
    assert not any(match.fired for match in decision.matches)


def test_отдельный_список_отрицаний_переносится():
    text = "ЗАКЛЮЧЕНИЕ: Полипа не выявлено."
    result = TeammateExtractor().extract(text)
    assert result.findings
    assert all(finding.in_negative_context for finding in result.findings)
    decision = DecisionEngine().decide(result.findings, conclusion_text=result.conclusion_text)
    assert not any(match.fired for match in decision.matches)


def test_ллм_явный_флаг_отрицания():
    text = "Полип эндометрия"
    result = TeammateExtractor(
        client=ResponseClient(
            [
                {"finding": "полип эндометрия", "quote": text, "certainty": "negated"},
            ]
        )
    ).extract(text)
    assert result.findings[0].in_negative_context


def test_недоступная_модель_откатывается_на_правила():
    class UnavailableClient:
        """Клиент с ошибкой соединения."""

        def extract_findings(self, document):
            raise OllamaNotAvailableError("Сервер недоступен")

    result = TeammateExtractor(client=UnavailableClient()).extract("ЗАКЛЮЧЕНИЕ: Миома матки")
    assert result.findings
    assert result.decoder_used == "rules"
    assert "Сервер недоступен" in result.llm_error

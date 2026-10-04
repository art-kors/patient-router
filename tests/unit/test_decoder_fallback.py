"""Проверки отказов модели без сети и загрузки весов."""

from dataclasses import asdict
from unittest.mock import Mock

import pytest

import app.services.extraction as registry
from app.settings import Settings
from doc_processing.adapter import TeammateExtractor
from doc_processing.llm.client import OllamaClient, OllamaExtractionError, OllamaNotAvailableError

TEXT = "ЗАКЛЮЧЕНИЕ: Миома матки 12 мм"


@pytest.fixture
def configured(monkeypatch):
    """Изолировать реестр и настройки от окружения и других тестов."""
    monkeypatch.setattr(registry, "_extractor", None)

    def configure(**kwargs):
        settings = Settings(_env_file=None, **kwargs)
        monkeypatch.setattr("app.settings.get_settings", lambda: settings)
        return registry.get_extractor()

    return configure


@pytest.mark.parametrize(
    "error",
    [OllamaNotAvailableError("Сервер недоступен"), TimeoutError("Таймаут"), RuntimeError("Сбой")],
)
def test_ошибки_модели(configured, monkeypatch, caplog, error):
    extractor = configured(decoder_mode="llm")
    call = Mock(side_effect=error)
    monkeypatch.setattr(extractor.client, "extract_findings", call)
    result = extractor.extract(TEXT)
    assert result.findings
    assert result.decoder_used == "rules"
    assert str(error) in result.llm_error
    assert result.llm_error in caplog.text
    assert "Откат на правила" in caplog.text
    assert asdict(result)["llm_error"]
    call.assert_called_once()


@pytest.mark.parametrize("content", ["не JSON", "", '{"findings": []}', '{"findings": "мусор"}'])
def test_мусор_и_пустота(configured, monkeypatch, content):
    extractor = configured(decoder_mode="llm")
    monkeypatch.setattr(
        extractor.client, "_post", lambda payload: {"message": {"content": content}}
    )
    result = extractor.extract(TEXT)
    assert result.findings
    assert result.decoder_used == "rules"
    assert result.llm_error
    if content == "не JSON":
        assert "невалидный JSON" in result.llm_error


def test_цитата_обязательна(configured, monkeypatch):
    extractor = configured(decoder_mode="llm")
    records = [{"finding": "выдуманная болезнь", "quote": ""}]
    monkeypatch.setattr(
        extractor.client, "extract_findings", lambda document: {"findings": records}
    )
    assert extractor.adapt_findings(TEXT, records) == []
    result = extractor.extract(TEXT)
    assert result.findings
    assert result.decoder_used == "rules"
    assert "цитатой" in result.llm_error
    assert all(f.finding != "выдуманная болезнь" and f.quote.strip() for f in result.findings)


def test_правила_по_умолчанию_без_клиента(configured, monkeypatch):
    constructor = Mock(side_effect=AssertionError("Модель не должна создаваться"))
    call = Mock(side_effect=AssertionError("Модель не должна вызываться"))
    monkeypatch.setattr(OllamaClient, "extract_findings", call)
    monkeypatch.setattr("doc_processing.llm.client.OllamaClient", constructor)
    extractor = configured()
    assert extractor.client is None
    result = extractor.extract(TEXT)
    assert result.findings
    assert result.decoder_used == "rules"
    assert result.llm_error is None
    constructor.assert_not_called()
    call.assert_not_called()


def test_откат_отключён(configured, monkeypatch):
    extractor = configured(decoder_mode="llm", decoder_fallback=False)
    monkeypatch.setattr(
        extractor.client, "extract_findings", Mock(side_effect=RuntimeError("Сбой"))
    )
    with pytest.raises(OllamaExtractionError, match="Откат на правила отключён"):
        extractor.extract(TEXT)


def test_настройки_клиента_без_соединения(configured, monkeypatch):
    network = Mock(side_effect=AssertionError("Сеть запрещена"))
    monkeypatch.setattr("urllib.request.urlopen", network)
    extractor = configured(
        decoder_mode="llm",
        ollama_url="http://example.test",
        ollama_model="пример",
        ollama_timeout=7,
    )
    assert extractor.client.base_url == "http://example.test"
    assert extractor.client.model == "пример"
    assert extractor.client.timeout == 7
    network.assert_not_called()


def test_успешная_модель_прозрачна(monkeypatch):
    client = Mock()
    client.extract_findings.return_value = {
        "findings": [{"finding": "миома матки", "quote": "Миома матки"}]
    }
    result = TeammateExtractor(client=client).extract(TEXT)
    assert result.decoder_used == "llm"
    assert result.llm_error is None
    assert result.findings


def test_причина_видна_в_ответе_анализа(monkeypatch):
    from app.api import analysis

    client = Mock()
    client.extract_findings.side_effect = RuntimeError("Сервер недоступен")
    monkeypatch.setattr(analysis, "get_extractor", lambda: TeammateExtractor(client=client))
    response = analysis._analyze(TEXT, None).model_dump()
    assert response["decoder_used"] == "rules"
    assert "Сервер недоступен" in response["llm_error"]
    assert response["findings"]

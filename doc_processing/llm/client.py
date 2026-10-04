from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .extractor import normalize_findings_response, prepare_document_for_llm
from .prompts import build_findings_prompt
from .schema import FINDINGS_SCHEMA


class OllamaNotAvailableError(RuntimeError):
    pass


class OllamaExtractionError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, *, base_url: str = "http://localhost:11434", model: str = "medgemma:4b", timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise OllamaNotAvailableError(f"Unable to reach Ollama at {self.base_url}: {exc}") from exc

    def extract_findings(self, document: dict[str, Any]) -> dict[str, Any]:
        sanitized = prepare_document_for_llm(document)
        prompt = build_findings_prompt(sanitized)
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": FINDINGS_SCHEMA,
        }

        data = self._post(payload)
        message = data.get("message", {}) if isinstance(data, dict) else {}
        raw_content = message.get("content") if isinstance(message, dict) else None

        if raw_content is None:
            if isinstance(data, dict) and "findings" in data:
                raw_content = data
            else:
                raise OllamaExtractionError("Ollama response did not include a findings payload.")

        if isinstance(raw_content, str):
            stripped = raw_content.strip()
            if not stripped:
                return {"findings": []}
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = {"findings": []}
        else:
            parsed = raw_content

        return normalize_findings_response(parsed)

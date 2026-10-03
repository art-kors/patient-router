"""API анализа протокола: загрузить протокол → получить объяснение.

Сквозной путь, который жюри увидит первым:
    POST /api/v1/analyze  →  находки + сработавшие и подавленные триггеры

Здесь НЕТ создания маршрута: это отдельный шаг (POST /api/v1/routes).
Разделение намеренное — анализ ничего не меняет в системе, его можно
гонять сколько угодно для отладки и разметки.
"""

import io
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, UploadFile
from pydantic import BaseModel, Field

from app.services.decision import DecisionEngine
from app.services.extraction import get_extractor

router = APIRouter(prefix="/api/v1", tags=["analysis"])


class FindingOut(BaseModel):
    """Находка с цитатой-доказательством."""

    finding: str
    organ: str | None = None
    laterality: str | None = None
    size_mm: float | None = None
    classification: str | None = None
    quote: str = Field(description="Дословный фрагмент исходного текста")
    char_start: int = 0
    char_end: int = 0
    in_negative_context: bool = False
    confidence: float = 0.0


class TriggerMatchOut(BaseModel):
    """Решение по одному триггеру — и сработавшему, и подавленному."""

    trigger_id: str
    display_name: str
    fired: bool
    suppressed: bool
    suppression_reason: str | None = None
    quote: str = ""
    confidence: float = 0.0
    applied_rule: str = Field(description="например endometrial_polyp@v1")
    detail: str = Field(description="Почему так: цитата, порог, отрицание")
    version: int = 1


class AnalyzeResponse(BaseModel):
    """Ответ анализа: что нашли и что с этим делать."""

    study_type: str | None = None
    extractor: str = Field(description="Какой декодер сработал")
    conclusion_extracted: bool

    findings: list[FindingOut]
    matches: list[TriggerMatchOut]

    route_would_be_created: bool = Field(
        description="Создался бы маршрут (true) или протокол признан нормой (false)"
    )
    winning_trigger: str | None = Field(
        description="Триггер, по которому создаётся маршрут. None — норма"
    )
    specialty: str | None = Field(description="Кого вызовем: профиль специалиста")
    potential_route: str | None = None
    target_sla_days: int | None = None

    is_emergency: bool = Field(
        description=(
            "Экстренная находка. При true маршрут НЕ создаётся: "
            "система только эскалирует персоналу."
        )
    )
    emergency_notice: str | None = None

    triggered_count: int = 0
    suppressed_count: int = 0


@router.post(
    "/analyze",
    response_model=AnalyzeResponse,
    summary="Разобрать протокол и объяснить решение",
)
async def analyze_text(payload: Annotated[dict[str, Any], Body()]) -> AnalyzeResponse:
    """Анализ текста протокола.

    Не меняет состояние системы: ни маршрута, ни уведомлений.
    Нужен для демонстрации объяснимости и для отладки правил.
    """
    text = payload.get("text") or ""
    study_type = payload.get("study_type")
    return _analyze(text, study_type)


@router.post(
    "/analyze/upload",
    response_model=AnalyzeResponse,
    summary="Разобрать загруженный файл протокола",
)
async def analyze_upload(
    file: Annotated[UploadFile, File(description="Файл протокола .docx или .txt")],
    study_type: Annotated[str | None, Form()] = None,
) -> AnalyzeResponse:
    """То же, что /analyze, но файлом.

    Поддерживается .docx (выданные протоколы хакатона) и .txt.
    """
    raw = await file.read()
    text = _decode(raw)
    return _analyze(text, study_type)


def _decode(raw: bytes) -> str:
    """Достать текст из .docx или .txt.

    Для .docx нужен python-docx; если его нет — честно падаем,
    а не молча возвращаем пустоту (иначе демо покажет «находок нет»).
    """
    if raw[:2] == b"PK":  # zip-сигнатура docx
        try:
            import docx  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ValueError(
                "Для .docx нужен пакет python-docx. "
                "Либо установите его, либо передайте текст в /api/v1/analyze"
            ) from exc
        document = docx.Document(io.BytesIO(raw))
        return "\n".join(p.text for p in document.paragraphs)
    return raw.decode("utf-8", errors="replace")


def _analyze(text: str, study_type: str | None) -> AnalyzeResponse:
    """Общая логика для обоих эндпоинтов."""
    extractor = get_extractor()
    engine = DecisionEngine()

    extraction = extractor.extract(text, study_type=study_type)
    decision = engine.decide(
        extraction.findings,
        study_type=extraction.meta.study_type,
        conclusion_text=extraction.conclusion_text,
    )

    winner = decision.winner

    return AnalyzeResponse(
        study_type=extraction.meta.study_type,
        extractor=extraction.extractor_name,
        conclusion_extracted=extraction.conclusion_extracted,
        findings=[
            FindingOut(
                finding=f.finding,
                organ=f.organ,
                laterality=f.laterality,
                size_mm=f.size_mm,
                classification=f.classification,
                quote=f.quote,
                char_start=f.char_start,
                char_end=f.char_end,
                in_negative_context=f.in_negative_context,
                confidence=f.confidence,
            )
            for f in extraction.findings
        ],
        matches=[TriggerMatchOut(**m.explanation) for m in decision.matches],
        route_would_be_created=winner is not None,
        winning_trigger=winner.trigger.display_name if winner else None,
        specialty=winner.trigger.specialty if winner else None,
        potential_route=winner.trigger.potential_route if winner else None,
        target_sla_days=winner.trigger.target_sla_days if winner else None,
        is_emergency=decision.is_emergency,
        emergency_notice=(
            "Экстренная находка: маршрут не создаётся, персонал уведомляется"
            if decision.is_emergency
            else None
        ),
        triggered_count=len(decision.fired),
        suppressed_count=len(decision.suppressed),
    )

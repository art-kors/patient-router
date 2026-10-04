"""Локальный интерфейс без сборки и сетевых зависимостей."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["ui"])
STATIC = Path(__file__).resolve().parents[1] / "static"


@router.get("/", include_in_schema=False)
def index():
    """Главная страница дашборда."""
    return FileResponse(STATIC / "index.html")


@router.get("/static/{filename}", include_in_schema=False)
def static(filename: str):
    """Только собственные ресурсы интерфейса, без доступа к произвольным файлам."""
    if filename not in {"dashboard.js", "style.css"}:
        raise HTTPException(404, "Ресурс не найден")
    return FileResponse(STATIC / filename)

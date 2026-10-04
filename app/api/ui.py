"""Локальный интерфейс без сборки и сетевых зависимостей."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["ui"])
STATIC = Path(__file__).resolve().parents[1] / "static"


@router.get("/analytics", include_in_schema=False)
def analytics():
    """Замкнутый цикл оценки качества и врачебной обратной связи."""
    return FileResponse(STATIC / "analytics.html")


@router.get("/patient", include_in_schema=False)
def patient():
    """Кабинет пациента с сообщениями и планом действий."""
    return FileResponse(STATIC / "patient.html")


@router.get("/doctor", include_in_schema=False)
def doctor():
    """Форма нового приёма с клиническим напоминанием."""
    return FileResponse(STATIC / "doctor.html")


@router.get("/pulse", include_in_schema=False)
def pulse():
    """События МИС и управление модельным временем."""
    return FileResponse(STATIC / "pulse.html")


@router.get("/admin", include_in_schema=False)
def admin():
    """Настройки правил и оценка качества для администратора."""
    return FileResponse(STATIC / "admin.html")


@router.get("/", include_in_schema=False)
def index():
    """Стартовая страница с выбором роли."""
    return FileResponse(STATIC / "index.html")


STATIC_FILES = frozenset(
    {
        "dashboard.js",
        "analytics.js",
        "style.css",
        "clinical.js",
        "clinical.css",
        "patient.html",
        "doctor.html",
        "pulse.html",
    }
)


@router.get("/static", include_in_schema=False)
def static_root():
    """Без имени ресурса каталог недоступен."""
    raise HTTPException(404, "Ресурс не найден")


@router.get("/static/{filename:path}", include_in_schema=False)
def static(filename: str):
    """Белый список и границы каталога проверяются независимо друг от друга."""
    path = (STATIC / filename).resolve()
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or path.parent != STATIC.resolve()
        or not path.is_file()
    ):
        raise HTTPException(404, "Ресурс не найден")
    if filename not in STATIC_FILES:
        raise HTTPException(404, "Ресурс не найден")
    return FileResponse(path)

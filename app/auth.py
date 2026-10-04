"""Демо-вход и серверные права. Открытая выдача демо-пароля разрешена только вне production.

HMAC-токены ограничены сроком жизни. Это демонстрационная авторизация, а не
изоляция реальных медицинских данных: публичные демо-роли доступны любому гостю.
Для закрытого стенда задаются AUTH_SECRET и AUTH_USERS с PBKDF2-хешами и patient_ids.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.db import get_session
from app.models import Route, Study
from app.settings import get_settings

ROLES = ("coordinator", "doctor", "patient", "hospitalization_manager", "admin", "manager")
_secret = secrets.token_bytes(32)
_demo_password = secrets.token_urlsafe(18)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def password_hash(password: str, salt: str | None = None) -> str:
    """Формат для AUTH_USERS; соль случайная, пароль в конфиг не записывается."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
    return f"pbkdf2${salt}${digest}"


def check_password(password, encoded):
    try:
        _, salt, _ = encoded.split("$")
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


def sign(payload: dict) -> str:
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    key = get_settings().auth_secret.encode() or _secret
    signature = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return data + "." + signature


def verify(token: str) -> dict:
    try:
        data, signature = token.split(".")
        key = get_settings().auth_secret.encode() or _secret
        if not hmac.compare_digest(
            hmac.new(key, data.encode(), hashlib.sha256).hexdigest(), signature
        ):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if payload["exp"] <= time.time() or payload["role"] not in ROLES:
            raise ValueError
        return payload
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, "Войдите заново", headers={"WWW-Authenticate": "Bearer"}) from None


def demo_enabled():
    settings = get_settings()
    return settings.auth_demo_enabled and not settings.is_production


@router.get("/demo")
def demo():
    """Одно понятное место входа для жюри; пароль генерируется при запуске."""
    if not demo_enabled():
        return {"roles": [], "enabled": False}
    return {"roles": list(ROLES), "password": _demo_password, "enabled": True}


class Login(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: Login):
    settings = get_settings()
    user = settings.auth_users.get(body.username)
    if user:
        valid = check_password(body.password, user.get("password_hash", ""))
        role = user.get("role")
        ids = user.get("patient_ids", [])
    else:
        valid = (
            demo_enabled()
            and body.username in ROLES
            and hmac.compare_digest(body.password, _demo_password)
        )
        role = body.username
        from app.services.mock_mis import MockMisService

        catalog = MockMisService().catalog() if valid else []
        ids = [str(p["patient_id"]) for p in catalog]
        if role == "patient":
            ids = ids[:1]
    if not valid or role not in ROLES:
        raise HTTPException(401, "Неверный логин или пароль")
    if settings.is_production and not settings.auth_secret:
        raise HTTPException(503, "Не настроен ключ авторизации")
    payload = {
        "sub": body.username,
        "role": role,
        "patient_ids": ids,
        "exp": time.time() + settings.auth_token_ttl,
    }
    return {
        "access_token": sign(payload),
        "token_type": "bearer",
        "role": role,
        "expires_in": settings.auth_token_ttl,
    }


def principal(request: Request):
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(401, "Требуется вход", headers={"WWW-Authenticate": "Bearer"})
    user = verify(token)
    if user.get("purpose"):
        raise HTTPException(401, "Требуется токен входа")
    return user


async def patient_access(user, patient_id, session):
    """ID пациента не расширяет подписанную область доступа пользователя."""
    if user["role"] == "admin" or str(patient_id) in user.get("patient_ids", []):
        return
    owned = await session.scalar(
        select(Study.id).where(
            Study.patient_id == patient_id, Study.document_ref == "coordinator:" + user["sub"]
        )
    )
    if user["role"] == "coordinator" and owned:
        return
    raise HTTPException(403, "Пациент вне вашей очереди")


async def authorize(request: Request, user=Depends(principal), session=Depends(get_session)):
    """Общие права API и границы пациента для старых кабинетов и маршрутов."""
    request.state.user = user
    path = request.url.path
    role = user["role"]
    allowed = {"admin"}
    if "/admin" in path:
        pass
    elif "/coordinator" in path:
        allowed |= {"coordinator"}
    elif "/routes" in path:
        allowed |= {"doctor", "coordinator", "hospitalization_manager", "manager"}
        if path.endswith("/transition"):
            allowed |= {"patient"}
        if path.endswith("/tactics"):
            allowed = {"admin", "doctor"}
    elif path == "/api/v1/demo/clock" and request.method == "GET":
        allowed |= set(ROLES)
    elif path == "/api/v1/mock/lk/coordinator/tasks":
        allowed |= {"coordinator"}
    elif "/mock/lk" in path:
        allowed |= {"patient", "doctor", "coordinator"}
    elif "/analy" in path or "/quality" in path:
        allowed |= {"doctor", "manager", "coordinator"}
    elif "/mock/mis/patients" in path:
        allowed |= {"patient", "doctor", "coordinator", "hospitalization_manager"}
    elif "/mis" in path or "/demo" in path:
        allowed |= {"coordinator", "doctor", "hospitalization_manager"}
    if role not in allowed:
        raise HTTPException(403, "Недостаточно прав для этого раздела")
    if role == "admin":
        return
    ids = []
    if request.query_params.get("patient_id"):
        ids.append(request.query_params["patient_id"])
    if request.path_params.get("patient_id") and "/coordinator/" not in path:
        ids.append(request.path_params["patient_id"])
    route_id = request.path_params.get("route_id")
    if route_id:
        try:
            route_uuid = UUID(str(route_id))
        except ValueError:
            raise HTTPException(422, "Некорректный идентификатор маршрута") from None
        route = await session.get(Route, route_uuid)
        if route:
            ids.append(route.patient_id)
    if request.method in {"POST", "PUT", "PATCH"} and "/coordinator/upload" not in path:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        subject = body.get("subject", body)
        if not isinstance(subject, dict):
            subject = {}
        if subject.get("patient_id"):
            ids.append(subject["patient_id"])
        if subject.get("route_id"):
            try:
                body_route = await session.get(Route, UUID(subject["route_id"]))
            except ValueError:
                raise HTTPException(422, "Некорректный идентификатор маршрута") from None
            if body_route:
                ids.append(body_route.patient_id)
        if subject.get("study_id"):
            from app.services.mock_mis import MockMisService

            study_key = str(subject["study_id"])
            found = False
            for patient in MockMisService().catalog():
                if any(
                    study_key in (str(study["id"]), study["study_id"])
                    for study in patient["studies"]
                ):
                    ids.append(patient["patient_id"])
                    found = True
                    break
            if not found:
                try:
                    study = await session.get(Study, UUID(study_key))
                except ValueError:
                    study = None
                if study:
                    ids.append(study.patient_id)
        event_type = body.get("event_type") if isinstance(body, dict) else None
        if "/emit/" in path:
            event_type = path.rsplit("/", 1)[-1]
        if path.endswith("/transition"):
            targets = {
                "patient": {"closed_by_patient"},
                "coordinator": {"notified", "awaiting_booking", "booked", "no_show"},
                "hospitalization_manager": {
                    "hospitalization_scheduled",
                    "hospitalized",
                    "discharged",
                },
                "doctor": {
                    "visit_done",
                    "decision_pending",
                    "referred",
                    "operated",
                    "closed_no_operation",
                },
            }
            if body.get("to_status") not in targets.get(role, set()):
                raise HTTPException(403, "Переход недоступен вашей роли")
        event_roles = {
            "StudyProtocolSigned": {"coordinator"},
            "StudyProtocolCorrected": {"coordinator"},
            "StudyProtocolCancelled": {"coordinator"},
            "TacticsChosen": {"doctor"},
            "VisitStarted": {"doctor"},
            "VisitCompleted": {"doctor"},
            "SurgeryPerformed": {"doctor"},
            "HospitalizationScheduled": {"hospitalization_manager"},
            "HospitalizationFactual": {"hospitalization_manager"},
            "Discharged": {"hospitalization_manager", "doctor"},
        }
        if event_type in event_roles and role not in event_roles[event_type]:
            raise HTTPException(403, "Событие недоступно вашей роли")
        if body.get("patient_external_id"):
            from app.models import Patient

            patient = await session.scalar(
                select(Patient).where(Patient.external_id == body["patient_external_id"])
            )
            if patient:
                ids.append(patient.id)
    for identifier in ids:
        try:
            identifier = UUID(str(identifier))
        except ValueError:
            raise HTTPException(422, "Некорректный идентификатор пациента") from None
        await patient_access(user, identifier, session)


def scope_filter(request: Request, column):
    """Одна область доступа для списков: фильтр действует до limit и count."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] == "admin":
        return []
    ids = [UUID(i) for i in user.get("patient_ids", [])]
    condition = column.in_(ids)
    if user["role"] == "coordinator":
        condition = or_(
            condition,
            column.in_(
                select(Study.patient_id).where(Study.document_ref == "coordinator:" + user["sub"])
            ),
        )
    return [condition]


@router.get("/me")
def me(user=Depends(principal)):
    """Возвращает подписанную область кабинета после входа."""
    return {"role": user["role"], "patient_ids": user.get("patient_ids", [])}

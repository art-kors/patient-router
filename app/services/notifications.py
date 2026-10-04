"""Демонстрационная доставка уведомлений через существующие таблицы.

Тексты меняются JSON-переменной NOTIFICATION_TEMPLATES, например:
{"notify_initial": "{{пациент}}, приглашаем вас в {{учреждение}}."}.
Вызов send_effect(session, effect) сохраняет сообщение; вызывающий обработчик
таймеров делает commit общей транзакции. Внешняя отправка СМС не выполняется.
"""

import re
from collections.abc import Iterable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import get_clock
from app.models import Appointment, Clinic, Doctor, Notification, Route, Task
from app.services.timers import Effect
from app.settings import get_settings

DEFAULT_TEMPLATES = {
    "notify_initial": (
        "{{пациент}}, по результатам УЗИ рекомендуем консультацию специалиста. "
        "Пожалуйста, запишитесь на приём в {{учреждение}}. Мы поможем с записью."
    ),
    "notify_reminder": (
        "{{пациент}}, напоминаем о рекомендованной консультации в {{учреждение}}. "
        "Дата приёма: {{дата_приёма}}. Врач: {{врач}}. "
        "Если вы ещё не записались, свяжитесь с клиникой."
    ),
    "create_task": (
        "Свяжитесь с пациентом {{пациент}} и помогите организовать следующий шаг "
        "в {{учреждение}}. Дата приёма: {{дата_приёма}}. Врач: {{врач}}."
    ),
    "escalate": (
        "Требуется внимание ответственного сотрудника: следующий шаг пациента "
        "{{пациент}} пока не подтверждён. Уточните дату и свяжитесь с {{учреждение}}."
    ),
    "close_route": (
        "{{пациент}}, нам не удалось подтвердить вашу запись в {{учреждение}}. "
        "Свяжитесь с клиникой, чтобы обсудить дальнейшие действия."
    ),
    "schedule_followup": (
        "{{пациент}}, вам рекомендован контрольный приём в {{учреждение}}. "
        "Уточните дату у координатора."
    ),
}


class NotificationSettings(BaseSettings):
    """Позволяет демонстратору заменить отдельные шаблоны через окружение или .env."""

    model_config = SettingsConfigDict(env_prefix="notification_", env_file=".env", extra="ignore")
    templates: dict[str, str] = {}


def render_message(template: str, context: Mapping[str, str]) -> str:
    """Подставляет только известные поля без выполнения выражений из шаблона."""

    def replace(match: re.Match) -> str:
        """Не допускает отправку сообщения с незаполненным полем."""
        key = match.group(1).strip()
        if key not in context:
            raise ValueError(f"Неизвестное поле шаблона: {key}")
        return str(context[key])

    return re.sub(r"{{\s*([^{}]+?)\s*}}", replace, template)


class NotificationService:
    """Сохраняет сообщения и задачи без собственной таблицы и без скрытого коммита."""

    def __init__(self, templates: Mapping[str, str] | None = None) -> None:
        self.templates = DEFAULT_TEMPLATES | NotificationSettings().templates
        if templates is not None:
            self.templates.update(templates)

    async def send_effect(
        self,
        session: AsyncSession,
        effect: Effect,
        context: Mapping[str, str] | None = None,
    ) -> Notification:
        """Превращает эффект таймера в сообщение и, при необходимости, задачу.

        Канал escalate представлен каналом task в существующей схеме и задачей
        с типом escalate и повышенным приоритетом. Имя пациента отсутствует
        в обезличенной модели: демонстратор может передать его через context.
        Каждый вызов означает отдельную доставку; повторную обработку таймера
        предотвращает TimerEngine.fire. Статус маршрута здесь не меняется.
        """
        if effect.kind not in {"notification", "task", "escalate", "close_route", "followup"}:
            raise ValueError("Неизвестный вид эффекта")
        channel = effect.kind if effect.kind in {"task", "escalate"} else effect.channel or "lk"
        if channel not in {"lk", "task", "escalate"}:
            raise ValueError("Неподдерживаемый канал")
        route = await session.get(Route, effect.route_id)
        if route is None:
            raise ValueError("Маршрут не найден")
        appointment = await session.scalar(
            select(Appointment)
            .where(Appointment.route_id == route.id, Appointment.visit_status == "booked")
            .order_by(Appointment.starts_at.desc(), Appointment.id)
            .limit(1)
        )
        clinic_id = (
            appointment.clinic_id if appointment and appointment.clinic_id else route.clinic_id
        )
        clinic = await session.get(Clinic, clinic_id) if clinic_id else None
        doctor = (
            await session.get(Doctor, appointment.doctor_id)
            if appointment and appointment.doctor_id
            else None
        )
        values = {
            "пациент": "Уважаемый пациент",
            "дата_приёма": appointment.starts_at.astimezone(
                ZoneInfo(get_settings().timezone)
            ).strftime("%d.%m.%Y в %H:%M")
            if appointment
            else "пока не назначена",
            "врач": doctor.full_name if doctor else "уточняется при записи",
            "учреждение": clinic.name if clinic else "СМ-Клиника",
        }
        if context:
            values.update(context)
        code = effect.template_code or effect.timer_type.value
        if code not in self.templates:
            raise ValueError(f"Шаблон не найден: {code}")
        body = render_message(self.templates[code], values)
        now = get_clock().now()
        message = Notification(
            id=uuid4(),
            route_id=route.id,
            channel="task" if channel == "escalate" else channel,
            template_code=code,
            body=body,
            sent_at=now,
            delivery_status="delivered",
            is_emergency=False,
        )
        session.add(message)
        if channel in {"task", "escalate"}:
            # Общий идентификатор связывает текст и задачу без изменения схемы.
            session.add(
                Task(
                    id=message.id,
                    route_id=route.id,
                    task_type=channel,
                    assignee_role=effect.assignee_role or "coordinator",
                    priority=1 if channel == "escalate" else 3,
                    due_at=now,
                    created_at=now,
                    status="open",
                )
            )
        await session.flush()
        return message


async def send_effect(
    session: AsyncSession, effect: Effect, context: Mapping[str, str] | None = None
) -> Notification:
    """Отправляет один эффект с актуальной конфигурацией шаблонов."""
    return await NotificationService().send_effect(session, effect, context)


async def deliver_effects(
    session: AsyncSession, effects: Iterable[Effect], context: Mapping[str, str] | None = None
) -> list[Notification]:
    """Отправляет пачку эффектов в транзакции вызывающего кода.

    Единственная точка, где эффект таймера превращается в сообщение пациенту
    или задачу координатору. Живёт в сервисном слое рядом с движком, а не
    внутри ``TimerEngine``: движок не знает про каналы доставки, но любой его
    вызов обязан пройти через эту функцию.

    Не-``Effect`` отбрасываются: слушатели ``ModelClock`` возвращают в
    ``fired`` произвольные объекты, и доставка не должна разбирать их как
    эффекты.

    Коммита здесь нет намеренно. Отметка ``timer.fired`` и сообщение должны
    уйти одной транзакцией вызывающего кода — иначе откат погасит сообщение,
    а отметка останется, и пациент получит напоминание дважды.
    """
    return [
        await send_effect(session, effect, context)
        for effect in effects
        if isinstance(effect, Effect)
    ]

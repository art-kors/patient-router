"""Проверка цикла через HTTP API в отдельной тестовой БД.

Запускать только с POSTGRES_DB, указывающим на одноразовую БД.
Правила сохраняются в БД, файл клинической матрицы не изменяется.
"""

import asyncio
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.clock import get_clock
from app.db import SessionFactory
from app.main import app
from app.models import AnalysisFeedback, AnalysisRun


async def request(client, method, path, **kwargs):
    """Любая ошибка HTTP прерывает демонстрацию."""
    response = await client.request(method, "/api/v1" + path, **kwargs)
    response.raise_for_status()
    return response.json()


async def main():
    """Продемонстрировать проверку врача и изменение правила в редакторе."""
    identifier = "endometrial_polyp"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://demo") as client:
        await request(
            client,
            "PUT",
            f"/admin/triggers/{identifier}",
            json={
                "display_name": "Полип эндометрия",
                "source_study": "УЗИ органов малого таза",
                "synonyms": ["полип эндометрия", "полипа эндометрия"],
                "thresholds": {"min_size_mm": 10},
                "author": "Демонстрация",
            },
        )
        responses = []
        for size in (12, 11, 8, 9):
            responses.append(
                await request(
                    client,
                    "POST",
                    "/analyze",
                    json={
                        "text": f"Заключение: полип эндометрия {size} мм.",
                        "study_type": "УЗИ органов малого таза",
                    },
                )
            )
        before = await request(client, "GET", "/analytics/metrics")
        assert before["doctor_metrics"]["recall"] is None
        print("До врача: recall и precision — недостаточно данных")
        for response, label in zip(
            responses, ("confirmed", "false_positive", "missed", "missed"), strict=True
        ):
            path = f"/analytics/analyses/{response['analysis_id']}/feedback/{identifier}"
            await request(client, "PUT", path, json={"label": label})
            await request(client, "PUT", path, json={"label": label})
        reviewed = await request(client, "GET", "/analytics/metrics")
        assert reviewed["doctor_metrics"]["recall"] == 1 / 3
        assert reviewed["doctor_metrics"]["precision"] == 0.5
        print(
            "Врач: TP=1 FP=1 FN=2; recall=0.333 precision=0.500, "
            "отклонено 50% проверенных находок (25% всех отметок)"
        )
        await request(
            client,
            "PUT",
            f"/admin/triggers/{identifier}",
            json={
                "thresholds": {"min_size_mm": 8},
                "author": "Врач",
                "description": "Снижение порога после проверки двух пропусков",
            },
        )
        after = await request(client, "GET", "/analytics/metrics?current=true")
        assert after["doctor_metrics"]["recall"] == 1
        assert after["doctor_metrics"]["precision"] == 0.75
        print("Правка через редактор 10→8 мм: TP=3 FP=1 FN=0; recall=1.000 precision=0.750")
        historical = await request(client, "GET", "/analytics/metrics")
        assert historical["doctor_metrics"] == reviewed["doctor_metrics"]
        async with SessionFactory() as session:
            ids = [UUID(r["analysis_id"]) for r in responses]
            feedback = (
                await session.scalars(
                    select(AnalysisFeedback).where(AnalysisFeedback.analysis_id.in_(ids))
                )
            ).all()
            runs = (await session.scalars(select(AnalysisRun).where(AnalysisRun.id.in_(ids)))).all()
            assert len(feedback) == len(runs) == 4
            assert all(run.created_at <= get_clock().now() for run in runs)
        print("PostgreSQL: 4 разбора, 4 отметки после 8 PUT; исходные решения сохранены")


if __name__ == "__main__":
    asyncio.run(main())

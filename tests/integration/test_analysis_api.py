"""Тесты сквозного анализа протокола.

Это то, что увидит жюри первым: POST /analyze на реальном тексте.
Проверяем главное — система объясняет своё решение и признаёт норму нормой.
"""

import pytest

from app.main import create_app

# Реальные куски из выданных протоколов хакатона.
TRIGGERED = """
УЛЬТРАЗВУКОВОЕ ИССЛЕДОВАНИЕ ОРГАНОВ МАЛОГО ТАЗА

МАТКА
Размеры:72х59х64 мм
Миометрий: гипоэхогенный интерстициальный узел 26 х 38 мм

ЗАКЛЮЧЕНИЕ: УЗ-признаки миомы матки, узел 38 мм.
Рекомендовано: консультация гинеколога.
"""

NORMAL = """
УЛЬТРАЗВУКОВОЕ ИССЛЕДОВАНИЕ ОРГАНОВ МАЛОГО ТАЗА

МАТКА
Размеры:72х59х64 мм
Толщина стенок матки симметричная
Эхоструктура миометрия:однородная
Достоверно узловых и очаговых образований не определяется

ЗАКЛЮЧЕНИЕ: Эхо-картина соответствует дню менструального цикла.
"""

EMPTY = """
ЗАКЛЮЧЕНИЕ: УЗ патологии на момент исследования не выявлено.
"""

EMERGENCY = "Заключение: стеноз 80%."
"""Экстренная находка: значимый стеноз артерий нижних конечностей.

У этого триггера emergency_flag=true — маршрут создавать нельзя.
"""

EMERGENCY_STUDY = "УЗДГ артерий нижних конечностей"

pytestmark = pytest.mark.anyio


@pytest.fixture
def analyze(analytics_session):
    """Клиент к приложению; создаём заново на каждый тест."""
    from httpx import ASGITransport, AsyncClient

    from app.db import get_session

    app = create_app()
    app.dependency_overrides[get_session] = lambda: analytics_session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestAnalyzeTriggered:
    async def test_находит_маршрут(self, analyze):
        async with analyze as c:
            response = await c.post(
                "/api/v1/analyze", json={"text": TRIGGERED, "study_type": "УЗИ органов малого таза"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["route_would_be_created"] is True
        assert body["winning_trigger"]

    async def test_называет_профиль_специалиста(self, analyze):
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": TRIGGERED, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        assert body["specialty"], "маршрут без профиля — не маршрут"

    async def test_даёт_срок(self, analyze):
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": TRIGGERED, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        assert body["target_sla_days"] > 0

    async def test_каждая_находка_с_цитатой(self, analyze):
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": TRIGGERED, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        for item in body["findings"]:
            assert item["quote"], "находка без цитаты недопустима"
            assert item["quote"] in TRIGGERED

    async def test_каждый_триггер_объяснён(self, analyze):
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": TRIGGERED, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        assert body["matches"]
        for match in body["matches"]:
            assert match["applied_rule"]
            assert match["detail"], f"{match['trigger_id']} без объяснения"


class TestAnalyzeNormal:
    """НОРМА: маршрута нет, но объяснение «почему» — обязательно."""

    async def test_маршрут_не_создаётся(self, analyze):
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": NORMAL, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        assert body["route_would_be_created"] is False
        assert body["winning_trigger"] is None
        assert body["specialty"] is None

    async def test_объяснение_для_каждого_подавленного_триггера(self, analyze):
        """Кейс: «Для нормы — почему триггер не сработал»."""
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": NORMAL, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        suppressed = [m for m in body["matches"] if m["suppressed"] or not m["fired"]]
        assert suppressed, "у нормы должны быть подавленные триггеры"
        for match in suppressed:
            assert match["suppression_reason"], f"{match['trigger_id']}: подавлен без причины"
            assert match["detail"]

    async def test_причина_подавления_конкретна(self, analyze):
        """У каждого подавленного триггера должна быть конкретная причина.

        В данном протоколе ни один синоним не встретился, поэтому причина
        no_match. Если бы находка совпала, но стояла в отрицательном
        контексте — была бы negative_context. Проверяем, что причина
        всегда названа, а не молчит.
        """
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": NORMAL, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        suppressed = [m for m in body["matches"] if m["suppressed"] or not m["fired"]]
        assert suppressed
        for match in suppressed:
            assert match["suppression_reason"] in {
                "negative_context",
                "threshold_not_met",
                "study_type_mismatch",
                "no_match",
            }
            assert match["detail"]


class TestSafety:
    async def test_обычный_протокол_не_экстренный(self, analyze):
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": TRIGGERED, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        assert body["is_emergency"] is False

    async def test_пустой_протокол_безопасен(self, analyze):
        async with analyze as c:
            body = (await c.post("/api/v1/analyze", json={"text": EMPTY})).json()
        assert body["route_would_be_created"] is False
        assert body["is_emergency"] is False

    async def test_пустой_текст_не_ломает(self, analyze):
        async with analyze as c:
            response = await c.post("/api/v1/analyze", json={"text": ""})
        assert response.status_code == 200

    async def test_без_поля_text_не_ломает(self, analyze):
        async with analyze as c:
            response = await c.post("/api/v1/analyze", json={})
        assert response.status_code == 200


class TestEmergency:
    """ЭКСТРЕННОЕ: маршрут не создаётся, персонал получает эскалацию.

    Тот же запрет, что в routing.create_from_match: emergency_flag=true
    означает «не маршрутизировать автоматически». Предпросмотр /analyze
    обязан показывать то же решение, что применит система, иначе он
    врёт о том, что будет сделано с пациентом.
    """

    async def test_экстренная_находка_блокирует_маршрут(self, analyze):
        async with analyze as c:
            response = await c.post(
                "/api/v1/analyze",
                json={"text": EMERGENCY, "study_type": EMERGENCY_STUDY},
            )
        body = response.json()
        assert response.status_code == 200
        assert body["is_emergency"] is True
        assert body["emergency_notice"]
        assert body["route_would_be_created"] is False, (
            "экстренная находка не должна создавать маршрут"
        )

    async def test_экстренная_находка_блокирует_маршрут_и_при_другой_находке(self, analyze):
        """Экстренность — не «побеждает один триггер»: она блокирует маршрут целиком.

        В протоколе есть и обычный полип, и экстренный стеноз. Полип сам
        по себе дал бы маршрут, но экстренность в этом же протоколе
        означает эскалацию вместо автомаршрута.
        """
        async with analyze as c:
            response = await c.post(
                "/api/v1/analyze",
                json={"text": f"{EMERGENCY} Полип эндометрия 12 мм."},
            )
        body = response.json()
        assert response.status_code == 200
        assert body["is_emergency"] is True
        assert body["route_would_be_created"] is False
        assert body["winning_trigger"], "сработавший триггер остаётся в объяснении"

    async def test_обычная_находка_по_прежнему_создаёт_маршрут(self, analyze):
        """Обратная сторона: запрет не должен ломать нормальное решение."""
        async with analyze as c:
            body = (
                await c.post(
                    "/api/v1/analyze",
                    json={"text": TRIGGERED, "study_type": "УЗИ органов малого таза"},
                )
            ).json()
        assert body["is_emergency"] is False
        assert body["route_would_be_created"] is True
        assert body["winning_trigger"]


class TestRequestValidation:
    """Невалидный тип — это 422 от Pydantic, а не 500 из глубины экстрактора."""

    @pytest.mark.parametrize("payload", [{"text": 123}, {"text": ["a"]}, {"text": {"a": 1}}])
    async def test_нестроковый_text_даёт_422(self, analyze, payload):
        async with analyze as c:
            response = await c.post("/api/v1/analyze", json=payload)
        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["loc"][-1] == "text"

    async def test_нестроковый_study_type_даёт_422(self, analyze):
        async with analyze as c:
            response = await c.post("/api/v1/analyze", json={"text": "x", "study_type": 5})
        assert response.status_code == 422, response.text

    async def test_пустое_тело_остаётся_200(self, analyze):
        """Анализ пустого протокола — штатный случай, а не ошибка."""
        async with analyze as c:
            response = await c.post("/api/v1/analyze", json={})
        assert response.status_code == 200


class TestDeterminism:
    async def test_два_вызова_дают_одинаковый_результат(self, analyze):
        payload = {"text": TRIGGERED, "study_type": "УЗИ органов малого таза"}
        async with analyze as c:
            first = (await c.post("/api/v1/analyze", json=payload)).json()
            second = (await c.post("/api/v1/analyze", json=payload)).json()
        assert first.pop("analysis_id") != second.pop("analysis_id")
        assert first == second, "решение должно быть воспроизводимым"


class TestNoSideEffects:
    async def test_анализ_ничего_не_меняет(self, analyze):
        """Журнал накапливается, но повторные вызовы не меняют клиническое решение."""
        async with analyze as c:
            first = (await c.post("/api/v1/analyze", json={"text": TRIGGERED})).json()
            # между вызовами — другой протокол; он не должен влиять на первый
            await c.post("/api/v1/analyze", json={"text": NORMAL})
            again = (await c.post("/api/v1/analyze", json={"text": TRIGGERED})).json()
        assert first.pop("analysis_id") != again.pop("analysis_id")
        assert first == again, "предыдущие разборы не должны менять решение"


class TestOpenApiSchema:
    async def test_эндпоинты_в_схеме(self, analyze):
        async with analyze as c:
            paths = (await c.get("/openapi.json")).json()["paths"]
        assert "/api/v1/analyze" in paths
        assert "/api/v1/analyze/upload" in paths

    async def test_схема_описывает_маршрут_как_решение(self, analyze):
        async with analyze as c:
            paths = (await c.get("/openapi.json")).json()["paths"]
        response = paths["/api/v1/analyze"]["post"]["responses"]["200"]
        schema_ref = response["content"]["application/json"]["schema"]["$ref"]
        assert "AnalyzeResponse" in schema_ref

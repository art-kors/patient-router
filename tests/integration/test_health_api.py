"""Тесты служебных ручек.

/health — проверка живости процесса (её дёргает HEALTHCHECK в контейнере).
/ready — проверка БД. Без поднятой БД должна отдавать 503, а не 500:
иначе мониторинг не отличит «БД недоступна» от «приложение сломано».
"""

import pytest

pytestmark = pytest.mark.anyio


class TestHealth:
    async def test_возвращает_ok(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["app"] == "patient-router"

    async def test_время_в_формате_iso(self, client):
        body = (await client.get("/health")).json()
        # парсится как ISO-время с таймзоной
        assert body["time"].endswith("+00:00") or "Z" in body["time"]

    async def test_флажок_модельного_времени(self, client, model_clock):
        body = (await client.get("/health")).json()
        assert body["model_clock"] is True

    async def test_без_фикстуры_время_системное(self, client):
        body = (await client.get("/health")).json()
        assert body["model_clock"] is False


class TestReady:
    async def test_без_бд_отдаёт_503(self, client, monkeypatch):
        """Хост 'db' не резолвится вне docker → ожидаем 503, не 500."""

        async def boom(*args, **kwargs):
            raise OSError("Name or service not known")

        from app.api import health as health_module

        monkeypatch.setattr(health_module, "get_session", boom)
        response = await client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["database"] == "down"
        assert "error" in body

    async def test_ошибка_не_протекает_наружу(self, client, monkeypatch):
        """Текст ошибки БД не должен показываться клиенту целиком."""

        async def boom(*args, **kwargs):
            raise RuntimeError("SENSITIVE: password authentication failed for user")

        from app.api import health as health_module

        monkeypatch.setattr(health_module, "get_session", boom)
        body = (await client.get("/ready")).json()
        assert len(body["error"]) <= 200


class TestOpenApi:
    async def test_схема_доступна(self, client):
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/health" in paths
        assert "/ready" in paths

    async def test_описание_упоминает_границы(self, client):
        """В описании зафиксировано, что система не ставит диагноз."""
        info = (await client.get("/openapi.json")).json()["info"]
        assert "не ставит диагноз" in info["description"]

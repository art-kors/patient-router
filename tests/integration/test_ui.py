"""Страницы и безопасность статики: постоянная версия /tmp/check_ui.py."""

from html.parser import HTMLParser
from urllib.parse import unquote

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import ui

pytestmark = pytest.mark.anyio


class Elements(HTMLParser):
    """Разбирает настоящие HTML-элементы, а не совпадения внутри комментариев."""

    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


PAGE_ELEMENTS = {
    "/analytics": {
        "cards": "div",
        "triggers": "div",
        "trend": "div",
        "analyze": "form",
        "runs": "div",
        "compare": "button",
    },
    "/": {
        "cards": "div",
        "confusion": "div",
        "trigger-list": "div",
        "editor": "form",
        "version-list": "div",
        "split": "select",
    },
    "/patient": {
        "messages": "div",
        "route-panel": "section",
        "patient-route": "div",
        "route-actions": "div",
        "unfinished-banner": "section",
        "patient-select": "select",
        "load-patient": "button",
    },
    "/doctor": {
        "unfinished-banner": "section",
        "start-visit": "button",
        "visit-status": "p",
        "patient-route": "div",
        "route-actions": "div",
    },
    "/pulse": {
        "send-event": "button",
        "event-type": "select",
        "event-payload": "textarea",
        "event-feed": "div",
        "time-actions": "div",
        "analysis-result": "div",
        "delivery-result": "div",
        "scenarios": "div",
    },
}


@pytest.mark.parametrize("path", PAGE_ELEMENTS)
async def test_page_contract(client, path):
    """Каждая страница подключена к приложению и содержит рабочие точки интерфейса."""
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    elements = Elements(response.text).elements
    for identifier, tag in PAGE_ELEMENTS[path].items():
        matches = [(t, attrs) for t, attrs in elements if attrs.get("id") == identifier]
        assert len(matches) == 1, identifier
        assert matches[0][0] == tag, identifier
    if path != "/":
        assert ("body", {"data-page": path[1:]}) in elements
        banner = [attrs for _, attrs in elements if attrs.get("id") == "unfinished-banner"]
        if banner:
            assert banner[0].get("aria-live") == "polite"


@pytest.mark.parametrize("path", PAGE_ELEMENTS)
async def test_page_resources_and_navigation(client, path):
    """Скрипт запускается через defer, ресурсы доступны, переходы ведут на все страницы."""
    elements = Elements((await client.get(path)).text).elements
    script = {"/": "dashboard.js", "/analytics": "analytics.js"}.get(path, "clinical.js")
    assert any(
        tag == "script" and attrs.get("src") == f"/static/{script}" and "defer" in attrs
        for tag, attrs in elements
    )
    styles = {
        attrs.get("href")
        for tag, attrs in elements
        if tag == "link" and attrs.get("rel") == "stylesheet"
    }
    assert "/static/style.css" in styles
    if path not in ("/", "/analytics"):
        assert "/static/clinical.css" in styles
    assert set(PAGE_ELEMENTS) <= {attrs.get("href") for tag, attrs in elements if tag == "a"}
    for resource in styles | {f"/static/{script}"}:
        assert (await client.get(resource)).status_code == 200


@pytest.mark.parametrize(
    "name,media",
    [
        ("style.css", "text/css"),
        ("clinical.css", "text/css"),
        ("dashboard.js", "javascript"),
        ("clinical.js", "javascript"),
        ("patient.html", "text/html"),
        ("doctor.html", "text/html"),
        ("pulse.html", "text/html"),
    ],
)
async def test_allowed_static(client, name, media):
    """Разрешённые ресурсы выдаются целиком с подходящим типом содержимого."""
    response = await client.get(f"/static/{name}")
    assert response.status_code == 200
    assert media in response.headers["content-type"]
    assert response.content == (ui.STATIC / name).read_bytes()


@pytest.mark.parametrize(
    "path",
    [
        "/static/../app/main.py",
        "/static/%2e%2e%2fapp/main.py",
        "/static/..%2fapp/main.py",
        "/static/%2e%2e%2fapp%2fmain.py",
        "/static/%2e%2e/app/main.py",
        "/static/%2e%2e%2f",
        "/static/..%2f",
        "/static/%252e%252e%252fapp%252fmain.py",
        "/static/..%5cmain.py",
        "/static/missing.js",
        "/static",
        "/static/",
        "/static/models.py",
        "/static/ui.py",
        "/static/index.html",
    ],
)
async def test_static_rejects_raw_path(path):
    """Исходный путь доходит до ASGI без нормализации ../ в HTTP-клиенте."""
    from app.main import app

    async def raw_path_app(scope, receive, send):
        scope = dict(scope, path=unquote(path), raw_path=path.encode("ascii"))
        await app(scope, receive, send)

    async with AsyncClient(
        transport=ASGITransport(app=raw_path_app), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 404


@pytest.mark.parametrize("name", ["secret.txt", "extra.js", "index.html", "main.py"])
async def test_existing_unlisted_file_is_private(client, monkeypatch, tmp_path, name):
    """Наличие файла на диске не заменяет явное разрешение на его выдачу."""
    (tmp_path / name).write_text("Секретные данные", encoding="utf-8")
    monkeypatch.setattr(ui, "STATIC", tmp_path)
    assert (await client.get(f"/static/{name}")).status_code == 404


async def test_allowed_missing_file_returns_404(client, monkeypatch, tmp_path):
    """Удалённый разрешённый ресурс даёт 404 вместо исключения FileResponse."""
    monkeypatch.setattr(ui, "STATIC", tmp_path)
    assert (await client.get("/static/clinical.js")).status_code == 404


async def test_allowed_symlink_cannot_escape_static(client, monkeypatch, tmp_path):
    """Даже разрешённое имя не открывает файл вне каталога статики."""
    static = tmp_path / "static"
    static.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("Секретные данные", encoding="utf-8")
    (static / "clinical.js").symlink_to(secret)
    monkeypatch.setattr(ui, "STATIC", static)
    assert (await client.get("/static/clinical.js")).status_code == 404


@pytest.mark.parametrize("filename", ["../secret.txt", "../static/clinical.js"])
async def test_allowlist_cannot_authorize_traversal(client, monkeypatch, tmp_path, filename):
    """Ошибочное расширение белого списка не разрешает обход пути."""
    static = tmp_path / "static"
    static.mkdir()
    (tmp_path / "secret.txt").write_text("Секретные данные", encoding="utf-8")
    (static / "clinical.js").write_text("Ресурс", encoding="utf-8")
    monkeypatch.setattr(ui, "STATIC", static)
    monkeypatch.setattr(ui, "STATIC_FILES", ui.STATIC_FILES | {filename})
    response = await client.get("/static/" + filename.replace("/", "%2f"))
    assert response.status_code == 404

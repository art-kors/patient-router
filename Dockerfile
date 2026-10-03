# syntax=docker/dockerfile:1.7
# ────────────────────────────────────────────────────────────
# patient-router · образ приложения
# Собран на uv.lock — сборка воспроизводима.
# ────────────────────────────────────────────────────────────

# ── сборочный слой: ставим зависимости в отдельный venv ──────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# сначала только манифесты — слой с зависимостями кэшируется
# отдельно от кода и пересобирается только при смене lock-файла
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# затем код приложения (пакет собирается как editable)
COPY app ./app
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ── рантайм: только venv с зависимостями ────────────────────
FROM python:3.12-slim AS runtime

# tzdata нужен для Europe/Moscow; curl — для healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# непривилегированный пользователь
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# venv целиком из сборочного слоя — нативные библиотеки совпадают
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/app /app/app

USER appuser

EXPOSE 8000

# проверяем именно живость процесса; готовность БД — /ready
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
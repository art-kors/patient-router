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

# затем код приложения + миграции (пакет собирается как editable)
# config/ — данные, а не код: без него в образе не будет routing_matrix.json,
# и первая же попытка принять решение упадёт (см. app/settings.py).
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY config ./config
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
COPY --from=builder --chown=appuser:appuser /app/alembic /app/alembic
COPY --from=builder --chown=appuser:appuser /app/alembic.ini /app/alembic.ini
# settings.routing_matrix_path = "config/routing_matrix.json", WORKDIR=/app
# → матрица обязана лежать по пути /app/config/routing_matrix.json
COPY --from=builder --chown=appuser:appuser /app/config /app/config

USER appuser

EXPOSE 8000

# проверяем именно живость процесса; готовность БД — /ready
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
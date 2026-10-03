# patient-router

**СМ-Клиника «Не потерять пациента»** — веб-сервис, который распознаёт в протоколе УЗИ клинически значимый триггер и организует следующий шаг пациента: уведомление → запись к профильному специалисту → приём → решение → госпитализация → операция → контроль.

Система **не ставит диагноз и не назначает лечение**. Она ведёт маршрут и не даёт пациенту исчезнуть ни на одном переходе.

## Стек

| Слой           | Технология                       |
| -------------- | -------------------------------- |
| API            | FastAPI + Pydantic v2            |
| ORM / миграции | SQLAlchemy 2.0 (async) + Alembic |
| БД             | PostgreSQL 16                    |
| Шаблоны        | Jinja2                           |
| Логирование    | structlog                        |
| Зависимости    | uv (.lock в репозитории)         |
| Контейнеры     | Docker + Docker Compose          |

## Быстрый старт

```bash
cp .env.example .env      # при необходимости
docker compose up --build
```

Проверка:

```bash
curl localhost:8000/health   # живость приложения
curl localhost:8000/ready    # готовность + связь с БД
open http://localhost:8000/docs
```

Приложение: <http://localhost:8000> · OpenAPI: <http://localhost:8000/docs> · БД: `localhost:5433`.

## Миграции

DSN берётся из `app/settings.py`, поэтому `alembic` и приложение всегда ходят
в одну и ту же БД.

```bash
uv run alembic upgrade head          # применить миграции
uv run alembic downgrade base        # откатить всё
uv run alembic revision --autogenerate -m "описание"   # новая миграция
uv run alembic check                 # есть ли расхождения с моделями
uv run alembic current               # текущая ревизия
```

Новые миграции автоматически форматируются `ruff` (хуки в `alembic.ini`).

> `docker compose up` **не** применяет миграции: делайте `alembic upgrade head`
> отдельно. Это осознанно — на боевой среде миграции накатываются явно, а не
> при старте контейнера.

## Локальная разработка без Docker

```bash
uv sync                              # зависимости из uv.lock
docker compose up -d db              # только БД
uv run alembic upgrade head          # создать схему
uv run uvicorn app.main:app --reload
```

Локально можно ходить в существующий PostgreSQL через unix-сокет, если указать
путь вместо хоста — пароль не потребуется:

```bash
POSTGRES_HOST=/run/postgresql
POSTGRES_DB=<база>
POSTGRES_USER=<роль>
```

## Команды

```bash
uv run ruff check .          # линтер
uv run ruff format .         # форматирование
uv run mypy app              # типы
uv run pytest                # тесты с покрытием
```

## Структура

```
app/
├── main.py           # FastAPI: приложение, lifespan, роутеры
├── api/health.py     # /health — живость, /ready — готовность к БД
├── settings.py       # конфигурация из переменных окружения
├── db.py             # async-движок SQLAlchemy и фабрика сессий
├── clock.py          # Clock: системное и модельное время
└── models.py         # ORM-модели (20 сущностей)
alembic/              # миграции (async, DSN из app/settings)
alembic.ini           # конфигурация; URL переопределяется в alembic/env.py
infra/postgres/       # эталонная схема и сиды (для справки и ручных проверок)
Dockerfile            # образ приложения (uv.lock, --frozen)
Dockerfile.dev        # dev-образ с hot reload
docker-compose.yaml   # db + app (+ dev в профиле)
```

## Ограничения

- Данные — обезличенные протоколы хакатона. Файлы не публикуются.
- Доступа к МИС (1С) и сервису расписания нет — используются адаптеры-заглушки с боевыми контрактами.
- В пилоте с реальными медицинскими данными — развёртывание в контуре клиники.

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

## Документация

Подробное описание — в [`docs/`](docs/):

| Файл | Содержание |
|------|-----------|
| [`architecture.md`](docs/architecture.md) | Слои системы, конвейер обработки протокола, модельное время, безопасность |
| [`data-model.md`](docs/data-model.md) | 20 сущностей, идемпотентность, критические поля, частичные индексы |
| [`api.md`](docs/api.md) | REST-контракты, события МИС, коды ошибок, пример ответа `/analyze` |
| [`routing-matrix.md`](docs/routing-matrix.md) | Триггеры и поля матрицы на языке врача, как добавить новый |

## Тесты

82 теста, покрытие 98 %. Юнит-тесты не требуют БД.

```bash
make test          # все тесты с покрытием
make test-unit     # только юнит-тесты
make cov           # HTML-отчёт → htmlcov/index.html
make check         # линт + тесты (то же, что делает CI)
```

Что проверяется:

| Файл | Что |
|------|-----|
| `tests/unit/test_clock.py` | модельное время: `advance(30 дней)` мгновенно, откат запрещён, singleton |
| `tests/unit/test_settings.py` | DSN (TCP и unix-сокет), окружение, `.env` в `.gitignore` |
| `tests/unit/test_models.py` | ограничения на дубли, CHECK, enum, частичные индексы, server defaults, связи |
| `tests/integration/test_health_api.py` | `/health`, `/ready` (503 без БД, ошибка не течёт наружу), OpenAPI |

## Непрерывная интеграция

`.github/workflows/ci.yml` — 6 задач:

| Job | Что делает |
|-----|-----------|
| `lint` | ruff format --check, ruff check, mypy |
| `test` | pytest + отчёт о покрытии артефактом |
| `migrate` | `alembic upgrade` → `check` → `downgrade base` → `upgrade` на postgres:16 |
| `docker` | сборка образа и проверка, что контейнер отвечает на `/health` |
| `smoke` | полный стек: `compose up` → `/ready` → миграции → проверка схемы |
| `quality-gate` | сводка, падает если хоть один job красный |

```bash
make pre-commit    # локальный прогон хуков (тот же набор проверок)
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

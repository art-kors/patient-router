# Postgres · сущности patient-router

Схема БД проекта **СМ-Клиника «Не потерять пациента»** — 20 таблиц, 4 enum, 3 представления, 77 индексов.

Источник правды: заметка `07 — Модель данных` в Obsidian.

## Запуск

```bash
cd infra/postgres

# поднять БД (схема + сиды применятся автоматически при первом старте)
docker compose up -d

# проверить состояние
docker compose ps
```

Подключение:

```bash
psql -h localhost -p 5433 -U pr -d patient_router
```

Порт **5433** (не 5432) — чтобы не конфликтовать с локальным PostgreSQL.

## Схема разделена на группы

| Группа | Таблицы | Смысл |
|--------|---------|--------|
| 1. Данные исследования | `patient`, `study`, `protocol`, `finding` | Что написал врач УЗИ |
| 2. Правила и решения | `trigger_def`, `trigger_match` | Что извлекла и как решила система |
| 3. Маршрут | `route`, `route_step`, `timer`, `task`, `notification` | Что происходит с пациентом |
| 4. Клинический контур | `appointment`, `hospitalization`, `surgery`, `followup` | Что реально произошло |
| 5. Сквозные | `mis_event`, `audit_log`, `specialty`, `clinic`, `doctor` | Интеграция, аудит, справочники |

## Что защищает каждый констрейнт

Четыре защиты от дублей — на них держится требование кейса «повторная доставка события не должна создавать дубли»:

| Ограничение | Что блокирует |
|-------------|---------------|
| `mis_event.event_id` UNIQUE | Повторная доставка события МИС |
| `uq_trigger_match_protocol_trigger` | Второй триггер того же типа на том же протоколе |
| `route.trigger_match_id` UNIQUE | Второй маршрут от одного срабатывания |
| `uq_timer_route_type_due` | Два одинаковых таймера при пересоздании |

Проверки (CHECK) ограничивают enum-поля: `route_status` (19 значений), `tactics`, `timer_type`, `suppression_reason`, `priority` 1–4, `patient.sex`, статусы задач/уведомлений/записей.

## Ключевые индексы

| Индекс | Зачем |
|--------|-------|
| `idx_timer_due` (partial `WHERE NOT fired`) | Выборка due-таймеров при `Clock.advance()` — движок модельного времени |
| `idx_route_unfinished` (partial `WHERE closed_at IS NULL`) | Баннер «Незавершённый клинический маршрут» + отчёт о потерях |
| `idx_mis_event_unprocessed` (partial) | Очередь необработанных событий |
| `idx_hosp_no_date` (partial) | Контроль «назначена ли дата госпитализации ≤ 3 раб. дней» |
| `idx_study_raw_text_trgm`, `idx_finding_name_trgm` | Полнотекстовый поиск по `pg_trgm` |

## Представления

| Представление | Для чего |
|---------------|----------|
| `v_route_funnel` | Воронка из 10 переходов (цифры кейса) |
| `v_route_losses` | Потерянные маршруты + `days_open` |
| `v_route_sla_violations` | Просрочки `target_sla_days` |

## Файлы

```
infra/postgres/
├── compose.yml                     # сервис db + одноразовый db-init
└── init/
    ├── 01_schema.sql               # таблицы, enum, индексы, представления
    └── 02_seed_reference.sql       # 11 специальностей, 3 площадки, 2 врача
```

## Переприменение скриптов к существующей БД

`docker-entrypoint-initdb.d` выполняется **только при первом старте**. Если volume уже инициализирован, а скрипты изменились:

```bash
docker compose --profile tools run --rm db-init
```

## Полный сброс

```bash
docker compose down -v      # -v удаляет volume <проект>_pgdata
docker compose up -d
```

> ⚠️ `-v` удаляет все данные безвозвратно. На боевой среде не выполнять.

> [!note] Имен контейнеров и тома в этом файле нет намеренно
> Compose называет их `<проект>-<сервис>-1` и `<проект>_pgdata`. Раньше здесь
> стояли `container_name: pr-db` и `name: pr_pgdata`, и стенд падал рядом с
> любой другой копией проекта (`The container name "/pr-db" is already in use`).
> Связь `db-init` → `db` идёт по имени **сервиса** в сети compose, поэтому
> `container_name` ей не был нужен.

## Переменные окружения

Все параметры переопределяются (`.env` рядом с `compose.yml`):

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `POSTGRES_DB` | `patient_router` | Имя БД |
| `POSTGRES_USER` | `pr` | Пользователь |
| `POSTGRES_PASSWORD` | `pr_local_dev` | Пароль (**сменить для не-dev**) |
| `POSTGRES_PORT` | `5433` | Порт на хосте |
| `TZ` | `Europe/Moscow` | Часовой пояс контейнера |

## Проверено

Схема применена на PostgreSQL 18.6, сиды загружены. Прогнаны 9 тестов констрейнтов: 4 защиты от дублей блокируют дубликаты, 5 CHECK-ограничений отклоняют невалидные значения. Планировщик использует все 4 частичных индекса (`Bitmap Index Scan` при `enable_seqscan=off`).
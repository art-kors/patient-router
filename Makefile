# patient-router · команды разработки
# Требование к оформлению: «скрипты для развертывания и настройки окружения»

.DEFAULT_GOAL := help
SHELL := /bin/bash

UV := uv
COMPOSE := $(UV) run

.PHONY: help
help: ## Показать доступные команды
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── окружение ──────────────────────────────────────────────────

.PHONY: install
install: ## Поставить зависимости из uv.lock
	$(UV) sync --frozen --all-extras

.PHONY: fmt
fmt: ## Отформатировать код
	$(COMPOSE) ruff format .

.PHONY: lint
lint: ## Проверить линтер и форматирование
	$(COMPOSE) ruff format --check .
	$(COMPOSE) ruff check .

.PHONY: typecheck
typecheck: ## Проверить типы
	$(COMPOSE) mypy app

# ── тесты ──────────────────────────────────────────────────────

.PHONY: test
test: ## Прогнать все тесты с покрытием
	$(COMPOSE) pytest

.PHONY: test-unit
test-unit: ## Только юнит-тесты (без БД)
	$(COMPOSE) pytest -m "not integration"

.PHONY: cov
cov: ## Отчёт о покрытии в HTML
	$(COMPOSE) pytest --cov=app --cov-report=html
	@echo "→ htmlcov/index.html"

.PHONY: pre-commit
pre-commit: ## Прогнать pre-commit по всем файлам
	pre-commit run --all-files

# ── база данных ────────────────────────────────────────────────

.PHONY: up
up: ## Поднять БД + приложение
	docker compose up -d --build

.PHONY: down
down: ## Остановить стек
	docker compose down

.PHONY: demo
demo: ## Поднять демо с модельным часом (порт 8010, без -E и правки .env)
	docker compose --profile demo up -d demo

.PHONY: demo-down
demo-down: ## Остановить демо-сервис
	docker compose --profile demo stop demo

.PHONY: clean
clean: ## Остановить стек и удалить volume с данными
	docker compose down -v

.PHONY: logs
logs: ## Логи сервисов
	docker compose logs -f

.PHONY: ps
ps: ## Статус контейнеров
	docker compose ps

.PHONY: migrate
migrate: ## Применить миграции
	$(COMPOSE) alembic upgrade head

.PHONY: migrate-down
migrate-down: ## Откатить все миграции
	$(COMPOSE) alembic downgrade base

.PHONY: migrate-new
migrate-new: ## Создать миграцию (make migrate-new M="описание")
	$(COMPOSE) alembic revision --autogenerate -m "$(M)"

.PHONY: migrate-check
migrate-check: ## Проверить расхождения моделей и схемы
	$(COMPOSE) alembic check

# ── проверка перед коммитом ─────────────────────────────────────

.PHONY: check
check: lint test ## Полная проверка перед коммитом

.PHONY: verify
verify: ## Всё, что делает CI (для локального запуска)
	$(MAKE) lint
	$(MAKE) test
	@echo "✓ локальная проверка пройдена (миграции и Docker проверяются в CI)"
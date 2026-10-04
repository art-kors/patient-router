"""Тесты того, что config/ попадает внутрь образа.

ЗАЧЕМ ЭТОТ ФАЙЛ
================
Регрессия, которую он ловит, была реальной: Dockerfile копировал app,
alembic и alembic.ini, но забывал про config/. Приложение при этом
стартовало, /health отвечал 200, а первая же попытка принять решение
о маршруте падала с «матрица маршрутизации не найдена».

Ловить это интеграционным тестом дорого: нужен docker и сборка образа.
Такие тесты в CI не запускаются, и регрессия спокойно возвращается.
Поэтому здесь статическая проверка Dockerfile — она дешёвая, работает
везде и падает ровно на том дефекте, который нас интересует.

Дополнительно проверяем, что путь в образе совпадает с тем, который
ожидает settings: config_dir = "config", WORKDIR = /app → /app/config.
"""

import re
from pathlib import Path

from app.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _prod_dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


def _dev_dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile.dev").read_text(encoding="utf-8")


def _instructions(dockerfile: str) -> list[str]:
    """Инструкции COPY в верхнем регистре (Dockerfile нечувствителен к нему)."""
    return [
        line.strip() for line in dockerfile.splitlines() if line.strip().upper().startswith("COPY ")
    ]


def _copies(dockerfile: str, source_suffix: str, from_stage: str | None = None) -> list[str]:
    """Инструкции COPY, копирующие указанный источник в рантайм-слой."""
    found = []
    for instruction in _instructions(dockerfile):
        parts = instruction.split()
        # пропускаем --chown=... и --from=... флаги, разбирая источники
        flags = [p for p in parts[1:] if p.startswith("--")]
        operands = [p for p in parts[1:] if not p.startswith("--")]
        if from_stage is not None and f"--from={from_stage}" not in flags:
            continue
        if from_stage is None and any(p.startswith("--from=") for p in flags):
            continue
        sources = operands[:-1]
        destination = operands[-1]
        # источник — либо "config", либо "/app/config" из сборочного слоя
        if any(s.rstrip("/") == source_suffix for s in sources):
            found.append(destination)
    return found


class TestProductionDockerfile:
    """prod-образ: config/ обязан попасть в финальный слой."""

    def test_конфиг_копируется_из_сборочного_слоя(self):
        dockerfile = _prod_dockerfile()
        """Runtime-слой берёт /app/config из builder — иначе в образе пусто."""
        destinations = _copies(dockerfile, "/app/config", from_stage="builder")
        assert destinations, (
            "Dockerfile: рантайм-слой не копирует config из сборочного. "
            "Без этого routing_matrix.json отсутствует в образе."
        )
        assert any(d.rstrip("/") == "/app/config" for d in destinations), (
            f"config копируется не в /app/config, а в {destinations}. "
            "settings.routing_matrix_path = 'config/routing_matrix.json', "
            "WORKDIR=/app → ожидаем /app/config."
        )

    def test_workdir_и_путь_настроек_согласованы(self):
        dockerfile = _prod_dockerfile()
        """Копируем туда, куда реально смотрит settings."""
        workdirs = re.findall(r"^WORKDIR\s+(\S+)", dockerfile, re.MULTILINE)
        assert workdirs, "в Dockerfile нет WORKDIR"
        # WORKDIR=/app и config_dir="config" → /app/config/routing_matrix.json
        assert any(w.rstrip("/") == "/app" for w in workdirs), (
            f"Ожидали WORKDIR /app (на него рассчитан settings.config_dir), получили {workdirs}"
        )
        assert Settings().routing_matrix_path == "config/routing_matrix.json"

    def test_конфиг_есть_в_сборочном_слое(self):
        dockerfile = _prod_dockerfile()
        """Builder тоже должен знать про config — оттуда runtime и берёт."""
        destinations = _copies(dockerfile, "config", from_stage=None)
        assert destinations, (
            "Dockerfile: builder не копирует ./config, значит и COPY --from=builder "
            "/app/config в рантайме не сработает."
        )

    def test_в_репозитории_есть_тот_самый_файл(self):
        """Страховка: тест бессмыслен, если файла нет и в исходниках."""
        assert (REPO_ROOT / "config" / "routing_matrix.json").is_file(), (
            "config/routing_matrix.json отсутствует в репозитории — "
            "копировать в образ нечего, и демо-стенд не заработает."
        )


class TestDevImage:
    """dev-образ: то же самое, плюс hot reload через монтирование."""

    def test_конфиг_копируется(self):
        dockerfile = _dev_dockerfile()
        assert _copies(dockerfile, "config"), (
            "Dockerfile.dev: не копирует config/, поэтому dev-образ "
            "не запустится, даже если монтирование забыли."
        )

    def test_в_compose_монтируется_read_only(self):
        """Hot reload матрицы: файл подменяется без пересборки образа."""
        compose = (REPO_ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        mounts = [
            line.strip() for line in compose.splitlines() if line.strip().startswith("- ./config")
        ]
        assert mounts, (
            "docker-compose.yaml: каталог config не смонтирован в dev-сервис — "
            "правка routing_matrix.json потребует пересборки образа."
        )
        assert any(":ro" in m for m in mounts), (
            f"config смонтирован не read-only: {mounts}. Приложение не должно писать в матрицу."
        )

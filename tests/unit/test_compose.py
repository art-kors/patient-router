"""Тесты compose-файлов: никаких жёстких имён и рабочее демо.

ЗАЧЕМ ЭТОТ ФАЙЛ
================
Регрессия ловилась трижды подряд: в compose были зашиты

    container_name: pr-db
    container_name: pr-app
    container_name: pr-app-dev
    volumes.pgdata.name: pr_pgdata

Из-за этого стенд не поднимался ни в одном из двух сценариев:

1. Вторая копия каталога рядом:
   ``Conflict. The container name "/pr-db" is already in use``
2. Каталог с другим именем (или чужой проект на той же машине):
   ``WARN volume "pr_pgdata" already exists but was created for project
   "patient-router"``

Compose умеет сам: контейнеры называются ``<проект>-<сервис>-1``, том —
``<проект>_pgdata``, а имя проекта задаётся флагом ``-p``. Значит,
правильный стенд вообще не должен содержать ни ``container_name``, ни
``name:`` у тома.

Вторая половина файла — про демо-сервис. Ключевая фича проекта
(``POST /api/v1/demo/clock/advance`` — 30 суток за секунду) не должна
требовать ни ``sudo -E``, ни правки ``.env``: у ``sudo`` на машине жюри
сбрасывается окружение (``env_reset``), и раньше единственным способом
включить модельное время был ``sudo -E docker compose up``. Теперь есть
отдельный сервис ``demo`` с жёстко заданным ``USE_MODEL_CLOCK=true`` и
собственным портом 8010, чтобы не драться с ``app`` на 8000.

Проверка статическая и без Docker: она обязана работать в CI и на машине
жюри, где ``docker`` может быть недоступен или запускаться только через
sudo. Структуру разбираем через PyYAML, а ``container_name`` дополнительно
ищем регуляркой по тексту: закомментированный дефект тоже считается
возвратом регрессии.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_COMPOSE = REPO_ROOT / "docker-compose.yaml"
INFRA_COMPOSE = REPO_ROOT / "infra" / "postgres" / "compose.yml"

# Комментарий «намеренно НЕТ container_name» дефектом не является, поэтому
# регулярка применяется к тексту без строк-комментариев.
_CONTAINER_NAME_RE = re.compile(r"^[ \t]*container_name\s*:", re.MULTILINE)
_COMMENT_LINE_RE = re.compile(r"^\s*#")

COMPOSE_FILES = [ROOT_COMPOSE, INFRA_COMPOSE]


def _text(path: Path) -> str:
    assert path.is_file(), (
        f"{path} отсутствует. Тест ловит возврат жёстких имён, а не пропажу "
        "файла: на пустом репозитории он был бы зелёным."
    )
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    """Текст compose без строк-комментариев."""
    return "\n".join(line for line in _text(path).splitlines() if not _COMMENT_LINE_RE.match(line))


def _load(path: Path) -> dict:
    data = yaml.safe_load(_text(path))
    assert isinstance(data, dict), f"{path.name}: корень файла — не отображение"
    return data


def _services(path: Path) -> dict:
    services = _load(path).get("services") or {}
    assert services, f"{path.name}: нет сервисов"
    return services


def _service(path: Path, name: str) -> dict:
    services = _services(path)
    assert name in services, (
        f"в {path.name} нет сервиса {name!r}, есть {sorted(services)}. "
        "Без него модельное время включается только через sudo -E."
    )
    return services[name]


def _env(path: Path, service: str) -> dict[str, str]:
    env = _service(path, service).get("environment") or {}
    assert isinstance(env, dict), f"{service}: environment — не отображение"
    return {str(k): str(v) for k, v in env.items()}


def _ports(path: Path, service: str) -> list[str]:
    return [str(p) for p in (_service(path, service).get("ports") or [])]


# ── жёсткие имена ────────────────────────────────────────────


class TestНетЖёсткихИмёнКонтейнеров:
    """container_name возвращает конфликт имён при любой второй копии."""

    @pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
    def test_container_name_отсутствует(self, path: Path):
        matches = _CONTAINER_NAME_RE.findall(_code(path))
        assert not matches, (
            f"{path.name}: найден container_name ({len(matches)} шт.). "
            "Compose сам назовёт контейнер <проект>-<сервис>-1; хардкод даёт "
            'Conflict. The container name "/pr-db" is already in use, '
            "как только рядом появляется вторая копия каталога."
        )

    @pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
    def test_у_сервисов_нет_ключа_container_name(self, path: Path):
        for name, spec in _services(path).items():
            assert "container_name" not in spec, (
                f"{path.name}: у сервиса {name!r} снова есть container_name — "
                "второй запуск стенда в соседнем каталоге упадёт."
            )

    @pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
    def test_имя_тома_не_зашито(self, path: Path):
        """``name:`` у тома привязывает его к чужому проекту навсегда."""
        volumes = _load(path).get("volumes") or {}
        for volume, spec in volumes.items():
            assert not (isinstance(spec, dict) and "name" in spec), (
                f"{path.name}: у тома {volume!r} задано name: {spec['name']!r}. "
                "Том должен называться <проект>_pgdata и жить вместе с проектом: "
                'иначе чужой каталог ругается "volume already exists but was '
                'created for project ...".'
            )

    def test_том_объявлен(self):
        """Том должен остаться: без него данные БД исчезают при каждом down -v."""
        volumes = _load(ROOT_COMPOSE).get("volumes") or {}
        assert "pgdata" in volumes, f"в docker-compose.yaml нет тома pgdata, есть {volumes}"

    def test_том_подключён_к_бд(self):
        """Том объявлен один раз и подключён к БД — ссылки не разъехались."""
        mounts = [str(v) for v in (_service(ROOT_COMPOSE, "db").get("volumes") or [])]
        assert "pgdata:/var/lib/postgresql/data" in mounts, (
            f"том pgdata больше не подключён к БД: {mounts}"
        )


# ── демо-сервис ──────────────────────────────────────────────


class TestДемоСервис:
    """docker compose --profile demo up -d demo — и всё работает."""

    def test_сервис_demo_существует_в_профиле_demo(self):
        spec = _service(ROOT_COMPOSE, "demo")
        assert spec.get("profiles") == ["demo"], (
            f"профили сервиса demo: {spec.get('profiles')}, ожидался только ['demo']. "
            "Сервис вне профиля стартует вместе с app и занимает лишний порт."
        )

    def test_модельное_время_включено_жёстко(self):
        """Ключевая проверка: значение не подставляется извне."""
        value = _env(ROOT_COMPOSE, "demo")["USE_MODEL_CLOCK"].strip().lower()
        assert value == "true", (
            f"USE_MODEL_CLOCK у demo = {value!r}, ожидалось 'true'. "
            "Подстановка вида ${USE_MODEL_CLOCK:-false} на машине жюри "
            "недоступна: sudo сбрасывает переменные окружения, и "
            "/api/v1/demo/clock/advance отдаёт 409 CLOCK_NOT_MOCK."
        )
        assert "$" not in value and "{" not in value, (
            f"USE_MODEL_CLOCK у demo = {value!r} содержит подстановку. "
            "Демо обязано подниматься без правки .env и без флагов."
        )

    def test_демо_не_требует_правки_env_и_флажков(self):
        """В блоке demo не осталось подстановок вместо жёсткой константы."""
        env = _env(ROOT_COMPOSE, "demo")
        assert "USE_MODEL_CLOCK" in env, "у demo исчез USE_MODEL_CLOCK из окружения"
        assert not [k for k in env if k != "USE_MODEL_CLOCK" and k.startswith("USE_MODEL_CLOCK")], (
            "в окружении demo появился второй источник USE_MODEL_CLOCK"
        )

    def test_у_демо_свой_порт_8010(self):
        ports = _ports(ROOT_COMPOSE, "demo")
        assert ports, "у сервиса demo нет проброса порта — до него не достучаться с хоста"
        assert "${DEMO_PORT:-8010}:8000" in ports, (
            f"порты demo = {ports}. Ожидался проброс ${{DEMO_PORT:-8010}}:8000: "
            "свой порт, иначе демо бьётся за 8000 с app."
        )

    def test_демо_не_делит_порт_с_app(self):
        def published(entries: list[str]) -> set[str]:
            return {e.split(":")[0] if ":" in e else e for e in entries}

        demo = published(_ports(ROOT_COMPOSE, "demo"))
        app = published(_ports(ROOT_COMPOSE, "app"))
        assert demo, "у demo нечего проверять"
        overlap = demo & app
        assert not overlap, (
            f"app и demo публикуют один и тот же порт {sorted(overlap)}: "
            "docker compose не сможет поднять оба контейнера."
        )

    def test_демо_собирается_из_prod_образа(self):
        build = _service(ROOT_COMPOSE, "demo").get("build") or {}
        dockerfile = str(build.get("dockerfile", ""))
        assert dockerfile == "Dockerfile", (
            f"демо собирается из {dockerfile!r}, ожидался Dockerfile. "
            "dev-образ нужен для hot reload, а не для стенда жюри."
        )

    def test_демо_подключён_к_той_же_бд(self):
        spec = _service(ROOT_COMPOSE, "demo")
        assert _env(ROOT_COMPOSE, "demo")["POSTGRES_HOST"] == "db", (
            "демо должен видеть БД по имени сервиса db, а не по localhost"
        )
        depends = spec.get("depends_on") or {}
        assert "db" in depends, f"демо не объявляет depends_on: db — {depends}"

    def test_демо_ждёт_готовности_бд(self):
        """condition: service_started — это гонка: миграции упадут на пустой БД."""
        for service in ("app", "demo"):
            depends = _service(ROOT_COMPOSE, service).get("depends_on") or {}
            db = depends.get("db")
            condition = (db or {}).get("condition") if isinstance(db, dict) else None
            assert condition == "service_healthy", (
                f"{service}: depends_on.db.condition = {condition!r}, ожидалось "
                "service_healthy. При service_started сервис стартует против "
                "непостованной БД — alembic отвалится по connection refused."
            )

    def test_демо_накатывает_миграции_сам(self):
        """Иначе жюри получит 500 на первом же advance: в БД нет таймеров."""
        command = _service(ROOT_COMPOSE, "demo").get("command")
        joined = command if isinstance(command, str) else " ".join(map(str, command or []))
        assert "alembic upgrade head" in joined, (
            "сервис demo не накатывает миграции при старте. Корневой compose "
            "этого не делает осознанно, но демо — одноразовый стенд, и "
            "docker compose --profile demo up -d demo должен быть единственной "
            "командой перед показом."
        )


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.dev"])
@pytest.mark.parametrize(
    ("stage", "instruction"),
    [
        ("builder", "COPY scripts ./scripts"),
        ("runtime", "COPY --from=builder --chown=appuser:appuser /app/scripts /app/scripts"),
    ],
)
def test_scripts_копируются_в_оба_этапа(dockerfile: str, stage: str, instruction: str):
    stages: dict[str, list[str]] = {}
    current_stage = ""
    for line in _code(REPO_ROOT / dockerfile).splitlines():
        line = line.strip()
        if line.upper().startswith("FROM "):
            current_stage = line.split()[-1].lower()
            stages[current_stage] = []
        elif current_stage:
            stages[current_stage].append(line)
    assert instruction in stages.get(stage, []), (
        f"{dockerfile}: этап {stage} не содержит {instruction!r}. "
        "Без scripts в образе невозможно запустить seed_demo.py и make_demo_data.py."
    )


# ── обратная совместимость ───────────────────────────────────


class TestПрочееНеСломалось:
    def test_корневой_compose_поднимает_db_и_app(self):
        """Связка docker-compose.yaml: db + app должна остаться."""
        services = set(_services(ROOT_COMPOSE))
        assert {"db", "app"} <= services, f"в docker-compose.yaml сервисы: {sorted(services)}"

    def test_dev_остался_в_профиле(self):
        spec = _service(ROOT_COMPOSE, "dev")
        assert spec.get("profiles") == ["dev"], f"профили dev: {spec.get('profiles')}"

    def test_app_остался_боевым_по_часам(self):
        """У app модельное время по-прежнему выключено по умолчанию."""
        value = _env(ROOT_COMPOSE, "app")["USE_MODEL_CLOCK"]
        assert "${USE_MODEL_CLOCK:-false}" in value, (
            f"USE_MODEL_CLOCK у app = {value!r}: для боевого режима ожидалось "
            "${USE_MODEL_CLOCK:-false}, иначе прод-стенд уехал бы в симуляцию."
        )

    def test_стенд_бд_остался_отдельным(self):
        """infra/postgres/compose.yml — самостоятельный стенд только для БД."""
        services = set(_services(INFRA_COMPOSE))
        assert "db" in services, f"в infra/postgres/compose.yml сервисы: {sorted(services)}"
        assert "app" not in services, (
            "в стенде infra/postgres не должно быть приложения: он поднимает "
            "эталонную схему с сидами, а корневой compose — пустую БД под миграции"
        )
        assert "db-init" in services, (
            "потерян одноразовый сервис db-init для переприменения init-скриптов"
        )

    def test_стенд_бд_по_прежнему_с_инициализацией(self):
        mounts = [str(v) for v in (_service(INFRA_COMPOSE, "db").get("volumes") or [])]
        assert any(m.startswith("./init") for m in mounts), (
            f"init-скрипты больше не монтируются в стенд БД: {mounts}"
        )

    def test_стенд_бд_не_переехал_на_другой_порт(self):
        """Стенд БД продолжает публиковать 5433 на хосте."""
        ports = _ports(INFRA_COMPOSE, "db")
        assert "${POSTGRES_PORT:-5433}:5432" in ports, f"порты БД в стенде: {ports}"

    def test_оба_compose_читаются(self):
        """Оба файла — валидный YAML (иначе compose их просто не примет)."""
        for path in COMPOSE_FILES:
            assert _load(path).get("services"), f"{path.name}: нет сервисов"

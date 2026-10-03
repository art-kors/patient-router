"""восстановлены 4 индекса, потерянные при переносе схемы в ORM

Ревизия ID: 2f8c1d4a7b93
Revises: e18a439f9a84
Создано: 2026-10-03

ЗАЧЕМ ЭТА МИГРАЦИЯ
==================
Четыре индекса были описаны в эталонной SQL-схеме (infra/postgres/init/
01_schema.sql), но потерялись при переносе в ORM-модели. На это указали
тесты на схему, а затем подтвердил CI: `alembic check` обнаружил, что
модели и база расходятся.

ЧТО ВОССТАНАВЛИВАЕМ
===================
1. idx_route_unfinished (route: patient_id, created_at WHERE closed_at IS NULL)
   Баннер «Незавершённый клинический маршрут» и отчёт о потерях.
   Пациент возвращается в клинику через 2 месяца — без этого индекса
   каждый визит сканирует всю таблицу маршрутов.

2. idx_hosp_no_date (hospitalization: route_id WHERE scheduled_date IS NULL
   AND status = 'referred')
   Контроль этапа 9 кейса: «направление создано → назначена ли дата
   госпитализации». Сканирует только незакрытые направления.

3. idx_study_raw_text_trgm (study: raw_text, GIN pg_trgm)
4. idx_finding_name_trgm (finding: finding, GIN pg_trgm)
   Полнотекстовый поиск по размытым находкам — нужен при отладке
   словарей и разметке протоколов.

ПОЧЕМУ ОТДЕЛЬНО, А НЕ В НАЧАЛЬНОЙ МИГРАЦИИ
===========================================
Начальная миграция применена на нескольких окружениях (в том числе на
GitHub Actions). Переписывать её означало бы рассинхронизировать их.
Правило: применённые миграции не трогаем, добавляем новые.

ПОЧЕМУ РАСШИРЕНИЕ pg_trgm ЗДЕСЬ
==============================
Расширение создаётся в этой миграции, а не в начальной: оно нужно
только для двух GIN-индексов. Идемпотентно (IF NOT EXISTS), поэтому
повторное применение безопасно.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2f8c1d4a7b93"
down_revision: str | None = "e18a439f9a84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создать расширение и четыре индекса."""
    # pg_trgm нужен для операторов gin_trgm_ops в индексах 3 и 4.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Баннер незавершённого маршрута: незакрытые маршруты пациента.
    op.create_index(
        "idx_route_unfinished",
        "route",
        ["patient_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    # Направления на госпитализацию, по которым дата ещё не назначена.
    op.create_index(
        "idx_hosp_no_date",
        "hospitalization",
        ["route_id"],
        unique=False,
        postgresql_where=sa.text("scheduled_date IS NULL AND status = 'referred'"),
    )

    # Полнотекстовый поиск по тексту протокола и по находкам.
    op.create_index(
        "idx_study_raw_text_trgm",
        "study",
        ["raw_text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"raw_text": "gin_trgm_ops"},
    )
    op.create_index(
        "idx_finding_name_trgm",
        "finding",
        ["finding"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"finding": "gin_trgm_ops"},
    )


def downgrade() -> None:
    """Удалить индексы.

    Расширение pg_trgm НЕ удаляем: после DROP SCHEMA public CASCADE оно
    исчезло бы вместе со схемой, а при обычном downgrade осталось бы
    висеть без пользы — это безвредно и безопаснее, чем снести
    расширение, которым могут пользоваться чужие объекты.
    """
    op.drop_index("idx_finding_name_trgm", table_name="finding")
    op.drop_index("idx_study_raw_text_trgm", table_name="study")
    op.drop_index("idx_hosp_no_date", table_name="hospitalization")
    op.drop_index("idx_route_unfinished", table_name="route")

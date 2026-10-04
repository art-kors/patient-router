"""Обезличенный журнал разборов и врачебная разметка."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "7ab91c230def"
down_revision: str = "2f8c1d4a7b93"
branch_labels = None
depends_on = None


def tables():
    """Неизменяемое описание ревизии, независимое от будущих ORM-моделей."""
    metadata = sa.MetaData()
    runs = sa.Table(
        "analysis_run",
        metadata,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("request_key", sa.String(64), unique=True),
        sa.Column("text_hash", sa.String(64), nullable=False, index=True),
        sa.Column("text_length", sa.Integer, nullable=False),
        sa.Column("study_type", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Numeric, nullable=False),
        sa.Column("decoder_used", sa.String(32), nullable=False),
        sa.Column("fallback", sa.Boolean, nullable=False),
        sa.Column("findings", sa.JSON, nullable=False),
        sa.Column("matches", sa.JSON, nullable=False),
    )
    feedback = sa.Table(
        "analysis_feedback",
        metadata,
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "analysis_id",
            UUID(as_uuid=True),
            sa.ForeignKey("analysis_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger_id", sa.String(128), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analysis_id", "trigger_id", name="uq_analysis_feedback_pair"),
        sa.CheckConstraint(
            "label IN ('confirmed', 'false_positive', 'missed')", name="feedback_label"
        ),
    )
    return runs, feedback


def upgrade():
    """Создать таблицы; повторный вызов безопасен."""
    for table in tables():
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    """Удалить сначала зависимую разметку, затем журнал."""
    for table in reversed(tables()):
        table.drop(op.get_bind(), checkfirst=True)

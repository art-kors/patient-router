"""Тесты ORM-моделей без подключения к БД.

Проверяем то, что ломается тихо и дорого:
- обязательные ограничения объявлены (защиты от дублей, CHECK, enum);
- частичные индексы на месте (на них держится модельное время);
- маршрут выводит свойства, которые считает доменный слой.

Сама схема проверяется интеграционными тестами с реальной БД.
"""

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.models import (
    AuditLog,
    Base,
    CloseReason,
    Finding,
    Hospitalization,
    MisEvent,
    Notification,
    Patient,
    Protocol,
    Route,
    RouteStatus,
    Study,
    Tactics,
    Task,
    Timer,
    TimerType,
    TriggerDef,
    TriggerMatch,
)


class TestRouteStatus:
    """19 состояний — как в автомате из заметки 03."""

    def test_количество_состояний(self):
        assert len(RouteStatus) == 19

    def test_все_значения_строковые(self):
        assert all(isinstance(s.value, str) for s in RouteStatus)

    def test_есть_стартовое_и_финальные(self):
        values = {s.value for s in RouteStatus}
        assert "created" in values
        assert {"cancelled", "route_not_realized", "closed_by_patient"} <= values

    def test_конфликтующих_значений_нет(self):
        values = [s.value for s in RouteStatus]
        assert len(values) == len(set(values))


class TestEnums:
    def test_timer_type(self):
        assert {t.value for t in TimerType} == {
            "notify_initial",
            "notify_reminder",
            "create_task",
            "escalate",
            "close_route",
            "schedule_followup",
        }

    def test_tactics_шесть_вариантов(self):
        """Кейс: врач обязан выбрать одну из шести тактик."""
        assert len(Tactics) == 6
        assert {t.value for t in Tactics} == {
            "surgery_indicated",
            "additional_exam",
            "observation",
            "no_surgery",
            "patient_refused",
            "refer_other_specialty",
        }

    def test_close_reason(self):
        assert "not_realized" in {c.value for c in CloseReason}


class TestDeduplicationConstraints:
    """Требование кейса: повторная доставка события не создаёт дубль."""

    @pytest.mark.parametrize(
        ("table", "column"),
        [
            (Route, "trigger_match_id"),
        ],
    )
    def test_уникальность_на_уровне_таблицы(self, table, column):
        uniques = [
            c
            for c in table.__table__.constraints
            if isinstance(c, UniqueConstraint) and column in [col.name for col in c.columns]
        ]
        assert uniques, f"{table.__tablename__}.{column} должен быть UNIQUE"

    def test_trigger_match_уникален_по_протоколу_и_триггеру(self):
        names = {
            c.name for c in TriggerMatch.__table__.constraints if isinstance(c, UniqueConstraint)
        }
        assert "uq_trigger_match_protocol_trigger" in names

    def test_timer_уникален_по_маршруту_типу_и_времени(self):
        names = {c.name for c in Timer.__table__.constraints if isinstance(c, UniqueConstraint)}
        assert "uq_timer_route_type_due" in names

    def test_event_id_уникален_через_unique_index(self):
        """event_id уникален, но объявлен как Index(unique=True), не Constraint."""
        idx = next(i for i in MisEvent.__table__.indexes if i.name == "uq_mis_event_event_id")
        assert idx.unique is True
        assert [c.name for c in idx.columns] == ["event_id"]


class TestCheckConstraints:
    def test_приоритет_в_диапазоне_1_4(self):
        checks = [
            c
            for c in TriggerDef.__table__.constraints
            if isinstance(c, CheckConstraint) and "priority" in str(c.sqltext)
        ]
        assert checks
        assert "BETWEEN 1 AND 4" in str(checks[0].sqltext)

    def test_пол_пациента_ограничен(self):
        checks = [
            c
            for c in Patient.__table__.constraints
            if isinstance(c, CheckConstraint) and "sex" in str(c.sqltext)
        ]
        assert checks
        assert "'M'" in str(checks[0].sqltext) and "'F'" in str(checks[0].sqltext)

    def test_suppression_reason_из_списка(self):
        """CHECK объявлен на уровне колонки, поэтому ищем его в DDL, а не в constraints.

        SQLAlchemy не выносит column-level CHECK в table.constraints —
        надёжнее проверить итоговый DDL.
        """
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        ddl = str(CreateTable(TriggerMatch.__table__).compile(dialect=postgresql.dialect()))
        assert "CHECK" in ddl
        assert "negative_context" in ddl
        assert "threshold_not_met" in ddl


class TestPartialIndexes:
    """На этих индексах держится модельное время и баннер незавершённого маршрута."""

    def test_timer_due_частичный_по_not_fired(self):
        index = next(i for i in Timer.__table__.indexes if i.name == "idx_timer_due")
        where = str(index.dialect_options["postgresql"]["where"])
        assert "fired" in where

    def test_route_unfinished_частичный(self):
        index = next(i for i in Route.__table__.indexes if i.name == "idx_route_unfinished")
        assert "closed_at IS NULL" in str(index.dialect_options["postgresql"]["where"])

    def test_mis_event_unprocessed_частичный(self):
        index = next(i for i in MisEvent.__table__.indexes if i.name == "idx_mis_event_unprocessed")
        assert "processed_at IS NULL" in str(index.dialect_options["postgresql"]["where"])


class TestServerDefaults:
    """Python-side default не работает для прямого SQL — нужен серверный."""

    @pytest.mark.parametrize(
        ("model", "column"),
        [
            (TriggerDef, "synonyms"),
            (TriggerDef, "negative_contexts"),
            (TriggerDef, "thresholds"),
            (TriggerDef, "target_sla_days"),
            (Protocol, "facts"),
            (MisEvent, "payload"),
            (Notification, "delivery_status"),
            (Route, "status"),
        ],
    )
    def test_есть_server_default(self, model, column):
        col = model.__table__.c[column]
        assert col.server_default is not None, f"{model.__tablename__}.{column} без server_default"

    def test_uuid_pk_генерируется_на_стороне_бд(self):
        for model in (Patient, Study, Protocol, Finding, Route, Timer, Task):
            col = model.__table__.c["id"]
            assert isinstance(col.type, PGUUID)
            assert col.server_default is not None


class TestForeignKeys:
    @pytest.mark.parametrize(
        ("parent", "child", "column"),
        [
            (Patient, Study, "patient_id"),
            (Study, Protocol, "study_id"),
            (Protocol, TriggerMatch, "protocol_id"),
            (TriggerDef, TriggerMatch, "trigger_def_id"),
            (Route, Timer, "route_id"),
            (Route, Notification, "route_id"),
            (Route, Task, "route_id"),
        ],
    )
    def test_связи_на_foreign_key(self, parent, child, column):
        fks = child.__table__.c[column].foreign_keys
        assert fks
        assert list(fks)[0].column.table is parent.__table__

    def test_каскадное_удаление_от_patient(self):
        """Пациент удалён → каскадом уходят его исследования и маршруты."""
        fk = next(iter(Study.__table__.c["patient_id"].foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_проверка_ondelete_не_мутирует_метаданные(self):
        """Тест читает ondelete, но не удаляет FK из общих метаданных.

        Регрессия: .pop() на foreign_keys ломал инициализацию маппингов
        для всех следующих тестов в том же процессе.
        """
        column = Study.__table__.c["patient_id"]
        before = len(list(column.foreign_keys))
        next(iter(column.foreign_keys))
        assert len(list(column.foreign_keys)) == before, "тест не должен менять метаданные"


class TestMetadataIntegrity:
    def test_все_таблицы_имеют_primary_key(self):
        for table in Base.metadata.sorted_tables:
            assert table.primary_key.columns, f"{table.name} без первичного ключа"

    def test_количество_таблиц_20(self):
        assert len(Base.metadata.tables) == 20

    def test_нет_таблиц_без_имени(self):
        assert all(t.name for t in Base.metadata.tables.values())

    def test_все_модели_экспортированы(self):
        from app import models

        for name in models.__all__:
            assert hasattr(models, name), f"{name} отсутствует в __all__"


class TestRouteDomain:
    """Доменные свойства, которые считает сервисный слой."""

    def test_статус_по_умолчанию_created(self):
        """Проверяем через DDL: server_default.arg — TextClause, строковым он не станет."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        ddl = str(CreateTable(Route.__table__).compile(dialect=postgresql.dialect()))
        assert "route_status" in ddl
        assert "'created'" in ddl

    def test_у_route_есть_target_date(self):
        assert "target_date" in Route.__table__.c

    def test_у_audit_log_есть_кто_что_когда_основание(self):
        cols = set(AuditLog.__table__.c.keys())
        assert {"actor", "action", "created_at", "basis"} <= cols

    def test_у_notification_есть_канал_и_текст(self):
        cols = set(Notification.__table__.c.keys())
        assert {"channel", "body", "template_code", "delivery_status"} <= cols

    def test_hospitalization_разделяет_план_и_факт(self):
        """Контроль этапа 9: направление есть → дата назначена?"""
        cols = set(Hospitalization.__table__.c.keys())
        assert {"scheduled_date", "actual_date"} <= cols

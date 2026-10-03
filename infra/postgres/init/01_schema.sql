-- ============================================================
-- СМ-Клиника · patient-router · схема БД
-- Источник: заметка 07 «Модель данных» (16 сущностей, 4 группы)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- Справочники
-- ============================================================

CREATE TABLE specialty (
    id          SERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    is_surgical BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE clinic (
    id      SERIAL PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,
    name    TEXT NOT NULL,
    address TEXT
);

CREATE TABLE doctor (
    id           SERIAL PRIMARY KEY,
    external_id  TEXT UNIQUE,
    full_name    TEXT NOT NULL,
    specialty_id INTEGER REFERENCES specialty (id),
    clinic_id    INTEGER REFERENCES clinic (id)
);

CREATE INDEX idx_doctor_specialty ON doctor (specialty_id);
CREATE INDEX idx_doctor_clinic ON doctor (clinic_id);

-- ============================================================
-- Группа 1 · Данные исследования — что написал врач УЗИ
-- ============================================================

CREATE TABLE patient (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id      TEXT NOT NULL UNIQUE,
    anonymized_hash  TEXT NOT NULL UNIQUE,
    age              SMALLINT,
    sex              CHAR(1) CHECK (sex IN ('M', 'F')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE study (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id    UUID NOT NULL REFERENCES patient (id) ON DELETE CASCADE,
    study_type    TEXT NOT NULL,
    study_date    DATE NOT NULL,
    document_ref  TEXT,
    raw_text      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'received'
                  CHECK (status IN ('received', 'extracted', 'processed', 'failed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_study_patient ON study (patient_id);
CREATE INDEX idx_study_date ON study (study_date);
CREATE INDEX idx_study_type ON study (study_type);
CREATE INDEX idx_study_raw_text_trgm ON study USING gin (raw_text gin_trgm_ops);

CREATE TABLE protocol (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    study_id       UUID NOT NULL REFERENCES study (id) ON DELETE CASCADE,
    signed_at      TIMESTAMPTZ NOT NULL,
    doctor_id      TEXT,
    facts          JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_corrected   BOOLEAN NOT NULL DEFAULT FALSE,
    is_cancelled   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_protocol_study ON protocol (study_id);
CREATE INDEX idx_protocol_facts ON protocol USING gin (facts jsonb_path_ops);

CREATE TABLE finding (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    study_id         UUID NOT NULL REFERENCES study (id) ON DELETE CASCADE,
    organ            TEXT,
    finding          TEXT NOT NULL,
    laterality       TEXT,
    size_mm          NUMERIC(6, 1),
    classification   TEXT,
    quote            TEXT NOT NULL,
    char_start       INTEGER,
    char_end         INTEGER,
    confidence       NUMERIC(4, 3),
    in_negative_ctx  BOOLEAN NOT NULL DEFAULT FALSE,
    source_section   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_finding_study ON finding (study_id);
CREATE INDEX idx_finding_name_trgm ON finding USING gin (finding gin_trgm_ops);

-- ============================================================
-- Группа 2 · Правила и срабатывания — что решила система
-- ============================================================

CREATE TABLE trigger_def (
    id                 SERIAL PRIMARY KEY,
    trigger_id         TEXT NOT NULL UNIQUE,
    display_name       TEXT NOT NULL,
    source_study       TEXT NOT NULL,
    synonyms           JSONB NOT NULL DEFAULT '[]'::jsonb,
    negative_contexts  JSONB NOT NULL DEFAULT '[]'::jsonb,
    thresholds         JSONB NOT NULL DEFAULT '{}'::jsonb,
    specialty_id       INTEGER REFERENCES specialty (id),
    potential_route    TEXT,
    target_sla_days    INTEGER NOT NULL DEFAULT 14,
    department         TEXT,
    priority           SMALLINT NOT NULL DEFAULT 3
                       CHECK (priority BETWEEN 1 AND 4),
    emergency_flag     BOOLEAN NOT NULL DEFAULT FALSE,
    version            INTEGER NOT NULL DEFAULT 1,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_trigger_def_specialty ON trigger_def (specialty_id);
CREATE INDEX idx_trigger_def_active ON trigger_def (is_active);
CREATE INDEX idx_trigger_def_synonyms ON trigger_def USING gin (synonyms jsonb_path_ops);

CREATE TABLE trigger_match (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    protocol_id       UUID NOT NULL REFERENCES protocol (id) ON DELETE CASCADE,
    trigger_def_id    INTEGER NOT NULL REFERENCES trigger_def (id),
    finding_id        UUID REFERENCES finding (id) ON DELETE SET NULL,
    fired             BOOLEAN NOT NULL DEFAULT FALSE,
    suppressed        BOOLEAN NOT NULL DEFAULT FALSE,
    suppression_reason TEXT
                      CHECK (suppression_reason IS NULL OR
                             suppression_reason IN ('negative_context',
                                                   'threshold_not_met',
                                                   'study_type_mismatch',
                                                   'low_confidence')),
    applied_rule      TEXT NOT NULL,
    confidence        NUMERIC(4, 3),
    explanation       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- гарантия отсутствия дублей при исправлении протокола
    CONSTRAINT uq_trigger_match_protocol_trigger UNIQUE (protocol_id, trigger_def_id)
);

CREATE INDEX idx_trigger_match_protocol ON trigger_match (protocol_id);
CREATE INDEX idx_trigger_match_finding ON trigger_match (finding_id);
CREATE INDEX idx_trigger_match_fired ON trigger_match (fired) WHERE fired;
CREATE INDEX idx_trigger_match_explanation ON trigger_match USING gin (explanation jsonb_path_ops);

-- ============================================================
-- Сквозное · Входящие события МИС (идемпотентность по event_id)
-- ============================================================

CREATE TABLE mis_event (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id       TEXT NOT NULL UNIQUE,
    event_type     TEXT NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    patient_id     UUID REFERENCES patient (id) ON DELETE SET NULL,
    study_id       UUID REFERENCES study (id) ON DELETE SET NULL,
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version INTEGER NOT NULL DEFAULT 1,
    processed_at   TIMESTAMPTZ,
    is_duplicate   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_mis_event_type_time ON mis_event (event_type, occurred_at);
CREATE INDEX idx_mis_event_patient ON mis_event (patient_id);
CREATE INDEX idx_mis_event_unprocessed ON mis_event (processed_at) WHERE processed_at IS NULL;

-- ============================================================
-- Группа 3 · Маршрут — что происходит с пациентом
-- ============================================================

CREATE TYPE route_status AS ENUM (
    'created', 'notified', 'awaiting_booking', 'booked', 'visit_done',
    'decision_pending', 'booking_required', 'no_show', 'referred',
    'hospitalization_scheduled', 'hospitalized', 'operated', 'discharged',
    'followup_scheduled', 'followup_done', 'route_not_realized',
    'closed_by_patient', 'closed_no_operation', 'cancelled'
);

CREATE TYPE close_reason AS ENUM (
    'not_realized', 'patient_refused', 'no_operation',
    'protocol_cancelled', 'trigger_withdrawn', 'other'
);

CREATE TYPE tactics AS ENUM (
    'surgery_indicated', 'additional_exam', 'observation',
    'no_surgery', 'patient_refused', 'refer_other_specialty'
);

CREATE TYPE timer_type AS ENUM (
    'notify_initial', 'notify_reminder', 'create_task',
    'escalate', 'close_route', 'schedule_followup'
);

CREATE TABLE route (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patient (id) ON DELETE CASCADE,
    trigger_match_id UUID REFERENCES trigger_match (id) ON DELETE SET NULL,
    specialty_id    INTEGER REFERENCES specialty (id),
    clinic_id       INTEGER REFERENCES clinic (id),
    status          route_status NOT NULL DEFAULT 'created',
    target_date     DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    close_reason    close_reason,
    -- ★ один маршрут на (протокол, триггер): защита от дублей
    CONSTRAINT uq_route_trigger_match UNIQUE (trigger_match_id)
);

CREATE INDEX idx_route_patient ON route (patient_id);
CREATE INDEX idx_route_status ON route (status);
CREATE INDEX idx_route_specialty ON route (specialty_id);
CREATE INDEX idx_route_target ON route (target_date) WHERE closed_at IS NULL;
-- источник баннера «Незавершённый клинический маршрут»
CREATE INDEX idx_route_unfinished ON route (patient_id, created_at)
    WHERE closed_at IS NULL AND status NOT IN ('cancelled');

CREATE TABLE route_step (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id   UUID NOT NULL REFERENCES route (id) ON DELETE CASCADE,
    step_no    INTEGER NOT NULL,
    status     TEXT NOT NULL,
    entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_at     TIMESTAMPTZ,
    CONSTRAINT uq_route_step UNIQUE (route_id, step_no)
);

CREATE INDEX idx_route_step_route ON route_step (route_id, step_no);
CREATE INDEX idx_route_step_due ON route_step (status, due_at);

CREATE TABLE timer (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id   UUID NOT NULL REFERENCES route (id) ON DELETE CASCADE,
    timer_type timer_type NOT NULL,
    due_at     TIMESTAMPTZ NOT NULL,
    fired      BOOLEAN NOT NULL DEFAULT FALSE,
    fired_at   TIMESTAMPTZ,
    channel    TEXT,
    -- защита от двойного срабатывания и от дублей при пересоздании
    CONSTRAINT uq_timer_route_type_due UNIQUE (route_id, timer_type, due_at)
);

-- ★ частичный индекс: выборка due-таймеров за одну операцию (Clock.advance)
CREATE INDEX idx_timer_due ON timer (due_at) WHERE NOT fired;
CREATE INDEX idx_timer_route ON timer (route_id);

CREATE TABLE task (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id      UUID NOT NULL REFERENCES route (id) ON DELETE CASCADE,
    task_type     TEXT NOT NULL,
    assignee_role TEXT NOT NULL,
    priority      SMALLINT NOT NULL DEFAULT 3,
    due_at        TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'in_progress', 'done', 'postponed', 'cancelled')),
    result        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);

CREATE INDEX idx_task_queue ON task (assignee_role, status, due_at);
CREATE INDEX idx_task_route ON task (route_id);
CREATE INDEX idx_task_overdue ON task (due_at) WHERE status IN ('open', 'in_progress');

CREATE TABLE notification (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id        UUID NOT NULL REFERENCES route (id) ON DELETE CASCADE,
    channel         TEXT NOT NULL
                    CHECK (channel IN ('lk', 'push', 'sms', 'task')),
    template_code   TEXT NOT NULL,
    body            TEXT NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivery_status TEXT NOT NULL DEFAULT 'sent'
                    CHECK (delivery_status IN ('sent', 'delivered', 'failed', 'suppressed')),
    is_emergency    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_notification_route ON notification (route_id, sent_at);
CREATE INDEX idx_notification_channel ON notification (channel, sent_at);

-- ============================================================
-- Группа 4 · Клинический контур — что реально произошло
-- ============================================================

CREATE TABLE appointment (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id     UUID NOT NULL REFERENCES route (id) ON DELETE CASCADE,
    doctor_id    INTEGER REFERENCES doctor (id),
    clinic_id    INTEGER REFERENCES clinic (id),
    slot_ref     TEXT,
    starts_at    TIMESTAMPTZ NOT NULL,
    is_online    BOOLEAN NOT NULL DEFAULT FALSE,
    visit_status TEXT NOT NULL DEFAULT 'booked'
                 CHECK (visit_status IN ('booked', 'completed',
                                         'cancelled', 'no_show')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_appointment_route ON appointment (route_id);
CREATE INDEX idx_appointment_doctor_time ON appointment (doctor_id, starts_at);
CREATE INDEX idx_appointment_status ON appointment (visit_status);

CREATE TABLE hospitalization (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id      UUID NOT NULL REFERENCES route (id) ON DELETE CASCADE,
    scheduled_date DATE,
    actual_date    DATE,
    ward           TEXT,
    status         TEXT NOT NULL DEFAULT 'referred'
                  CHECK (status IN ('referred', 'scheduled', 'admitted',
                                    'discharged', 'cancelled')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_hosp_route ON hospitalization (route_id);
-- контроль «назначена ли дата ≤ 3 рабочих дней»
CREATE INDEX idx_hosp_no_date ON hospitalization (route_id)
    WHERE scheduled_date IS NULL AND status = 'referred';

CREATE TABLE surgery (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hospitalization_id UUID NOT NULL REFERENCES hospitalization (id) ON DELETE CASCADE,
    service_code       TEXT NOT NULL,
    performed_at       TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_surgery_hosp ON surgery (hospitalization_id);
CREATE INDEX idx_surgery_performed ON surgery (performed_at);

CREATE TABLE followup (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id    UUID NOT NULL REFERENCES route (id) ON DELETE CASCADE,
    target_date DATE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled', 'confirmed', 'completed', 'missed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_followup_route ON followup (route_id);
CREATE INDEX idx_followup_target ON followup (target_date) WHERE status IN ('scheduled', 'confirmed');

-- ============================================================
-- Сквозное · Журнал действий (кто, что, когда, на каком основании)
-- ============================================================

CREATE TABLE audit_log (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id   UUID REFERENCES route (id) ON DELETE CASCADE,
    study_id   UUID REFERENCES study (id) ON DELETE CASCADE,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    basis      TEXT,
    details    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_route_time ON audit_log (route_id, created_at);
CREATE INDEX idx_audit_actor_time ON audit_log (actor, created_at);
CREATE INDEX idx_audit_action ON audit_log (action, created_at);

-- ============================================================
-- Представления для дашборда
-- ============================================================

-- Воронка из кейса: 10 переходов
CREATE VIEW v_route_funnel AS
SELECT
    count(*) FILTER (WHERE status <> 'cancelled')                          AS triggered,
    count(*) FILTER (WHERE status NOT IN ('created', 'cancelled'))         AS notified,
    count(*) FILTER (WHERE status IN ('booked', 'visit_done', 'no_show',
                                     'booking_required', 'referred',
                                     'hospitalization_scheduled', 'hospitalized',
                                     'operated', 'discharged',
                                     'followup_scheduled', 'followup_done',
                                     'route_not_realized', 'closed_by_patient',
                                     'closed_no_operation'))               AS booked,
    count(*) FILTER (WHERE status IN ('visit_done', 'decision_pending', 'referred',
                                     'hospitalization_scheduled', 'hospitalized',
                                     'operated', 'discharged',
                                     'followup_scheduled', 'followup_done',
                                     'route_not_realized', 'closed_by_patient',
                                     'closed_no_operation'))               AS visit_done,
    count(*) FILTER (WHERE status IN ('referred', 'hospitalization_scheduled',
                                     'hospitalized', 'operated', 'discharged',
                                     'followup_scheduled', 'followup_done')) AS surgery_suggested,
    count(*) FILTER (WHERE status IN ('hospitalization_scheduled', 'hospitalized',
                                     'operated', 'discharged',
                                     'followup_scheduled', 'followup_done')) AS referral,
    count(*) FILTER (WHERE status IN ('hospitalized', 'operated', 'discharged',
                                     'followup_scheduled', 'followup_done')) AS hosp_scheduled,
    count(*) FILTER (WHERE status IN ('hospitalized', 'operated', 'discharged',
                                     'followup_scheduled', 'followup_done')) AS hospitalized,
    count(*) FILTER (WHERE status IN ('operated', 'discharged',
                                     'followup_scheduled', 'followup_done')) AS operated,
    count(*) FILTER (WHERE status = 'followup_done')                        AS followup_done
FROM route;

-- Отчёт о потерях
CREATE VIEW v_route_losses AS
SELECT
    id,
    patient_id,
    status,
    created_at,
    closed_at,
    close_reason,
    EXTRACT(DAY FROM (closed_at - created_at))::INTEGER AS days_open
FROM route
WHERE close_reason IN ('not_realized', 'patient_refused')
   OR status = 'route_not_realized';

-- Просрочки SLA по триггерам
CREATE VIEW v_route_sla_violations AS
SELECT
    r.id            AS route_id,
    r.patient_id,
    r.status,
    t.display_name  AS trigger_name,
    t.target_sla_days,
    r.created_at,
    r.target_date,
    CURRENT_DATE - r.target_date AS days_overdue
FROM route r
JOIN trigger_match tm ON tm.id = r.trigger_match_id
JOIN trigger_def t ON t.id = tm.trigger_def_id
WHERE r.closed_at IS NULL
  AND r.target_date IS NOT NULL
  AND CURRENT_DATE > r.target_date;

COMMENT ON VIEW v_route_funnel IS 'Воронка хирургической конверсии диагностического потока (10 переходов из кейса)';
COMMENT ON VIEW v_route_losses IS 'Маршруты, потерянные пациентом или закрытые пациентом';
COMMENT ON VIEW v_route_sla_violations IS 'Маршруты с просрочкой целевого срока записи к специалисту';
-- FINAL schema (FT_MODE=final) = fault-tolerant schema + tighter domain CHECKs + security/audit schema.
-- Fault-tolerant schema: integrity is enforced BY THE DATABASE, so that retries,
-- duplicates and replays can never create inconsistent state.
-- One PostgreSQL schema per service = explicit data-ownership boundary
-- (single physical database for simplicity; see docs/architecture.md, "Database").

CREATE SCHEMA IF NOT EXISTS student;
CREATE SCHEMA IF NOT EXISTS payment;
CREATE SCHEMA IF NOT EXISTS academic;

CREATE TABLE IF NOT EXISTS student.students (
    student_id  TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,              -- duplicate registration protection
    program     TEXT NOT NULL DEFAULT 'General',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payment.payments (
    payment_id        TEXT PRIMARY KEY,
    idempotency_key   TEXT NOT NULL UNIQUE,         -- duplicate-request detection
    student_id        TEXT NOT NULL,
    invoice_id        TEXT NOT NULL,
    amount            NUMERIC(12,2) NOT NULL CHECK (amount > 0 AND amount <= 50000000),
    currency          CHAR(3) NOT NULL DEFAULT 'KZT' CHECK (currency ~ '^[A-Z]{3}$'),
    status            TEXT NOT NULL CHECK (status IN ('PENDING','COMPLETED','ROLLED_BACK')),
    student_verified  BOOLEAN NOT NULL DEFAULT TRUE, -- FALSE when accepted in degraded mode
    lease_expires_at  TIMESTAMPTZ,                  -- owner lease of an in-flight attempt
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);
-- an invoice can be COMPLETED at most once, even under different idempotency keys
CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_invoice_completed
    ON payment.payments (invoice_id) WHERE status IN ('PENDING','COMPLETED');
CREATE INDEX IF NOT EXISTS ix_payments_status ON payment.payments (status, lease_expires_at);

CREATE TABLE IF NOT EXISTS payment.accounts (
    student_id  TEXT PRIMARY KEY,
    total_paid  NUMERIC(14,2) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS academic.records (
    record_id    BIGSERIAL PRIMARY KEY,
    student_id   TEXT NOT NULL,
    course_code  TEXT NOT NULL,
    term         TEXT NOT NULL,
    grade        NUMERIC(5,2) NOT NULL CHECK (grade BETWEEN 0 AND 100),
    batch_id     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, course_code, term)          -- no duplicate academic records
);
CREATE INDEX IF NOT EXISTS ix_records_student ON academic.records (student_id);

-- ---------------------------------------------------------------------------
-- Security / intrusion tolerance (C6): append-only, hash-chained audit trail.
-- One chain per service instance; the head row is locked FOR UPDATE by writers,
-- so a chain can never fork, and audit rows commit atomically with the business
-- write they describe.
CREATE SCHEMA IF NOT EXISTS security;

CREATE TABLE IF NOT EXISTS security.audit_heads (
    chain_id  TEXT PRIMARY KEY,
    seq       BIGINT NOT NULL,
    head      CHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS security.audit_events (
    chain_id     TEXT NOT NULL,
    seq          BIGINT NOT NULL,
    epoch_ms     BIGINT NOT NULL,
    service      TEXT NOT NULL,
    request_id   TEXT,
    actor        TEXT,
    role         TEXT,
    action       TEXT NOT NULL,
    resource     TEXT,
    outcome      TEXT NOT NULL CHECK (outcome IN ('ALLOW','DENY')),
    status_code  INT NOT NULL,
    detail       TEXT,
    prev_hash    CHAR(64) NOT NULL,
    event_hash   CHAR(64) NOT NULL,
    PRIMARY KEY (chain_id, seq)
);

-- append-only: application role may INSERT but never UPDATE/DELETE audit rows
CREATE OR REPLACE FUNCTION security.audit_immutable() RETURNS trigger AS $$
BEGIN
  IF current_setting('uis.allow_audit_tamper', true) = 'on' THEN   -- only for the tamper-detection experiment
    RETURN COALESCE(NEW, OLD);
  END IF;
  RAISE EXCEPTION 'security.audit_events is append-only';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_audit_immutable ON security.audit_events;
CREATE TRIGGER trg_audit_immutable BEFORE UPDATE OR DELETE ON security.audit_events
    FOR EACH ROW EXECUTE FUNCTION security.audit_immutable();

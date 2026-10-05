-- BASELINE schema: functionally identical tables, but WITHOUT the integrity
-- constraints that make retries safe (no unique idempotency key, no unique
-- academic record, no unique email, no payment status machine / lease).
-- This is intentional: it models a naive first implementation.

CREATE SCHEMA IF NOT EXISTS student;
CREATE SCHEMA IF NOT EXISTS payment;
CREATE SCHEMA IF NOT EXISTS academic;

CREATE TABLE IF NOT EXISTS student.students (
    student_id  TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    email       TEXT NOT NULL,
    program     TEXT NOT NULL DEFAULT 'General',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payment.payments (
    payment_id        TEXT PRIMARY KEY,
    idempotency_key   TEXT,                          -- stored but never checked
    student_id        TEXT NOT NULL,
    invoice_id        TEXT NOT NULL,
    amount            NUMERIC(12,2) NOT NULL,
    currency          CHAR(3) NOT NULL DEFAULT 'KZT',
    status            TEXT NOT NULL,
    student_verified  BOOLEAN NOT NULL DEFAULT TRUE,
    lease_expires_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS payment.accounts (
    student_id  TEXT PRIMARY KEY,
    total_paid  NUMERIC(14,2) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS academic.records (
    record_id    BIGSERIAL PRIMARY KEY,
    student_id   TEXT NOT NULL,
    course_code  TEXT NOT NULL,
    term         TEXT NOT NULL,
    grade        NUMERIC(5,2) NOT NULL,
    batch_id     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

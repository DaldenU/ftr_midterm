"""Payment Service - tuition payments with idempotent, recoverable processing.

FT mode payment life-cycle (two durable checkpoints + atomic completion):

    request(Idempotency-Key)
       |  replay?  -> return stored result (duplicate-request detection)
       v
    CHECKPOINT 1: INSERT payment(status=PENDING, lease) ... ON CONFLICT(key) DO NOTHING
       |           (a crash after this point leaves a recoverable PENDING row)
       v
    external processor call (simulated)
       v
    CHECKPOINT 2 (one DB transaction): PENDING -> COMPLETED  +  ledger += amount
       ^
       |  retry with same key after lease expiry => RESUME (forward recovery)
       |  nobody retries within `payment_rollback_after_s` => reconciler ROLLS BACK

BASELINE mode: two independent autocommit statements (payment row, then ledger),
no idempotency check - an interruption between them corrupts the books and a
client retry creates a duplicate payment.
"""
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.environ.get("FTLIB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared")))

import psycopg
from fastapi import Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ftlib.app import create_app
from ftlib.client import ServiceClient, ServiceUnavailable
from ftlib.logging import current_state, log_event

app, ctx = create_app("payment-service", "Payment Service")
db, s = ctx.db, ctx.settings
student_client = ServiceClient("student-service", s.student_urls, s, ctx.metrics, ctx.log) if s.student_urls else None
if student_client:
    ctx.clients["student-service"] = student_client
ctx.notes.update({"rollbacks": 0, "resumed": 0, "verified_late": 0, "compensated": 0})


class PaymentIn(BaseModel):
    student_id: str
    invoice_id: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    currency: str = Field("KZT", min_length=3, max_length=3)


_LEDGER_UPSERT = ("INSERT INTO payment.accounts(student_id, total_paid) VALUES (%s,%s) "
                  "ON CONFLICT (student_id) DO UPDATE SET total_paid = payment.accounts.total_paid + EXCLUDED.total_paid")


def _processor():
    """Simulated call to an external payment processor."""
    time.sleep(s.processor_latency_ms / 1000.0)


def _verify_student(student_id: str, rid: str) -> bool:
    """True = student verified. False = accepted unverified (degraded mode, reconciled later)."""
    if student_client is None:
        return True
    if ctx.overloaded:
        ctx.mark_degraded("overload-unverified")
        return False
    try:
        r = student_client.get(f"/students/{student_id}", request_id=rid)
    except ServiceUnavailable:
        ctx.mark_degraded("student-unverified")
        return False
    if r.status_code == 404:
        raise HTTPException(404, "unknown student")
    return True


# ----------------------------------------------------------------------------- create
@app.post("/payments", status_code=201, tags=["payments"], summary="Pay tuition (idempotent)")
def create_payment(body: PaymentIn, request: Request, idempotency_key: str | None = Header(None)):
    fault = request.headers.get("x-fault")
    rid = current_state().request_id
    return _create_ft(body, idempotency_key, fault, rid) if s.is_ft else _create_baseline(body, idempotency_key, fault, rid)


def _create_baseline(body: PaymentIn, key, fault, rid):
    if student_client is not None:
        r = student_client.get(f"/students/{body.student_id}", request_id=rid)
        if r.status_code == 404:
            raise HTTPException(404, "unknown student")
        r.raise_for_status()
    pid = "pay_" + uuid.uuid4().hex[:16]

    def op(conn):  # autocommit: every statement commits on its own
        row = conn.execute(
            "INSERT INTO payment.payments(payment_id, idempotency_key, student_id, invoice_id, amount, currency, status, completed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'COMPLETED', now()) RETURNING *",
            (pid, key, body.student_id, body.invoice_id, body.amount, body.currency)).fetchone()
        ctx.maybe_fault(fault, "after_pending")        # <- interruption window (books already say COMPLETED)
        _processor()
        conn.execute(_LEDGER_UPSERT, (body.student_id, body.amount))
        return row

    return JSONResponse(jsonable_encoder(db.run(op, autocommit=True)), status_code=201)


def _create_ft(body: PaymentIn, key, fault, rid):
    if not key:
        raise HTTPException(400, "Idempotency-Key header is required")

    # (1) duplicate-request detection - fast path
    row = db.run(lambda c: c.execute("SELECT * FROM payment.payments WHERE idempotency_key=%s", (key,)).fetchone())
    if row:
        return _existing(row, body, fault)

    verified = _verify_student(body.student_id, rid)

    # (2) CHECKPOINT 1 - durable PENDING record with an ownership lease
    pid = "pay_" + uuid.uuid4().hex[:16]

    def checkpoint(conn):
        r = conn.execute(
            "INSERT INTO payment.payments(payment_id, idempotency_key, student_id, invoice_id, amount, currency, status, "
            "student_verified, lease_expires_at) VALUES (%s,%s,%s,%s,%s,%s,'PENDING',%s, now() + make_interval(secs => %s)) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
            (pid, key, body.student_id, body.invoice_id, body.amount, body.currency, verified, s.payment_lease_s)).fetchone()
        if r:
            return r, False
        return conn.execute("SELECT * FROM payment.payments WHERE idempotency_key=%s", (key,)).fetchone(), True

    try:
        row, existed = db.run(checkpoint)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "invoice already has a pending or completed payment")
    if existed:
        return _existing(row, body, fault)

    ctx.maybe_fault(fault, "after_pending")           # injected interruption: PENDING row stays behind
    _processor()
    done = _complete(row["payment_id"], body.student_id, body.amount)
    return JSONResponse(jsonable_encoder(done), status_code=201)


def _complete(pid: str, student_id: str, amount: float) -> dict:
    """CHECKPOINT 2 - status change and ledger update commit atomically (or not at all)."""
    def op(conn):
        row = conn.execute(
            "UPDATE payment.payments SET status='COMPLETED', completed_at=now(), updated_at=now(), lease_expires_at=NULL "
            "WHERE payment_id=%s AND status='PENDING' RETURNING *", (pid,)).fetchone()
        if row is None:                                   # somebody else completed / rolled it back
            return conn.execute("SELECT * FROM payment.payments WHERE payment_id=%s", (pid,)).fetchone()
        conn.execute(_LEDGER_UPSERT, (student_id, amount))
        return row

    row = db.run(op)
    if row["status"] != "COMPLETED":
        raise HTTPException(409, "payment was rolled back; retry with the same Idempotency-Key")
    return row


def _existing(row: dict, body: PaymentIn, fault):
    """A payment with this Idempotency-Key already exists: replay, resume or reject."""
    if (row["student_id"], row["invoice_id"], float(row["amount"])) != (body.student_id, body.invoice_id, body.amount):
        raise HTTPException(422, "Idempotency-Key was already used with a different request body")
    st = current_state()
    if row["status"] == "COMPLETED":
        st.replay = True
        ctx.metrics.replays.labels(s.service_name, s.instance_id).inc()
        return JSONResponse(jsonable_encoder(row), status_code=200)

    # PENDING (crashed / still running) or ROLLED_BACK: try to take over the attempt atomically.
    def takeover(conn):
        return conn.execute(
            "UPDATE payment.payments SET status='PENDING', lease_expires_at = now() + make_interval(secs => %s), updated_at=now() "
            "WHERE payment_id=%s AND ((status='PENDING' AND lease_expires_at < now()) OR status='ROLLED_BACK') RETURNING *",
            (s.payment_lease_s, row["payment_id"])).fetchone()

    try:
        taken = db.run(takeover)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "invoice was paid by another request meanwhile")
    if taken is None:
        raise HTTPException(409, detail="payment is still being processed", headers={"Retry-After": "1"})
    # forward recovery: finish the interrupted payment from its checkpoint
    ctx.metrics.recovery_events.labels(s.service_name, s.instance_id, "payment_resumed").inc()
    ctx.notes["resumed"] += 1
    log_event(ctx.log, "payment_resumed", operation="recovery", payment_id=taken["payment_id"])
    _processor()
    done = _complete(taken["payment_id"], taken["student_id"], float(taken["amount"]))
    st.extra["recovered"] = True
    return JSONResponse(jsonable_encoder({**done, "recovered": True}), status_code=200)


# ----------------------------------------------------------------------------- read
@app.get("/payments/{payment_id}", tags=["payments"], summary="Payment status")
def get_payment(payment_id: str):
    def op(conn):
        return conn.execute("SELECT * FROM payment.payments WHERE payment_id=%s", (payment_id,)).fetchone()
    row = db.run(op, deadline=2.0) if s.is_ft else db.run(op)
    if not row:
        raise HTTPException(404, "payment not found")
    return jsonable_encoder(row)


@app.get("/accounts/{student_id}", tags=["payments"], summary="Total paid by a student (ledger)")
def get_account(student_id: str):
    def op(conn):
        return conn.execute("SELECT total_paid FROM payment.accounts WHERE student_id=%s", (student_id,)).fetchone()
    row = db.run(op, deadline=2.0) if s.is_ft else db.run(op)
    return {"student_id": student_id, "total_paid": float(row["total_paid"]) if row else 0.0}


# ----------------------------------------------------------------------------- recovery (FT)
def reconcile_once() -> dict:
    """One pass of the background recovery job. Returns counters (also used by tests)."""
    out = {"rolled_back": 0, "verified": 0, "compensated": 0}

    # rollback: PENDING payments nobody has retried for `payment_rollback_after_s`
    def rollback(conn):
        return conn.execute(
            "UPDATE payment.payments SET status='ROLLED_BACK', updated_at=now(), lease_expires_at=NULL "
            "WHERE status='PENDING' AND lease_expires_at < now() - make_interval(secs => %s) RETURNING payment_id",
            (s.payment_rollback_after_s,)).fetchall()
    rows = db.run(rollback, deadline=1.0)
    if rows:
        out["rolled_back"] = len(rows)
        ctx.notes["rollbacks"] += len(rows)
        ctx.metrics.recovery_events.labels(s.service_name, s.instance_id, "payment_rollback").inc(len(rows))
        log_event(ctx.log, "payments_rolled_back", operation="recovery", count=len(rows), ids=[r["payment_id"] for r in rows][:10])

    # late verification of payments accepted while the Student Service was unavailable
    if student_client is not None:
        pending = db.run(lambda c: c.execute(
            "SELECT payment_id, student_id, amount, status FROM payment.payments WHERE student_verified=false "
            "ORDER BY created_at LIMIT 20").fetchall(), deadline=1.0)
        for p in pending:
            try:
                r = student_client.get(f"/students/{p['student_id']}")
            except ServiceUnavailable:
                break
            if r.status_code == 404:
                def compensate(conn, p=p):
                    row = conn.execute("UPDATE payment.payments SET status='ROLLED_BACK', student_verified=true, updated_at=now() "
                                       "WHERE payment_id=%s AND status IN ('PENDING','COMPLETED') RETURNING status", (p["payment_id"],)).fetchone()
                    if row and p["status"] == "COMPLETED":     # compensating transaction on the ledger
                        conn.execute("UPDATE payment.accounts SET total_paid = total_paid - %s WHERE student_id=%s",
                                     (p["amount"], p["student_id"]))
                db.run(compensate, deadline=1.0)
                out["compensated"] += 1
                ctx.notes["compensated"] += 1
                ctx.metrics.recovery_events.labels(s.service_name, s.instance_id, "payment_compensated").inc()
            else:
                db.run(lambda c, p=p: c.execute("UPDATE payment.payments SET student_verified=true WHERE payment_id=%s",
                                                (p["payment_id"],)), deadline=1.0)
                out["verified"] += 1
                ctx.notes["verified_late"] += 1
                ctx.metrics.recovery_events.labels(s.service_name, s.instance_id, "payment_verified_late").inc()
    return out


def _reconciler_loop():
    while True:
        time.sleep(1.0)
        try:
            reconcile_once()
        except Exception as exc:  # noqa: BLE001 - the recovery job must never die
            log_event(ctx.log, "reconciler_error", level=30, operation="recovery", error=repr(exc)[:200])


if s.is_ft:
    @app.on_event("startup")
    def _start_reconciler():
        threading.Thread(target=_reconciler_loop, name="reconciler", daemon=True).start()

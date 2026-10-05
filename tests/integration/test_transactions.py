"""Interrupted-transaction behaviour: checkpoint, resume (forward recovery), rollback, atomic batches."""
import time

import pytest

from tests.conftest import db_query, uid, wait_until

pytestmark = pytest.mark.integration


def pay(client, key, inv, fault=None, student="S0006"):
    h = {"Idempotency-Key": key}
    if fault:
        h["X-Fault"] = fault
    return client.post("/payments", json={"student_id": student, "invoice_id": inv, "amount": 100.0}, headers=h)


def test_ft_interrupted_payment_leaves_recoverable_pending_checkpoint(ft_client, ft_stack):
    key, inv = uid("k"), uid("INV")
    before = ft_client.get("/accounts/S0006").json()["total_paid"]
    r = pay(ft_client, key, inv, fault="interrupt:after_pending")
    assert r.status_code == 500
    row = db_query(ft_stack, "SELECT status FROM payment.payments WHERE idempotency_key=%s", key)[0]
    assert row[0] == "PENDING"                                                   # checkpoint 1 survived the interruption
    assert ft_client.get("/accounts/S0006").json()["total_paid"] == before       # ledger untouched: no partial effect
    # retry straight away: lease still valid -> told to wait, nothing is duplicated
    assert pay(ft_client, key, inv).status_code in (409, 200)
    # retry after the lease expired: payment is resumed from its checkpoint
    r2 = wait_until(lambda: (lambda x: x if x.status_code == 200 else None)(pay(ft_client, key, inv)), timeout=8, interval=0.4)
    assert r2 is not None and r2.json()["status"] == "COMPLETED"
    assert ft_client.get("/accounts/S0006").json()["total_paid"] == before + 100.0
    assert db_query(ft_stack, "SELECT count(*) FROM payment.payments WHERE invoice_id=%s", inv)[0][0] == 1


def test_ft_abandoned_payment_is_rolled_back_by_reconciler(ft_client, ft_stack):
    key, inv = uid("k"), uid("INV")
    before = ft_client.get("/accounts/S0006").json()["total_paid"]
    assert pay(ft_client, key, inv, fault="interrupt:after_pending").status_code == 500
    status = wait_until(lambda: (lambda s: s if s == "ROLLED_BACK" else None)(
        db_query(ft_stack, "SELECT status FROM payment.payments WHERE idempotency_key=%s", key)[0][0]), timeout=15, interval=0.5)
    assert status == "ROLLED_BACK"
    assert ft_client.get("/accounts/S0006").json()["total_paid"] == before
    # the client can still come back later with the same key: the rolled-back attempt is re-opened
    r = pay(ft_client, key, inv)
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"


def test_ft_hard_crash_mid_payment_is_recovered_after_restart(ft_client, ft_stack):
    """The whole process dies (os._exit) after checkpoint 1; supervisor restarts it; the retry completes the payment once."""
    key, inv = uid("k"), uid("INV")
    r = pay(ft_client, key, inv, fault="crash:after_pending")                    # gateway fails over / reports error
    assert r.status_code in (200, 201, 409, 500, 502, 503, 504)   # 409 = replica 2 sees replica 1's live lease
    assert wait_until(lambda: all(i.alive() for n, i in ft_stack.instances.items()), timeout=20)
    r2 = wait_until(lambda: (lambda x: x if x.status_code == 200 else None)(pay(ft_client, key, inv)), timeout=12, interval=0.5) \
        or pay(ft_client, key, inv)
    assert r2.json()["status"] == "COMPLETED"
    assert db_query(ft_stack, "SELECT count(*) FROM payment.payments WHERE invoice_id=%s AND status='COMPLETED'", inv)[0][0] == 1


def test_ft_interrupted_batch_rolls_back_atomically_and_retry_inserts_once(ft_client, ft_stack):
    bid = uid("BATCH")
    recs = [{"student_id": "S0007", "course_code": f"{bid}-{i}", "term": "2026F", "grade": 60 + i} for i in range(6)]
    r = ft_client.post("/grades/batch", json={"batch_id": bid, "records": recs}, headers={"X-Fault": "interrupt:mid_batch"})
    assert r.status_code == 500
    assert db_query(ft_stack, "SELECT count(*) FROM academic.records WHERE batch_id=%s", bid)[0][0] == 0   # all-or-nothing
    assert ft_client.post("/grades/batch", json={"batch_id": bid, "records": recs}).status_code == 201
    again = ft_client.post("/grades/batch", json={"batch_id": bid, "records": recs})
    assert again.json()["inserted"] == 0                                                                   # replay inserts nothing
    assert db_query(ft_stack, "SELECT count(*) FROM academic.records WHERE batch_id=%s", bid)[0][0] == 6


def test_baseline_interruption_corrupts_ledger_and_leaves_partial_batch(baseline_stack):
    """Documents the baseline weakness (no atomicity): payment row exists but ledger was never updated."""
    import httpx
    with httpx.Client(base_url=baseline_stack.gateway_url, timeout=10) as c:
        key, inv = uid("k"), uid("INV")
        before = c.get("/accounts/S0008").json()["total_paid"]
        assert pay(c, key, inv, fault="interrupt:after_pending", student="S0008").status_code == 500
        assert db_query(baseline_stack, "SELECT status FROM payment.payments WHERE invoice_id=%s", inv)[0][0] == "COMPLETED"
        assert c.get("/accounts/S0008").json()["total_paid"] == before                     # ledger missed the payment
        bid = uid("BB")
        recs = [{"student_id": "S0009", "course_code": f"{bid}-{i}", "term": "2026F", "grade": 60} for i in range(6)]
        assert c.post("/grades/batch", json={"batch_id": bid, "records": recs}, headers={"X-Fault": "interrupt:mid_batch"}).status_code == 500
        n = db_query(baseline_stack, "SELECT count(*) FROM academic.records WHERE batch_id=%s", bid)[0][0]
        assert 0 < n < 6                                                                  # partial batch persisted

"""Idempotency / duplicate-request protection (payments) - the heart of the data-consistency requirement."""
import concurrent.futures as cf

import pytest

from tests.conftest import db_query, uid

pytestmark = pytest.mark.integration


def pay(client, key, inv, student="S0003", amount=100.0, headers=None):
    h = {"Idempotency-Key": key, **(headers or {})}
    return client.post("/payments", json={"student_id": student, "invoice_id": inv, "amount": amount}, headers=h)


def test_ft_same_key_returns_original_transaction(ft_client, ft_stack):
    key, inv = uid("k"), uid("INV")
    before = ft_client.get("/accounts/S0003").json()["total_paid"]
    first = pay(ft_client, key, inv)
    second = pay(ft_client, key, inv)
    third = pay(ft_client, key, inv)
    assert first.status_code == 201
    assert second.status_code == 200 and third.status_code == 200
    assert second.headers["idempotent-replay"] == "true"
    assert first.json()["payment_id"] == second.json()["payment_id"] == third.json()["payment_id"]
    assert ft_client.get("/accounts/S0003").json()["total_paid"] == before + 100.0     # charged exactly once
    assert db_query(ft_stack, "SELECT count(*) FROM payment.payments WHERE idempotency_key=%s", key)[0][0] == 1


def test_ft_same_key_different_body_rejected(ft_client):
    key = uid("k")
    assert pay(ft_client, key, uid("INV"), amount=10).status_code == 201
    assert pay(ft_client, key, uid("INV"), amount=10).status_code == 422


def test_ft_missing_idempotency_key_rejected(ft_client):
    r = ft_client.post("/payments", json={"student_id": "S0003", "invoice_id": uid("I"), "amount": 5})
    assert r.status_code == 400


def test_ft_same_invoice_with_different_key_cannot_be_paid_twice(ft_client):
    inv = uid("INV")
    assert pay(ft_client, uid("k"), inv).status_code == 201
    assert pay(ft_client, uid("k"), inv).status_code == 409


def test_ft_concurrent_duplicates_create_exactly_one_payment(ft_client, ft_stack):
    key, inv = uid("k"), uid("INV")
    import httpx
    with cf.ThreadPoolExecutor(12) as ex:
        def one(_):
            with httpx.Client(base_url=ft_stack.gateway_url, timeout=10) as c:
                return pay(c, key, inv).status_code
        codes = list(ex.map(one, range(12)))
    assert 201 in codes and codes.count(201) == 1               # exactly one creator
    assert set(codes) <= {201, 200, 409}                         # others: replay or "still processing"
    assert db_query(ft_stack, "SELECT count(*) FROM payment.payments WHERE invoice_id=%s AND status='COMPLETED'", inv)[0][0] == 1


def test_ft_duplicate_student_registration_is_idempotent(ft_client):
    sid = uid("DS")
    body = {"student_id": sid, "full_name": "Dup", "email": f"{sid}@uni.test"}
    assert ft_client.post("/students", json=body).status_code == 201
    r = ft_client.post("/students", json=body)
    assert r.status_code == 200 and r.headers["idempotent-replay"] == "true"


def test_ft_duplicate_grade_is_idempotent(ft_client):
    body = {"student_id": "S0004", "course_code": uid("G"), "term": "2026F", "grade": 77}
    assert ft_client.post("/grades", json=body).status_code == 201
    r = ft_client.post("/grades", json=body)
    assert r.status_code == 200 and r.headers["idempotent-replay"] == "true"
    assert ft_client.post("/grades", json={**body, "grade": 50}).status_code == 409   # conflicting duplicate


def test_baseline_is_vulnerable_to_duplicate_payments(baseline_stack):
    """Documents the baseline weakness the fault-tolerant version fixes."""
    import httpx
    with httpx.Client(base_url=baseline_stack.gateway_url, timeout=10) as c:
        key, inv = uid("k"), uid("INV")
        a, b = pay(c, key, inv, student="S0005"), pay(c, key, inv, student="S0005")
        assert a.status_code == b.status_code == 201
        assert a.json()["payment_id"] != b.json()["payment_id"]                    # charged twice!
    assert db_query(baseline_stack, "SELECT count(*) FROM payment.payments WHERE invoice_id=%s", inv)[0][0] == 2

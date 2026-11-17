"""Integration tests of FT_MODE=final against a live stack (gateway + 2x3 services + PostgreSQL)."""
import time
import uuid

import httpx
import psycopg
import pytest

from ftlib.security import verify_chain


def auth(st, user):
    return st.security.auth(user)


def test_happy_path_workflow_with_internal_service_auth(final_stack, final_client):
    c, st = final_client, final_stack
    inv = "INV-" + uuid.uuid4().hex[:8]
    r = c.post("/payments", json={"student_id": "S0003", "invoice_id": inv, "amount": 500},
               headers={"Idempotency-Key": "k" + inv, **auth(st, "S0003")})
    assert r.status_code == 201
    course = "T" + uuid.uuid4().hex[:6]
    assert c.post("/grades", json={"student_id": "S0003", "course_code": course, "term": "2026F", "grade": 88},
                  headers=auth(st, "FAC02")).status_code == 201
    t = c.get("/students/S0003/transcript", headers=auth(st, "S0003"))
    assert t.status_code == 200 and t.json()["student"]["student_id"] == "S0003"   # academic -> student via HMAC
    assert c.get("/accounts/S0003", headers=auth(st, "FIN01")).status_code == 200


def test_login_endpoint_and_bruteforce_throttling(final_stack, final_client):
    st, c = final_stack, final_client
    r = c.post("/auth/token", json={"username": "S0004", "password": st.security.passwords["S0004"]})
    assert r.status_code == 200
    tok = r.json()["access_token"]
    assert c.get("/students/S0004", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    codes = [c.post("/auth/token", json={"username": "S0004", "password": f"wrong{i}"}).status_code for i in range(14)]
    assert 401 in codes and 429 in codes


@pytest.mark.parametrize("headers,code", [({}, 401), ({"Authorization": "Bearer abc.def.ghi"}, 401),
                                          ({"Authorization": "Basic Zm9vOmJhcg=="}, 401)])
def test_unauthenticated_requests_rejected(final_client, headers, code):
    assert final_client.get("/students/S0001", headers=headers).status_code == code


def test_rbac_and_bola(final_stack, final_client):
    st, c = final_stack, final_client
    assert c.get("/students/S0002", headers=auth(st, "S0001")).status_code == 403          # BOLA read
    assert c.get("/accounts/S0002", headers=auth(st, "S0001")).status_code == 403
    assert c.post("/grades", json={"student_id": "S0001", "course_code": "X1", "term": "2026F", "grade": 99},
                  headers=auth(st, "S0001")).status_code == 403                             # vertical escalation
    assert c.post("/payments", json={"student_id": "S0009", "invoice_id": "INV-X", "amount": 1},
                  headers={"Idempotency-Key": "bola1", **auth(st, "S0001")}).status_code == 403
    assert c.get("/students/S0002", headers=auth(st, "FAC01")).status_code == 200            # staff may read


def test_services_reenforce_auth_when_gateway_is_bypassed(final_stack):
    st = final_stack
    be = st.instances["payment-1"].url
    assert httpx.get(be + "/accounts/S0002").status_code == 401
    forged = {"X-Internal-Auth": f"v1;payment-service;{time.time():.3f};{'0' * 64}"}
    assert httpx.get(st.instances["student-1"].url + "/students/S0002", headers=forged).status_code == 401
    # a valid user token presented directly to a backend is still subject to RBAC/BOLA there
    assert httpx.get(be + "/accounts/S0002", headers=auth(st, "S0001")).status_code == 403
    assert httpx.post(st.instances["student-1"].url + "/admin/fault", json={"fail_all": True}).status_code == 401


def test_input_validation_size_cap_and_idempotency_binding(final_stack, final_client):
    st, c = final_stack, final_client
    assert c.get("/students/S0001' OR '1'='1", headers=auth(st, "FAC01")).status_code == 422
    assert c.post("/students", json={"full_name": "<script>x</script>", "email": "a@b.cd"},
                  headers=auth(st, "REG01")).status_code == 422
    assert c.post("/payments", json={"student_id": "S0001", "invoice_id": "INV-N", "amount": -5},
                  headers={"Idempotency-Key": "neg1", **auth(st, "S0001")}).status_code == 422
    big = b'{"student_id":"S0001","course_code":"BIG","term":"2026F","grade":1,"p":"' + b"A" * 40000 + b'"}'
    assert c.post("/grades", content=big, headers={"content-type": "application/json", **auth(st, "FAC01")}).status_code == 413
    key = "tamper-" + uuid.uuid4().hex[:6]
    body = {"student_id": "S0001", "invoice_id": "INV-" + key, "amount": 100}
    assert c.post("/payments", json=body, headers={"Idempotency-Key": key, **auth(st, "S0001")}).status_code == 201
    assert c.post("/payments", json={**body, "amount": 9999}, headers={"Idempotency-Key": key, **auth(st, "S0001")}).status_code == 409


def test_rate_limit_isolates_flooding_principal(final_stack, final_client):
    st, c = final_stack, final_client
    codes = [c.get("/students/S0040", headers=auth(st, "S0040")).status_code for _ in range(160)]
    assert codes.count(429) > 0
    assert c.get("/students/S0041", headers=auth(st, "S0041")).status_code == 200          # others unaffected


def test_audit_chain_records_denials_and_writes_and_is_intact(final_stack, final_client):
    st, c = final_stack, final_client
    c.get("/students/S0001' OR 1=1", headers=auth(st, "FAC01"))                              # produce a DENY
    with psycopg.connect(st.database_url, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute("SELECT * FROM security.audit_events").fetchall()
        assert any(r["outcome"] == "DENY" for r in rows) and any(r["outcome"] == "ALLOW" for r in rows)
        assert verify_chain(rows)["broken_links"] == 0
        with pytest.raises(psycopg.Error):                                                  # append-only trigger
            conn.execute("DELETE FROM security.audit_events")

"""API / integration tests - executed against BOTH the baseline and the fault-tolerant deployment:
the services must work normally in either mode."""
import pytest

from tests.conftest import uid

pytestmark = pytest.mark.integration


def test_gateway_and_service_health(client, stack):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").status_code == 200
    for svc in ("student", "payment", "academic"):
        for url in stack.service_urls(svc):
            import httpx
            assert httpx.get(url + "/health").json()["status"] == "ok"
            assert httpx.get(url + "/ready").json()["ready"] is True


def test_openapi_documentation_available(stack):
    import httpx
    for svc in ("student", "payment", "academic"):
        spec = httpx.get(stack.service_urls(svc)[0] + "/openapi.json").json()
        assert spec["paths"]


def test_student_registration_and_lookup(client):
    sid = uid("T")
    r = client.post("/students", json={"student_id": sid, "full_name": "Ada L", "email": f"{sid}@uni.test", "program": "CS"})
    assert r.status_code == 201 and r.json()["student_id"] == sid
    g = client.get(f"/students/{sid}")
    assert g.status_code == 200 and g.json()["email"] == f"{sid}@uni.test"
    assert client.get("/students/NOPE-404").status_code == 404


def test_student_validation_error(client):
    assert client.post("/students", json={"full_name": "x", "email": "not-an-email"}).status_code == 422


def test_payment_flow_and_status(client):
    inv = uid("INV")
    r = client.post("/payments", json={"student_id": "S0001", "invoice_id": inv, "amount": 250.5},
                    headers={"Idempotency-Key": uid("k")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "COMPLETED" and float(body["amount"]) == 250.5
    g = client.get(f"/payments/{body['payment_id']}")
    assert g.status_code == 200 and g.json()["payment_id"] == body["payment_id"]
    assert client.get("/payments/pay_missing").status_code == 404
    assert client.get("/accounts/S0001").json()["total_paid"] >= 250.5


def test_payment_for_unknown_student_rejected(client):
    r = client.post("/payments", json={"student_id": "NOBODY", "invoice_id": uid("INV"), "amount": 10},
                    headers={"Idempotency-Key": uid("k")})
    assert r.status_code == 404


def test_payment_validation(client):
    r = client.post("/payments", json={"student_id": "S0001", "invoice_id": uid("I"), "amount": -5}, headers={"Idempotency-Key": uid("k")})
    assert r.status_code == 422


def test_grades_and_transcript(client):
    sid = uid("TR")
    client.post("/students", json={"student_id": sid, "full_name": "Grace H", "email": f"{sid}@uni.test"})
    for course, grade in (("CS101", 90), ("MA201", 80)):
        assert client.post("/grades", json={"student_id": sid, "course_code": course, "term": "2026F", "grade": grade}).status_code == 201
    t = client.get(f"/students/{sid}/transcript").json()
    assert t["student"]["full_name"] == "Grace H" and t["gpa"] == 85.0 and len(t["records"]) == 2 and t["degraded"] is None


def test_grade_batch(client):
    sid = "S0002"
    code = uid("B")
    recs = [{"student_id": sid, "course_code": f"{code}{i}", "term": "2026F", "grade": 70 + i} for i in range(4)]
    r = client.post("/grades/batch", json={"records": recs})
    assert r.status_code == 201 and r.json()["inserted"] == 4


def test_request_id_is_propagated_and_returned(client):
    r = client.get("/students/S0001", headers={"X-Request-ID": "trace-me-123"})
    assert r.headers["x-request-id"] == "trace-me-123"


def test_metrics_endpoint(stack):
    import httpx
    txt = httpx.get(stack.service_urls("student")[0] + "/metrics").text
    assert "http_requests_total" in txt and "service_up" in txt

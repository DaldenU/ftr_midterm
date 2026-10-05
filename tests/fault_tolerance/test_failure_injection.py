"""Fault-tolerance / failure-injection / recovery tests.

Each test injects a real fault (kill -9 a process, stop PostgreSQL, add latency) into the running
deployment and asserts the fault is DETECTED, the impact is CONTAINED, the system RECOVERS and the
data stays CONSISTENT."""
import time

import httpx
import pytest

from experiments.analysis.consistency import check
from tests.conftest import db_query, uid, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.fault]


def all_alive(stack):
    return all(i.alive() for i in stack.instances.values())


def gw_ready(stack):
    try:
        return httpx.get(stack.gateway_url + "/ready", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def hammer(client, seconds, path="/students/S0001"):
    """Issue sequential GETs for ``seconds``; return (ok, failed)."""
    ok = failed = 0
    end = time.time() + seconds
    while time.time() < end:
        try:
            r = client.get(path)
            ok += r.status_code == 200
            failed += r.status_code != 200
        except httpx.HTTPError:
            failed += 1
        time.sleep(0.02)
    return ok, failed


# ------------------------------------------------------------------ application crash / replicas
def test_application_crash_is_masked_by_replicas_and_self_healed(ft_stack, ft_client):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)
    t0 = time.time()
    ft_stack.kill_instance("student-1")
    ok, failed = hammer(ft_client, 4.0)                       # traffic during the failure
    assert failed == 0 and ok > 50                           # replica masked the failure (fault masking)
    ev = ft_stack.gateway_events()["events"]
    assert any(e["state"] == "down" and e["backend"] == ft_stack.instances["student-1"].url and e["ts"] >= t0 for e in ev)   # DETECTED
    assert wait_until(lambda: ft_stack.instances["student-1"].alive() and gw_ready(ft_stack), 20)                             # RECOVERED
    assert wait_until(lambda: any(e["state"] == "up" and e["backend"] == ft_stack.instances["student-1"].url and e["ts"] > t0
                                  for e in ft_stack.gateway_events()["events"]), 10)                                          # re-admitted by health check


def test_baseline_application_crash_causes_outage(baseline_stack):
    with httpx.Client(base_url=baseline_stack.gateway_url, timeout=5) as c:
        assert c.get("/accounts/S0001").status_code == 200
        baseline_stack.kill_instance("payment-1")
        time.sleep(0.3)
        assert c.get("/accounts/S0001").status_code in (502, 504)          # no replica, nobody restarts it
        assert not baseline_stack.instances["payment-1"].alive()
        baseline_stack.restart_instance("payment-1")                       # operator action
        assert c.get("/accounts/S0001").status_code == 200


def test_node_failure_survived_without_total_interruption(ft_stack, ft_client):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)
    t, names = ft_stack.kill_node("A")
    assert set(names) == {"student-1", "payment-1", "academic-1"}
    ok = failed = 0
    end = time.time() + 3
    while time.time() < end:                                              # all three service types, during node loss
        for path in ("/students/S0001", "/accounts/S0001", "/students/S0001/transcript"):
            try:
                r = ft_client.get(path)
                ok += r.status_code == 200
                failed += r.status_code != 200
            except httpx.HTTPError:
                failed += 1
        time.sleep(0.05)
    assert failed == 0 and ok > 30
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 25)         # node recovered by orchestrator


def test_health_check_detects_failed_replica_and_load_balancer_avoids_it(ft_stack, ft_client):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)
    ft_stack.kill_instance("academic-2")
    assert wait_until(lambda: not [b for b in ft_stack.gateway_events()["backends"]["academic"]
                                   if b["url"] == ft_stack.instances["academic-2"].url and b["healthy"]], 5)
    served = {ft_client.get("/students/S0001/transcript").headers["x-served-by"] for _ in range(10)}
    assert served == {ft_stack.instances["academic-1"].url}               # only the healthy replica receives traffic
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)


def test_load_balancer_distributes_traffic_across_replicas(ft_stack, ft_client):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)
    served = [ft_client.get("/students/S0001").headers["x-served-by"] for _ in range(20)]
    assert len(set(served)) == 2 and abs(served.count(served[0]) - 10) <= 2   # round robin


# ------------------------------------------------------------------ timeouts / retry / breaker / degradation
def test_slow_dependency_trips_circuit_breaker_and_service_degrades_gracefully(ft_stack, ft_client):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)
    sid = uid("DG")
    ft_client.post("/students", json={"student_id": sid, "full_name": "Deg", "email": f"{sid}@uni.test"})
    ft_stack.set_fault("student", latency_ms=3000)                        # student-service becomes very slow
    try:
        t = time.time()
        r = ft_client.post("/payments", json={"student_id": sid, "invoice_id": uid("INV"), "amount": 10}, headers={"Idempotency-Key": uid("k")})
        dt = time.time() - t
        assert r.status_code == 201                                      # TIMEOUT (1 s) + RETRY instead of waiting 3 s
        assert r.headers.get("x-degraded")                               # accepted unverified: graceful degradation
        assert int(r.headers.get("x-retry-count", 0)) >= 1                # retries with backoff happened
        assert dt < 2.8                                          # bounded by the 2 s retry budget (+ one 1 s timeout), not by the 3 s dependency
        # hammer until the breaker is OPEN, then calls fail fast
        for _ in range(8):
            ft_client.post("/payments", json={"student_id": sid, "invoice_id": uid("INV"), "amount": 10}, headers={"Idempotency-Key": uid("k")})
        states = [ft_stack.admin_state(n)["breakers"]["student-service"]["state"] for n in ("payment-1", "payment-2")]
        assert "open" in states
        t = time.time()
        r = ft_client.post("/payments", json={"student_id": sid, "invoice_id": uid("INV"), "amount": 10}, headers={"Idempotency-Key": uid("k")})
        assert r.status_code == 201 and time.time() - t < 0.5                # fail-fast path
        tr = ft_client.get(f"/students/{sid}/transcript")
        assert tr.status_code == 200 and tr.json()["degraded"] == "student-service-unavailable" and tr.json()["student"] is None
    finally:
        ft_stack.clear_faults()
    # recovery: breaker closes again and the late verification job confirms the unverified payments
    def closed():
        # a HALF_OPEN breaker is only resolved by a trial call, so send traffic that exercises every replica
        for _ in range(2):
            ft_client.get(f"/students/{sid}/transcript")
            ft_client.post("/payments", json={"student_id": sid, "invoice_id": uid("INV"), "amount": 1}, headers={"Idempotency-Key": uid("k")})
        return all(ft_stack.admin_state(n)["breakers"]["student-service"]["state"] == "closed" for n in ("payment-1", "payment-2", "academic-1", "academic-2")
                   if ft_stack.admin_state(n))
    assert wait_until(closed, 15, 0.5)
    assert wait_until(lambda: db_query(ft_stack, "SELECT count(*) FROM payment.payments WHERE student_id=%s AND NOT student_verified", sid)[0][0] == 0, 20, 0.5)


def test_baseline_has_no_timeout_protection_against_slow_dependency(baseline_stack):
    with httpx.Client(base_url=baseline_stack.gateway_url, timeout=10) as c:
        baseline_stack.set_fault("student", latency_ms=2500)
        try:
            t = time.time()
            r = c.post("/payments", json={"student_id": "S0001", "invoice_id": uid("INV"), "amount": 10}, headers={"Idempotency-Key": uid("k")})
            assert r.status_code == 201 and time.time() - t >= 2.4          # caller blocked for the full dependency latency
        finally:
            baseline_stack.clear_faults()


def test_gateway_retries_gray_failure_on_other_replica(ft_stack, ft_client):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)
    inst = ft_stack.instances["student-1"]
    httpx.post(inst.url + "/admin/fault", json={"fail_all": True})        # replica answers 503 to everything
    try:
        res = [ft_client.get("/students/S0001") for _ in range(10)]
        assert all(r.status_code == 200 for r in res)
        assert any(int(r.headers.get("x-gateway-attempts", 1)) > 1 for r in res)    # masked by failover retry
    finally:
        httpx.delete(inst.url + "/admin/fault")


# ------------------------------------------------------------------ database failure (last: restarts PostgreSQL)
def test_database_failure_detected_degraded_reads_retried_writes_and_recovery(ft_stack, ft_client, pg):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 20)
    ft_client.get("/students/S0001")                                       # warm the last-known-good cache on both replicas
    ft_client.get("/students/S0001")
    pg.stop("immediate")                                                    # crash-stop the database
    try:
        # DETECTION: readiness probes report the dependency failure, gateway marks backends down
        assert wait_until(lambda: httpx.get(ft_stack.instances["payment-1"].url + "/ready").status_code == 503, 5)
        assert wait_until(lambda: httpx.get(ft_stack.gateway_url + "/ready").status_code == 503, 6)
        # GRACEFUL DEGRADATION: reads are served from cache, flagged as degraded
        r = ft_client.get("/students/S0001")
        assert r.status_code == 200 and r.headers.get("x-degraded") == "stale-cache"
        # RETRY WITH BACKOFF: a write issued during the outage succeeds once the DB comes back
        import threading
        result = {}
        key, inv = uid("k"), uid("INV")

        def write():
            result["r"] = ft_client.post("/payments", json={"student_id": "S0001", "invoice_id": inv, "amount": 42},
                                         headers={"Idempotency-Key": key}, timeout=15)
        th = threading.Thread(target=write)
        th.start()
        time.sleep(1.5)
        pg.start()
    finally:
        if not pg.is_up():
            pg.start()
    th.join(15)
    r = result["r"]
    assert r.status_code == 201 and int(r.headers.get("x-retry-count", 0)) >= 1       # recovered request
    # RECOVERY: readiness is back, no data lost, no duplicates
    assert wait_until(lambda: gw_ready(ft_stack), 15)
    assert db_query(ft_stack, "SELECT count(*) FROM payment.payments WHERE invoice_id=%s AND status='COMPLETED'", inv)[0][0] == 1


def test_database_failure_in_baseline_fails_requests(baseline_stack, pg):
    with httpx.Client(base_url=baseline_stack.gateway_url, timeout=10) as c:
        pg.stop("immediate")
        try:
            assert c.get("/students/S0001").status_code == 503
            assert httpx.get(baseline_stack.instances["student-1"].url + "/ready").status_code == 503   # detectable, but nobody acts on it
        finally:
            pg.start()
        assert wait_until(lambda: c.get("/students/S0001").status_code == 200, 10)


# ------------------------------------------------------------------ final consistency
def test_data_remains_consistent_after_all_injected_faults(ft_stack):
    assert wait_until(lambda: all_alive(ft_stack) and gw_ready(ft_stack), 25)
    time.sleep(4)                                                            # let the reconciler roll back abandoned attempts
    res = check(ft_stack.database_url)
    assert res["duplicate_payments"] == 0 and res["ledger_mismatch_students"] == 0 and res["duplicate_academic_records"] == 0, res["violations"]
    assert res["stuck_pending_payments"] == 0, res

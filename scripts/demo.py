#!/usr/bin/env python3
"""Narrated live demonstrations of failure and recovery (local process stack - no Docker needed).

  python scripts/demo.py 1            # Demo 1: kill one application instance, replica keeps serving
  python scripts/demo.py 2            # Demo 2: database outage + slow dependency -> timeout/retry/breaker/recovery
  python scripts/demo.py 3            # Demo 3: duplicate / interrupted payment -> idempotency
  python scripts/demo.py 1 --mode baseline     # same demo on the baseline, for contrast
Add --keep to leave the stack running (gateway on http://127.0.0.1:20000) after the demo.
The same steps with Docker Compose are in docs/demonstration.md.
"""
import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "shared")]

import httpx  # noqa: E402
import psycopg  # noqa: E402

from experiments.harness.pg import PgCluster  # noqa: E402
from experiments.harness.stack import Stack  # noqa: E402
from experiments.workload.generator import Client, Recorder, Workload, summarize  # noqa: E402

WORK = Path("/var/tmp/ftuis-demo")


def say(msg):
    print(f"\n\033[1m>>> {msg}\033[0m", flush=True)


def observe(msg):
    print(f"    \033[36mOBSERVE:\033[0m {msg}", flush=True)


def live_load(stack, seconds, rate=20):
    rec = Recorder()
    wl = Workload(Client(stack.gateway_url, timeout=5.0), rec)
    th = threading.Thread(target=wl.run_open_loop, args=(seconds, rate), daemon=True)
    th.start()
    return rec, th


def ticker(rec, th, label=""):
    t0 = time.time()
    while th.is_alive():
        time.sleep(1)
        s = summarize([r for r in rec.snapshot() if r.t1 >= time.time() - 1])
        print(f"      t={time.time() - t0:4.0f}s ok={s['success']:3d} failed={s['failed']:3d} degraded={s['degraded']:3d} "
              f"recovered={s['recovered']:3d} {label}", flush=True)


def gw_events(stack, since):
    return [f"{e['ts'] - since:+.2f}s {e['service']} {e['backend'].rsplit(':', 1)[1]} -> {e['state']} ({e['why']})"
            for e in stack.gateway_events()["events"] if e["ts"] >= since]


def demo1(stack, pg):
    say("DEMO 1 - kill one application instance (payment-1) under live traffic")
    rec, th = live_load(stack, 14)
    time.sleep(4)
    t = stack.kill_instance("payment-1")
    say("SIGKILL sent to payment-1")
    ticker(rec, th)
    s = summarize(rec.snapshot())
    print("      gateway health events:", *gw_events(stack, t), sep="\n        ")
    print("      supervisor events:", *[f"{e['ts'] - t:+.2f}s {e['type']} {e['name']}" for e in stack.events if e["ts"] >= t], sep="\n        ")
    say(f"RESULT: {s['success']}/{s['total']} requests OK, {s['failed']} failed, {s['recovered']} recovered by failover")
    observe("failed stays ~0 in FT (payment-2 served); in baseline every payment/account request fails until restart")
    observe("metric: gateway_failovers_total / recovery_events_total{type=backend_restored} (http://127.0.0.1:20000/metrics)")


def demo2(stack, pg):
    say("DEMO 2a - database outage (6 s) under live traffic")
    rec, th = live_load(stack, 20)
    time.sleep(4)
    pg.stop("immediate")
    say("PostgreSQL crash-stopped")
    time.sleep(6)
    pg.start()
    say("PostgreSQL restarted")
    ticker(rec, th)
    s = summarize(rec.snapshot())
    say(f"RESULT: failed={s['failed']} recovered(retried)={s['recovered']} degraded(cache)={s['degraded']} of {s['total']}")
    observe("FT: reads served from cache (X-Degraded: stale-cache); writes retried with backoff and succeed after the DB returns")
    observe("log line: grep '\"retry_count\"' in the service logs; metric recovery_events_total{type=db_reconnected}")

    say("DEMO 2b - slow dependency: student-service +3000 ms -> timeout, retry, circuit breaker, degradation")
    c = httpx.Client(base_url=stack.gateway_url, timeout=10)
    stack.set_fault("student", latency_ms=3000)
    for i in range(8):
        t = time.time()
        r = c.post("/payments", json={"student_id": "S0001", "invoice_id": uuid.uuid4().hex, "amount": 5}, headers={"Idempotency-Key": uuid.uuid4().hex})
        states = [(stack.admin_state(n) or {}).get("breakers", {}).get("student-service", {}).get("state") for n in ("payment-1", "payment-2")]
        print(f"      payment #{i + 1}: HTTP {r.status_code} in {time.time() - t:5.2f}s retries={r.headers.get('x-retry-count', 0)} "
              f"degraded={r.headers.get('x-degraded')} breaker={states}", flush=True)
    observe("first calls take ~2 s (1 s timeout + backoff retry), then the breaker OPENS and calls return in milliseconds")
    stack.clear_faults()
    say("fault cleared; waiting for the breaker to probe (HALF_OPEN) and close")
    for _ in range(12):
        c.post("/payments", json={"student_id": "S0001", "invoice_id": uuid.uuid4().hex, "amount": 5}, headers={"Idempotency-Key": uuid.uuid4().hex})
        states = [(stack.admin_state(n) or {}).get("breakers", {}).get("student-service", {}).get("state") for n in ("payment-1", "payment-2")]
        print("      breaker:", states, flush=True)
        if all(s == "closed" for s in states):
            break
        time.sleep(1)
    observe("breaker returns to CLOSED; unverified payments are verified later by the reconciler (recovery_events_total{type=payment_verified_late})")


def demo3(stack, pg):
    say("DEMO 3 - repeated and interrupted payments (idempotency, checkpoint/resume, rollback)")
    c = httpx.Client(base_url=stack.gateway_url, timeout=10)
    dsn = stack.database_url

    def rows(inv):
        with psycopg.connect(dsn, autocommit=True) as db:
            return db.execute("SELECT payment_id,status FROM payment.payments WHERE invoice_id=%s", (inv,)).fetchall()

    inv, key = "DEMO-" + uuid.uuid4().hex[:6], uuid.uuid4().hex
    body = {"student_id": "S0001", "invoice_id": inv, "amount": 1000}
    before = c.get("/accounts/S0001").json()["total_paid"]
    for i in range(3):
        r = c.post("/payments", json=body, headers={"Idempotency-Key": key})
        print(f"      attempt {i + 1}: HTTP {r.status_code} payment_id={r.json().get('payment_id')} replay={r.headers.get('idempotent-replay')}", flush=True)
    after = c.get("/accounts/S0001").json()["total_paid"]
    say(f"3 identical requests -> {len(rows(inv))} payment row(s); ledger moved by {after - before} (expected 1000 in FT, 3000 in baseline)")

    inv2, key2 = "DEMO-" + uuid.uuid4().hex[:6], uuid.uuid4().hex
    body2 = {"student_id": "S0001", "invoice_id": inv2, "amount": 1000}
    say("interrupted payment: the service 'crashes' after writing its first checkpoint")
    r = c.post("/payments", json=body2, headers={"Idempotency-Key": key2, "X-Fault": "interrupt:after_pending"})
    print(f"      attempt 1: HTTP {r.status_code}  DB rows: {rows(inv2)}", flush=True)
    time.sleep(1.8)
    r = c.post("/payments", json=body2, headers={"Idempotency-Key": key2})
    print(f"      retry    : HTTP {r.status_code} status={r.json().get('status')} recovered={r.json().get('recovered')}  DB rows: {rows(inv2)}", flush=True)
    observe("FT: PENDING checkpoint survives, retry RESUMES it -> exactly one COMPLETED row. Baseline: the retry creates a SECOND payment")
    observe("log line: {\"msg\": \"payment_resumed\", \"operation\": \"recovery\"}; metric recovery_events_total{type=payment_resumed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("demo", choices=["1", "2", "3"])
    ap.add_argument("--mode", choices=["ft", "baseline"], default="ft")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    pg = PgCluster(WORK, port=57432, standby_port=57433)
    pg.start()
    pg.reset_db(f"uni_{a.mode}", ROOT / f"database/schema_{a.mode}.sql")
    stack = Stack(a.mode, pg, base_port=20000, workdir=WORK, restart_delay_s=1.0, env={"PAYMENT_LEASE_S": 1.5, "BREAKER_RESET_TIMEOUT_S": 3})
    try:
        say(f"starting {a.mode} system (gateway {stack.gateway_url}) ...")
        stack.start()
        {"1": demo1, "2": demo2, "3": demo3}[a.demo](stack, pg)
        if a.keep:
            input("\nStack running - press Enter to stop ...")
    finally:
        stack.stop()
        pg.stop()


if __name__ == "__main__":
    main()

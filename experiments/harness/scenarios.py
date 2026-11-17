"""Failure scenarios S1-S6 (+S7) with a uniform measurement protocol.

Every scenario produces ONE record (dict) with the fields required by the
assignment: experiment id, date/time, system version, failure injected, initial
state, failure start, detection time, recovery start/end/duration, request
counts (total / successful / failed / recovered / duplicate), consistency
result and observations. Everything is measured, nothing is estimated.

Time bookkeeping
  failure_start         instant the fault is injected
  detection_*           seconds after failure_start at which a signal appeared:
                        monitor  = external /ready poller saw an unhealthy instance
                        internal = the system itself reacted (gateway ejection, circuit breaker)
                        client   = first client request that returned an error
                        detection_time_s = earliest available signal
  recovery_start        instant the repair action began (supervisor/operator restart, DB start, fault cleared)
  recovery_end          instant full service was restored (definition per scenario)
  recovery_duration_s   recovery_end - failure_start   ("time to recover", the MTTR sample)
  repair_action_s       recovery_end - recovery_start
"""
from __future__ import annotations

import datetime as dt
import json
import random
import threading
import time
from pathlib import Path

import psycopg

from ..analysis.consistency import check
from ..workload.generator import Client, Rec, Recorder, Workload, pct, summarize
from .pg import PgCluster, ROOT
from .probe import Prober, ReadyMonitor
from .prom import AlertWatcher, PromRunner
from .stack import Stack

SYSTEM_VERSION = {"baseline": "baseline-1.0", "ft": "fault-tolerant-1.0", "final": "final-2.0"}
SERVICES = ("student", "payment", "academic")


class Run:
    """Lifecycle of one experiment run: fresh database, fresh stack, probes."""

    def __init__(self, sid: str, mode: str, rep: int, pg: PgCluster, results: Path, *,
                 restart_delay_s: float = 1.0, env: dict | None = None, ha_db: bool | None = None, seed: int = 0):
        if ha_db is None:
            ha_db = (sid == "S7" and mode != "baseline")
        self.sid, self.mode, self.rep, self.pg, self.results = sid, mode, rep, pg, Path(results)
        self.stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.id = f"{sid}-{mode}-r{rep}-{self.stamp}"
        base_env = {"PAYMENT_ROLLBACK_AFTER_S": 4, "PAYMENT_LEASE_S": 1.5}
        base_env.update(env or {})
        self.stack = Stack(mode, pg, restart_delay_s=restart_delay_s, env=base_env, ha_db=ha_db)
        self.recorder = Recorder()
        self.sec = self.stack.security
        self.client = Client(self.stack.gateway_url, timeout=5.0, seed=seed + rep, security=self.sec)
        self.wl = Workload(self.client, self.recorder, seed=seed + rep)
        self.prober = Prober(self.stack.gateway_url, headers=self.sec.auth("S0001") if self.sec else None)
        self.monitor = ReadyMonitor({n: i.url for n, i in self.stack.instances.items()})
        jobs: dict[str, list] = {}
        for n, i in self.stack.instances.items():
            jobs.setdefault("gateway" if i.service == "gateway" else f"{i.service}-service", []).append((n, f"127.0.0.1:{i.port}"))
        self.prom = PromRunner(jobs)
        self.alerts = AlertWatcher(self.prom)
        self.notes: list[str] = []

    def __enter__(self):
        self.pg.reset_db(self.stack.db_name, ROOT / f"database/schema_{self.mode}.sql")
        self.stack.start()
        if self.prom.start():
            self.alerts.start()
        time.sleep(0.5)
        self.prober.start()
        self.monitor.start()
        self.t_start = time.time()
        return self

    def __exit__(self, *exc):
        self.wl.stop()
        self.prober.stop()
        self.monitor.stop()
        self.alerts.stop()
        self.prom.stop()
        self.stack.clear_faults() if exc[0] is None else None
        self.stack.stop()

    # ------------------------------------------------------------------ helpers
    def settle(self):
        """Give the recovery machinery (payment reconciler) time to finish before consistency checks."""
        time.sleep(6.5 if self.mode != "baseline" else 1.0)

    def ev(self, type_: str, name: str, after: float):
        return next((e["ts"] for e in self.stack.events if e["type"] == type_ and e["name"] == name and e["ts"] >= after), None)

    def gw_event(self, backend_url: str, state: str, after: float):
        evs = self.stack.gateway_events().get("events", [])
        return next((e["ts"] for e in evs if e["backend"] == backend_url and e["state"] == state and e["ts"] >= after), None)

    def finalize(self, *, failure_injected: str, initial_state: dict, t_fail: float, t_rec_start: float | None,
                 t_rec_end: float | None, d_internal: float | None = None, monitor_names=None,
                 affected: tuple = SERVICES, extra: dict | None = None, expected_batches=None,
                 duplicate_requests: int | None = None, settle: bool = True) -> dict:
        recs = self.recorder.snapshot()
        t_end = time.time()
        # --- detection signals
        m = self.monitor.first_unhealthy_after(t_fail, monitor_names)
        d_monitor = round(m - t_fail, 3) if m else None
        fails = [r.t1 for r in recs if not r.ok and r.t1 >= t_fail]
        d_client = round(min(fails) - t_fail, 3) if fails else None
        signals = [x for x in (d_monitor, d_internal, d_client) if x is not None]
        # --- request accounting
        win_end = t_rec_end or t_end
        window = [r for r in recs if t_fail <= r.t0 <= win_end]
        total_s, window_s = summarize(recs), summarize(window)
        per_op = {op: summarize([r for r in recs if r.op == op]) for op in sorted({r.op for r in recs})}
        extra_attempts = sum(1 for r in recs if r.attempt > 1)
        dup_sent = duplicate_requests if duplicate_requests is not None else extra_attempts
        # --- consistency
        if settle:
            self.settle()
        acked_p = [r.payment_id for r in recs if r.ok and r.payment_id]
        acked_g = [r.grade_key for r in recs if r.ok and r.grade_key] + list(getattr(self, "acked_grade_keys", []))
        cons = check(self.stack.database_url, acked_p, acked_g, expected_batches)
        # --- availability seen by black-box probes (whole run after failure start)
        avail = {s: round(self.prober.availability(s, t_fail, t_end, strict=True), 4) for s in SERVICES}
        avail_lenient = {s: round(self.prober.availability(s, t_fail, t_end, strict=False), 4) for s in SERVICES}
        downtime = {s: round(self.prober.downtime(s, t_fail, t_end, strict=False), 2) for s in SERVICES}
        downtime_strict = {s: round(self.prober.downtime(s, t_fail, t_end, strict=True), 2) for s in SERVICES}
        rec = {
            "experiment_id": self.id, "scenario": self.sid, "repetition": self.rep,
            "datetime": dt.datetime.fromtimestamp(self.t_start, dt.timezone.utc).isoformat(timespec="seconds"),
            "system_version": SYSTEM_VERSION[self.mode], "mode": self.mode,
            "failure_injected": failure_injected, "initial_state": initial_state,
            "failure_start": round(t_fail, 3), "failure_start_rel_s": round(t_fail - self.t_start, 2),
            "detection_time_s": min(signals) if signals else None,
            "detection_monitor_s": d_monitor, "detection_internal_s": d_internal, "detection_client_s": d_client,
            "recovery_start": round(t_rec_start, 3) if t_rec_start else None,
            "recovery_end": round(t_rec_end, 3) if t_rec_end else None,
            "recovery_duration_s": round(t_rec_end - t_fail, 3) if t_rec_end else None,
            "repair_action_s": round(t_rec_end - t_rec_start, 3) if (t_rec_end and t_rec_start) else None,
            "total_requests": total_s["total"], "successful_requests": total_s["success"],
            "failed_requests": total_s["failed"], "shed_requests": total_s["shed"], "hard_failed_requests": total_s["hard_failed"],
            "recovered_requests": total_s["recovered"],
            "degraded_requests": total_s["degraded"], "duplicate_requests": dup_sent,
            "duplicates_prevented": total_s["replays"],
            "window_total": window_s["total"], "window_failed": window_s["failed"], "window_success": window_s["success"],
            "window_error_rate": window_s["error_rate"],
            "user_visible_downtime_s": downtime, "user_visible_downtime_strict_s": downtime_strict,
            "probe_availability_strict": avail, "probe_availability_lenient": avail_lenient,
            "latency": {k: total_s[k] for k in ("p50_ms", "p95_ms", "p99_ms")},
            "per_op": per_op,
            "consistency": cons, "consistent": cons["consistent"],
            "stack_events": [e for e in self.stack.events if e["ts"] >= self.t_start],
            "observations": list(self.notes),
            "alerts": self.alert_record(t_fail, t_end) if self.prom.enabled else None,
        }
        rec.update(extra or {})
        return rec

    def alert_record(self, t_fail: float, t_end: float) -> dict:
        """System-side detection by Prometheus alert rules, relative to fault injection."""
        a = self.alerts
        fired = a.fired_between(t_fail, t_end)
        first = {n: round(a.first(n, "firing", t_fail) - t_fail, 2) for n in fired}
        return {"false_alerts_before_fault": a.fired_between(self.t_start, t_fail),
                "fired_after_fault": fired, "first_firing_after_fault_s": first,
                "detection_by_alert_s": min(first.values()) if first else None,
                "timeline": [{**t, "rel_s": round(t["ts"] - t_fail, 2)} for t in a.transitions]}

    def start_workload(self, duration: float, rate: float = 40.0) -> threading.Thread:
        th = threading.Thread(target=self.wl.run_open_loop, args=(duration, rate), daemon=True)
        th.start()
        return th


# =============================================================================== S1
def s1_app_crash(run: Run, pre=8.0, total=32.0, rate=40.0, operator_delay=10.0) -> dict:
    st = run.stack
    th = run.start_workload(total, rate)
    time.sleep(pre)
    target = "payment-1"
    url = st.instances[target].url
    t_fail = st.kill_instance(target)
    if run.mode == "baseline":
        run.notes.append(f"No orchestrator: the dead instance stays down until an operator restarts it (assumed reaction time {operator_delay}s).")
        time.sleep(operator_delay)
        t_rs = time.time()
        st.operator_restart([target])
        t_ready = run.ev("restart_ready", target, t_fail)
        t_end = t_ready
        d_int = None
    else:
        t_rs = None
        for _ in range(300):
            t_rs = run.ev("restart_start", target, t_fail)
            t_ready = run.ev("restart_ready", target, t_fail)
            if t_ready:
                break
            time.sleep(0.1)
        time.sleep(1.5)   # let the gateway's health checker see the replica again
        g_down, g_up = run.gw_event(url, "down", t_fail), run.gw_event(url, "up", t_fail)
        d_int = round(g_down - t_fail, 3) if g_down else None
        t_end = max(x for x in (t_ready, g_up) if x)
        run.notes.append("Gateway ejected the dead replica and routed to payment-2; supervisor restarted payment-1 automatically.")
    th.join()
    return run.finalize(
        failure_injected="SIGKILL of application instance payment-1", t_fail=t_fail, t_rec_start=t_rs, t_rec_end=t_end,
        initial_state={"replicas_per_service": st.replicas, "db": "up", "note": "steady 40 req/s mixed workload"},
        d_internal=d_int, monitor_names=[target], affected=("payment",),
        extra={"operator_delay_s": operator_delay if run.mode == "baseline" else None})


# =============================================================================== S2
def s2_db_failure(run: Run, pre=8.0, outage=8.0, total=32.0, rate=40.0) -> dict:
    st, pg = run.stack, run.pg
    th = run.start_workload(total, rate)
    time.sleep(pre)
    t_fail = time.time()
    pg.stop("immediate")                       # crash-stop: no shutdown checkpoint, WAL replay on restart
    time.sleep(max(0.0, outage - (time.time() - t_fail)))
    t_rs = time.time()
    pg.start()
    t_dbup = pg.wait_up()
    th.join()
    # full functionality = every service strictly OK for 5 consecutive probes after the DB is back
    t_end = run.prober.full_ok_since(t_dbup, k=5)
    d_int = None
    if run.mode != "baseline":
        ev = run.stack.gateway_events().get("events", [])
        downs = [e["ts"] for e in ev if e["state"] == "down" and e["ts"] >= t_fail]
        d_int = round(min(downs) - t_fail, 3) if downs else None
        run.notes.append("Reads fell back to the last-known-good cache (degraded); writes were retried with backoff inside a 6 s budget.")
    else:
        run.notes.append("Baseline opens a new connection per request and has no retry: every request during the outage fails; it recovers by itself once the DB is back.")
    return run.finalize(
        failure_injected=f"PostgreSQL crash-stop (pg_ctl -m immediate) for {outage:.0f}s, then restart", t_fail=t_fail,
        t_rec_start=t_rs, t_rec_end=t_end, d_internal=d_int,
        initial_state={"replicas_per_service": st.replicas, "db": "up"},
        extra={"db_outage_s": round(t_rs - t_fail, 2), "db_back_s": round(t_dbup - t_fail, 2)})


# =============================================================================== S3
def s3_service_timeout(run: Run, pre=8.0, fault_s=20.0, total=46.0, rate=40.0, latency_ms=3000, error_rate=0.3) -> dict:
    st = run.stack
    th = run.start_workload(total, rate)
    time.sleep(pre)
    t_fail = st.set_fault("student", latency_ms=latency_ms, error_rate=error_rate)
    time.sleep(fault_s)
    st.set_fault("student")              # clear
    t_clear = time.time()
    th.join()
    t_end = run.prober.full_ok_since(t_clear, k=5)
    d_int, closed_ts = None, None
    if run.mode != "baseline":
        opens, closes = [], []
        for n in ("payment-1", "payment-2", "academic-1", "academic-2"):
            s = st.admin_state(n)
            for e in ((s or {}).get("breakers", {}).get("student-service", {}).get("events", [])):
                (opens if e["to"] == "open" else closes if e["to"] == "closed" else []).append(e["ts"])
        o = [t for t in opens if t >= t_fail]
        d_int = round(min(o) - t_fail, 3) if o else None
        c = [t for t in closes if t >= t_clear]
        closed_ts = min(c) if c else None
        run.notes.append("Circuit breakers on payment/academic opened after consecutive timeouts, then fast-failed into degraded mode "
                         "(payment accepted unverified, transcript without student profile).")
    else:
        run.notes.append("Baseline waits on the slow dependency (30 s timeout): worker threads pile up and latency propagates to callers.")
    return run.finalize(
        failure_injected=f"student-service gray failure: +{latency_ms} ms latency and {int(error_rate*100)}% HTTP 503 on all replicas for {fault_s:.0f}s",
        t_fail=t_fail, t_rec_start=t_clear, t_rec_end=t_end, d_internal=d_int,
        initial_state={"replicas_per_service": st.replicas, "db": "up"},
        extra={"breaker_closed_after_clear_s": round(closed_ts - t_clear, 2) if closed_ts else None,
               "fault": {"latency_ms": latency_ms, "error_rate": error_rate, "duration_s": fault_s}})


# =============================================================================== S4
def s4_node_failure(run: Run, pre=8.0, total=40.0, rate=40.0, operator_delay=15.0) -> dict:
    st = run.stack
    if run.mode != "baseline":
        st.restart_delay_s = 5.0      # a replacement node/pod needs time to be scheduled and booted
    th = run.start_workload(total, rate)
    time.sleep(pre)
    t_fail, names = st.kill_node("A")
    if run.mode == "baseline":
        run.notes.append(f"All services live on the single node: total outage until an operator restores it (assumed {operator_delay}s).")
        time.sleep(operator_delay)
        t_rs = time.time()
        st.operator_restart(names)
        t_end = max(run.ev("restart_ready", n, t_fail) for n in names)
        d_int = None
    else:
        t_rs, t_ready = None, {}
        for _ in range(400):
            t_ready = {n: run.ev("restart_ready", n, t_fail) for n in names}
            if all(t_ready.values()):
                break
            time.sleep(0.1)
        t_rs = min(run.ev("restart_start", n, t_fail) for n in names)
        time.sleep(1.5)
        gdown = [run.gw_event(st.instances[n].url, "down", t_fail) for n in names]
        gup = [run.gw_event(st.instances[n].url, "up", t_fail) for n in names]
        d_int = round(min(g for g in gdown if g) - t_fail, 3) if any(gdown) else None
        t_end = max([t for t in t_ready.values() if t] + [g for g in gup if g])
        run.notes.append("Node A (student-1, payment-1, academic-1) was lost; node B replicas kept serving through the gateway.")
    th.join()
    return run.finalize(
        failure_injected="Node A lost: SIGKILL of every instance placed on it", t_fail=t_fail, t_rec_start=t_rs, t_rec_end=t_end,
        initial_state={"replicas_per_service": st.replicas, "nodes": sorted({i.node for i in st.instances.values() if i.service != 'gateway'}), "db": "up"},
        d_internal=d_int, monitor_names=names,
        extra={"killed_instances": names, "operator_delay_s": operator_delay if run.mode == "baseline" else None,
               "node_restart_delay_s": st.restart_delay_s if run.mode != "baseline" else None})


# =============================================================================== S5
def s5_transaction(run: Run, n_payments=60, n_batches=20, batch_size=5, workers=6) -> dict:
    client, rec_ = run.client, run.recorder
    rng = random.Random(1000 + run.rep)
    plan = []
    for i in range(n_payments):
        x = rng.random()
        kind = "normal" if x < 0.5 else "interrupt" if x < 0.8 else "double"
        plan.append((i, kind, kind == "interrupt" and rng.random() < 0.34))
    recover_times, lock = [], threading.Lock()
    first_fault = {"t": None}

    def send(i, attempt, fault=None):
        body = {"student_id": f"S{(i % 50) + 1:04d}", "invoice_id": f"INV-S5-{i}", "amount": 1000.0}
        h = {"Idempotency-Key": f"KEY-S5-{i}", **(run.sec.auth(body["student_id"]) if run.sec else {})}
        if fault:
            h["X-Fault"] = fault
            with lock:
                first_fault["t"] = first_fault["t"] or time.time()
        r = Rec(op="post_payment", t0=time.time(), logical_id=f"INV-S5-{i}", attempt=attempt, phase="s5")
        client.call(r, "POST", "/payments", json=body, headers=h)
        rec_.add(r)
        return r

    def retry_until_ok(i, first_attempt, delays, t_failed):
        a = first_attempt
        for d in delays:
            time.sleep(d)
            a += 1
            r = send(i, a)
            if r.ok:
                with lock:
                    recover_times.append(r.t1 - t_failed)
                return

    def do_payment(item):
        i, kind, abandon = item
        if kind == "normal":
            send(i, 1)
        elif kind == "interrupt":
            r = send(i, 1, fault="interrupt:after_pending")
            if not abandon:
                retry_until_ok(i, 1, [0.7, 1.4, 2.0, 2.0], r.t1)
        else:  # double submit: two identical requests at the same instant (double click / network duplicate)
            results = []
            ts = [threading.Thread(target=lambda a=a: results.append(send(i, a))) for a in (1, 2)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            for r in results:
                if not r.ok and r.status in (409, 500, 503):
                    retry_until_ok(i, 2, [0.5, 1.0, 2.0], r.t1)
                    break

    t0 = time.time()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(do_payment, plan))

    # academic batches ---------------------------------------------------------------
    expected, acked_keys = {}, []
    bplan = [(j, rng.random() < 0.3) for j in range(n_batches)]

    def do_batch(item):
        j, faulty = item
        bid = f"B-S5-{j}-{run.rep}"
        recs = [{"student_id": f"S{((j * batch_size + k) % 50) + 1:04d}", "course_code": f"S5C{j}{k}", "term": "2026F", "grade": 80.0}
                for k in range(batch_size)]
        expected[bid] = batch_size
        last_fail = None
        for attempt, d in enumerate([0, 0.3, 0.6, 1.0], start=1):
            time.sleep(d)
            h = {"X-Fault": "interrupt:mid_batch"} if (faulty and attempt == 1) else {}
            if run.sec:
                h.update(run.sec.auth("REG01"))
            if "X-Fault" in h:
                with lock:
                    first_fault["t"] = first_fault["t"] or time.time()
            r = Rec(op="post_batch", t0=time.time(), logical_id=bid, attempt=attempt, phase="s5")
            client.call(r, "POST", "/grades/batch", json={"batch_id": bid, "records": recs}, headers=h)
            rec_.add(r)
            if r.ok:
                acked_keys.extend(f"{x['student_id']}|{x['course_code']}|{x['term']}" for x in recs)
                if last_fail:
                    with lock:
                        recover_times.append(r.t1 - last_fail)
                return
            last_fail = last_fail or r.t1

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(do_batch, bplan))
    run.acked_grade_keys = acked_keys
    t_end = time.time()

    run.settle()                       # let the payment reconciler finish before reading DB-side statistics
    t_fail = first_fault["t"] or t0
    recs = rec_.snapshot()
    faulted_first = [r for r in recs if r.attempt == 1 and r.t0 >= t_fail and r.error]
    d_client = None
    if faulted_first:
        d_client = round(min(r.t1 for r in faulted_first) - t_fail, 3)
    logical_ops = n_payments + n_batches
    dup_sent = len(recs) - logical_ops
    # rollback latency measured by the database (created_at -> rolled back)
    rb = None
    try:
        with psycopg.connect(run.pg.dsn(run.stack.db_name), autocommit=True) as c:
            r = c.execute("SELECT avg(extract(epoch FROM updated_at - created_at)), count(*) FROM payment.payments WHERE status='ROLLED_BACK'").fetchone()
            rb = (round(float(r[0]), 2) if r[0] is not None else None, r[1])
    except Exception:
        pass
    acked_logical = {r.logical_id for r in recs if r.ok and r.op == "post_payment"}
    stats = {"logical_payments_planned": n_payments, "logical_payments_acknowledged": len(acked_logical),
             "logical_batches_planned": n_batches,
             "logical_batches_acknowledged": len({r.logical_id for r in recs if r.ok and r.op == "post_batch"}),
             "interrupted_payments": sum(1 for _, k, _a in plan if k == "interrupt"),
             "abandoned_payments": sum(1 for _, k, a in plan if a), "double_submitted_payments": sum(1 for _, k, _a in plan if k == "double"),
             "interrupted_batches": sum(1 for _, f in bplan if f), "retry_recoveries_measured": len(recover_times),
             "mean_client_recovery_s": round(sum(recover_times) / len(recover_times), 3) if recover_times else None,
             "p95_client_recovery_s": round(pct(recover_times, 95), 3) if recover_times else None,
             "db_rollbacks": {"avg_age_at_rollback_s": rb[0], "count": rb[1]} if rb else None}
    if run.mode != "baseline":
        run.notes.append("Interrupted payments stayed PENDING (checkpoint 1); a retry with the same Idempotency-Key resumed them, "
                         "abandoned ones were rolled back by the reconciler; batches were rolled back atomically.")
    else:
        run.notes.append("Baseline commits payment row and ledger update separately and has no idempotency check: an interruption "
                         "corrupts the ledger and the client's retry creates a second payment.")
    # recovery_end: when the last retried operation finally succeeded
    return run.finalize(
        failure_injected="Interrupted transactions: payment interrupted after first write (30%), grade batch interrupted mid-way (30%), "
                         "double-submitted payments (20%), abandoned payments",
        t_fail=t_fail, t_rec_start=t_fail, t_rec_end=t_end,
        initial_state={"replicas_per_service": run.stack.replicas, "db": "up", "payments_planned": n_payments, "batches_planned": n_batches},
        expected_batches=expected, duplicate_requests=dup_sent, extra=stats, settle=False)


# =============================================================================== S6
def s6_high_load(run: Run, steps=(5, 25, 75, 150), step_s=12.0, cooldown_s=12.0) -> dict:
    st = run.stack
    phases = []
    base_conc = steps[0]
    t_peak_start = t_peak_end = None
    for conc in steps:
        name = f"c{conc}"
        run.wl.phase = name
        t0 = time.time()
        if conc == steps[-1]:
            t_peak_start = t0
        run.wl.run_closed_loop_async(conc, step_s)
        phases.append((name, conc, t0, time.time()))
    t_peak_end = time.time()
    run.wl.phase = "cooldown"
    t0 = time.time()
    run.wl.run_closed_loop_async(base_conc, cooldown_s)
    phases.append(("cooldown", base_conc, t0, time.time()))
    recs = run.recorder.snapshot()
    per_phase = {}
    for name, conc, a, b in phases:
        sub = [r for r in recs if r.phase == name]
        s = summarize(sub)
        s.update({"concurrency": conc, "throughput_rps": round(s["success"] / (b - a), 1), "offered_rps": round(s["total"] / (b - a), 1),
                  "duration_s": round(b - a, 1)})
        per_phase[name] = s
    # SLO windows (1 s): breach = p95 > 1000 ms or error rate > 2 %
    def windows(a, b):
        out = []
        t = a
        while t < b:
            w = [r for r in recs if t <= r.t1 < t + 1.0]
            if w:
                s = summarize(w)
                out.append((t, s["p95_all_ms"], s["hard_error_rate"]))
            t += 1.0
        return out
    breach = next((t for t, p95, er in windows(t_peak_start, t_peak_end) if p95 > 1000 or er > 0.02), None)
    base_p95 = per_phase[f"c{base_conc}"]["p95_all_ms"]
    rec_t = next((t for t, p95, er in windows(t_peak_end, time.time()) if p95 < max(2 * base_p95, 100) and er == 0), None)
    shed = (sum(st.service_metric(n, "load_shed_total") for n in st.instances if n != "gateway")
            + st.service_metric("gateway", "gateway_load_shed_total")) if run.mode != "baseline" else 0
    degraded = sum(st.service_metric(n, "degraded_responses_total") for n in st.instances if n != "gateway")
    if run.mode != "baseline":
        run.notes.append("FT gateway/services shed excess load (HTTP 503 + Retry-After) above the in-flight limits and skip optional downstream calls above DEGRADE_INFLIGHT.")
    else:
        run.notes.append("Baseline accepts all work and queues it: latency grows with concurrency instead of requests being rejected.")
    return run.finalize(
        failure_injected=f"Closed-loop load steps of {list(steps)} concurrent clients ({step_s:.0f}s each), then cool-down at {base_conc}",
        t_fail=t_peak_start, t_rec_start=t_peak_end, t_rec_end=rec_t, d_internal=None,
        initial_state={"replicas_per_service": st.replicas, "db": "up"},
        extra={"per_phase": per_phase, "slo_breach_after_s": round(breach - t_peak_start, 2) if breach else None,
               "recovery_after_peak_s": round(rec_t - t_peak_end, 2) if rec_t else None,
               "load_shed_total": shed, "degraded_responses_total": degraded,
               "slo": "1s window breach = p95 > 1000 ms or hard (non-shed) error rate > 2 %"})


# =============================================================================== S7
def s7_db_primary_loss(run: Run, pre=8.0, total=48.0, rate=40.0, operator_delay=20.0) -> dict:
    """Permanent loss of the primary database host.

    baseline: logical backup (pg_dump) taken at the start, an operator rebuilds the DB from it.
    ft:       asynchronous streaming-replication standby + failover controller (promotes after 3 failed
              health probes); services use a multi-host DSN and reconnect to the promoted node by themselves.
    """
    pg, st = run.pg, run.stack
    dump = None
    if run.mode != "baseline":
        pg.start_standby()
        caught_up = pg.wait_standby_caught_up()
        run.notes.append(f"Standby streaming from primary, caught up before test: {caught_up}.")
    else:
        dump = pg.backup(st.db_name, run.results / "tmp_backup.dump")
        run.notes.append("Backup-only strategy: pg_dump taken at start of the run; everything written afterwards is at risk.")
    th = run.start_workload(total, rate)
    time.sleep(pre)
    t_fail = time.time()
    pg.destroy_primary()
    t_promoted = None
    if run.mode != "baseline":
        # failover controller (stand-in for Patroni/repmgr): promote after 3 consecutive failed probes
        fails = 0
        while fails < 3:
            fails = 0 if pg.is_up() else fails + 1
            time.sleep(0.25)
        t_rs = time.time()
        pg.promote_standby()
        t_promoted = pg.wait_up(port=pg.standby_port)
        t_dbup, d_int = t_promoted, round(t_rs - t_fail, 3)
        # promotion finishes when the standby accepts read-write sessions
        for _ in range(100):
            try:
                with psycopg.connect(pg.dsn_ha(st.db_name), connect_timeout=2, autocommit=True) as c:
                    c.execute("CREATE TEMP TABLE _rw_probe(x int)")
                break
            except Exception:
                time.sleep(0.1)
        t_dbup = time.time()
    else:
        time.sleep(operator_delay)
        t_rs = time.time()
        pg.rebuild_from_backup(st.db_name, dump)
        t_dbup, d_int = time.time(), None
    th.join()
    t_end = run.prober.full_ok_since(t_dbup, k=5)
    rec = run.finalize(
        failure_injected="Permanent loss of the primary database host (SIGKILL + data directory deleted)",
        t_fail=t_fail, t_rec_start=t_rs, t_rec_end=t_end, d_internal=d_int,
        initial_state={"replicas_per_service": st.replicas, "db": "primary" + (" + hot standby" if run.mode != "baseline" else " + nightly-style backup")},
        extra={"db_available_again_s": round(t_dbup - t_fail, 2), "operator_delay_s": operator_delay if run.mode == "baseline" else None})
    c = rec["consistency"]
    rec["rpo_lost_acknowledged_writes"] = c["lost_acknowledged_payments"] + c["lost_acknowledged_grades"]
    return rec


# sid -> (title, function, function kwargs, extra environment for the services)
# S6: admission-control thresholds are tuned to the capacity of the 2-vCPU test machine (Little's law:
# in-flight = throughput x latency); on bigger hardware they should be raised.
SCENARIOS = {
    "S1": ("Application crash", s1_app_crash, {}, {}),
    "S2": ("Database failure", s2_db_failure, {}, {}),
    "S3": ("Network/service timeout", s3_service_timeout, {}, {}),
    "S4": ("Node/hardware failure", s4_node_failure, {}, {}),
    "S5": ("Corrupted/lost transaction", s5_transaction, {}, {}),
    "S6": ("High load", s6_high_load, {}, {"MAX_INFLIGHT": 20, "DEGRADE_INFLIGHT": 6, "GATEWAY_MAX_INFLIGHT": 24}),
    "S7": ("Primary database loss (replication vs backup)", s7_db_primary_loss, {}, {}),
}

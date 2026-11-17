"""Final-project scenarios added to the midterm set (S1-S7).

S0  steady-state overhead      same open-loop load, no faults: latency and CPU cost of each configuration
S8  monitoring & alert trigger three faults in sequence; detection/resolution by Prometheus alert rules,
                               false positives during warm-up, diagnosability of failed requests from logs
S9  unauthorized / invalid     a battery of misuse cases (auth, RBAC, BOLA, bypass, injection, tampering,
    operations                 oversized/malformed input, admin backdoor, brute force, flood) against
                               concurrent legitimate traffic; denial, audit and data-integrity evidence

All functions follow the protocol of scenarios.py and return one record via Run.finalize().
"""
from __future__ import annotations

import base64
import json
import threading
import time
from collections import defaultdict
from pathlib import Path

import httpx
import psycopg

from ..workload.generator import pct, summarize
from .scenarios import Run


# =============================================================================== S0
def s0_overhead(run: Run, warm=4.0, duration=30.0, rate=40.0) -> dict:
    st = run.stack
    th = run.start_workload(warm + duration, rate)
    time.sleep(warm)
    t_fail = time.time()                 # "measurement start" (no fault is injected)
    cpu0 = st.cpu_seconds()
    time.sleep(duration)
    cpu1 = st.cpu_seconds()
    th.join()
    t_end = time.time()
    recs = [r for r in run.recorder.snapshot() if t_fail <= r.t0 <= t_end]
    s = summarize(recs)
    cpu = {n: round(cpu1[n] - cpu0.get(n, 0.0), 3) for n in cpu1}
    cpu_total = round(sum(cpu.values()), 3)
    run.notes.append("No fault injected: measures the cost (latency, CPU) of the configuration under identical load.")
    return run.finalize(
        failure_injected="none (steady state)", t_fail=t_fail, t_rec_start=None, t_rec_end=None,
        initial_state={"replicas_per_service": st.replicas, "db": "up", "rate_rps": rate},
        extra={"measure_window_s": duration, "measured": s, "cpu_seconds": cpu, "cpu_seconds_total": cpu_total,
               "cpu_ms_per_request": round(cpu_total * 1000 / max(s["total"], 1), 2),
               "rss_mb": st.rss_mb(), "rss_mb_total": round(sum(st.rss_mb().values()), 1)})


# =============================================================================== S8
EXPECTED = {
    "instance_crash": ("InstanceDown", "InstanceRecentlyDown", "HighErrorRatio"),
    "slow_dependency": ("HighLatencyP95", "CircuitBreakerOpen", "DegradedResponses", "HighErrorRatio"),
    "database_outage": ("DatabaseUnreachable", "HighErrorRatio", "DegradedResponses"),
}


def _load_logs(logdir: Path) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(Path(logdir).glob("*.log")):
        for line in f.read_text(errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            rid = j.get("request_id")
            if rid:
                idx[rid].append(j)
    return idx


def _diagnose(recs, idx) -> dict:
    """For every failed or degraded client request: can the incident be traced from telemetry alone?

    found       the client's request id appears in at least one log line
    localized   a log line for that id names the failing/degrading hop (service + instance) and a cause
                (status >= 500, an error text or a degraded reason)
    multi_hop   lines from >= 2 different services share the id (trace spans across hops)
    """
    bad = [r for r in recs if (not r.ok and not r.shed) or r.degraded]
    found = localized = multi = 0
    for r in bad:
        lines = idx.get(r.request_id, [])
        if lines:
            found += 1
        if any((l.get("status") or 0) >= 500 or l.get("error") or l.get("degraded") for l in lines
               if l.get("service") and (l.get("instance") or l.get("target_service"))):
            localized += 1
        if len({l.get("service") for l in lines}) >= 2:
            multi += 1
    n = len(bad)
    return {"incident_requests": n, "found_in_logs": found, "localized_cause": localized, "multi_hop_trace": multi,
            "found_ratio": round(found / n, 4) if n else None, "localized_ratio": round(localized / n, 4) if n else None,
            "multi_hop_ratio": round(multi / n, 4) if n else None}


def _sample_trace(recs, idx):
    """One degraded transcript request with its full span tree (evidence for the report)."""
    for r in recs:
        if r.op == "get_transcript" and r.degraded and len({l.get("service") for l in idx.get(r.request_id, [])}) >= 2:
            spans = sorted(idx[r.request_id], key=lambda l: l.get("epoch", 0))
            keep = ("ts", "service", "instance", "msg", "operation", "status", "latency_ms", "span_id", "parent_span_id",
                    "degraded", "error", "retry_count", "actor", "role", "served_by", "gateway_attempts")
            return {"request_id": r.request_id, "client_status": r.status, "client_degraded": r.degraded,
                    "spans": [{k: l.get(k) for k in keep if l.get(k) is not None} for l in spans]}
    return None


def s8_observability(run: Run, rate=30.0, warmup=14.0, crash_obs=16.0, slow_s=12.0, slow_obs=8.0, db_s=6.0,
                     tail=12.0, latency_ms=1500, operator_delay=10.0) -> dict:
    st, pg = run.stack, run.pg
    total = warmup + crash_obs + slow_s + slow_obs + db_s + tail
    th = run.start_workload(total, rate)
    faults = []
    time.sleep(warmup)

    # --- fault a: instance crash
    t_a = st.kill_instance("payment-1")
    if run.mode == "baseline":
        def op_restart():
            time.sleep(operator_delay)
            st.operator_restart(["payment-1"])
        threading.Thread(target=op_restart, daemon=True).start()
    faults.append({"fault": "instance_crash", "t_inject": t_a, "t_clear": None})
    time.sleep(crash_obs)

    # --- fault b: slow dependency (gray failure, latency only)
    t_b = st.set_fault("student", latency_ms=latency_ms)
    time.sleep(slow_s)
    st.set_fault("student")
    t_b_clear = time.time()
    faults.append({"fault": "slow_dependency", "t_inject": t_b, "t_clear": t_b_clear})
    time.sleep(slow_obs)

    # --- fault c: database outage
    t_c = time.time()
    pg.stop("immediate")
    time.sleep(db_s)
    pg.start()
    pg.wait_up()
    t_c_clear = time.time()
    faults.append({"fault": "database_outage", "t_inject": t_c, "t_clear": t_c_clear})
    th.join()
    time.sleep(4.0)                      # let alerts resolve
    t_end = time.time()

    # restore crashed instance clear time (restart ready)
    ready = run.ev("restart_ready", "payment-1", t_a)
    faults[0]["t_clear"] = ready

    a = run.alerts
    nxt = [faults[1]["t_inject"], faults[2]["t_inject"], t_end]
    per_fault = []
    for f, t_next in zip(faults, nxt):
        exp = EXPECTED[f["fault"]]
        firings = {n: a.first(n, "firing", f["t_inject"]) for n in exp}
        firings = {n: t for n, t in firings.items() if t and t < t_next}
        first_name = min(firings, key=firings.get) if firings else None
        det = round(firings[first_name] - f["t_inject"], 2) if first_name else None
        # resolution: last expected alert resolved after the fault was cleared
        res = None
        if firings and f["t_clear"]:
            rs = [a.first(n, "resolved", firings[n]) for n in firings]
            if all(rs):
                res = round(max(rs) - f["t_clear"], 2)
        per_fault.append({"fault": f["fault"], "inject_rel_s": round(f["t_inject"] - run.t_start, 2),
                          "duration_s": round(f["t_clear"] - f["t_inject"], 2) if f["t_clear"] else None,
                          "alerts_fired": sorted(firings), "first_alert": first_name, "detection_s": det,
                          "resolved_after_clear_s": res})
    warm_false = a.fired_between(run.t_start, faults[0]["t_inject"])
    recs = run.recorder.snapshot()
    idx = _load_logs(st.logdir)
    diag = _diagnose([r for r in recs if r.t0 >= faults[0]["t_inject"]], idx)
    sample = _sample_trace(recs, idx) if run.mode != "baseline" else None
    # client-visible detection for comparison (first failed request after each fault)
    for f, pf, t_next in zip(faults, per_fault, nxt):
        fails = [r.t1 for r in recs if not r.ok and f["t_inject"] <= r.t0 < t_next]
        pf["first_client_error_s"] = round(min(fails) - f["t_inject"], 2) if fails else None
    run.notes.append("Three faults injected in sequence; detection = first firing of an alert rule expected for that fault.")
    rec = run.finalize(
        failure_injected=f"sequence: SIGKILL payment-1; +{latency_ms} ms latency on student-service for {slow_s:.0f}s; "
                         f"PostgreSQL crash-stop for {db_s:.0f}s",
        t_fail=faults[0]["t_inject"], t_rec_start=None, t_rec_end=t_c_clear,
        initial_state={"replicas_per_service": st.replicas, "db": "up", "prometheus": run.prom.enabled,
                       "scrape_eval_interval": "1s"},
        extra={"per_fault": per_fault, "false_alerts_during_warmup": warm_false,
               "warmup_s": warmup, "diagnosability": diag, "sample_trace": sample,
               "detected_faults": sum(1 for p in per_fault if p["detection_s"] is not None),
               "operator_delay_s": operator_delay if run.mode == "baseline" else None})
    return rec


# =============================================================================== S9
def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=").decode()


def _forged_tokens(run: Run) -> dict:
    """Tokens an attacker could present. Without the real secret they cannot be valid."""
    import jwt
    now = int(time.time())
    claims = {"sub": "S0001", "role": "admin", "iat": now, "exp": now + 600, "iss": "uis-gateway"}
    forged = jwt.encode(claims, "attacker-guessed-secret-attacker-guessed", algorithm="HS256")
    alg_none = _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(claims) + "."
    expired = tampered = None
    if run.sec:                         # a once-valid token that has expired (stolen from logs, say)
        from ftlib import security as sec
        expired = sec.issue_token(run.sec.jwt_secret, "S0001", "student", ttl_s=-60, now=now - 600)
        good = run.sec.token("S0001").split(".")
        payload = json.loads(base64.urlsafe_b64decode(good[1] + "=="))
        payload["role"] = "admin"       # privilege escalation by editing the claims, signature kept
        tampered = good[0] + "." + _b64(payload) + "." + good[2]
    else:
        expired = jwt.encode({**claims, "role": "student", "exp": now - 60}, "x" * 40, algorithm="HS256")
        tampered = forged
    return {"forged": forged, "alg_none": alg_none, "expired": expired, "tampered": tampered}


def s9_security(run: Run, rate=20.0, pre=8.0, total=44.0, flood_rps=200.0, flood_s=6.0) -> dict:
    st = run.stack
    sec = run.sec
    auth = (lambda u: sec.auth(u)) if sec else (lambda u: {})
    gw = st.gateway_url
    be = {svc: st.instances[f"{svc}-1"].url for svc in ("student", "payment", "academic")}
    tok = _forged_tokens(run)
    th = run.start_workload(total, rate)
    time.sleep(pre)
    t_fail = time.time()
    results: list[dict] = []
    c = httpx.Client(timeout=5.0)
    n = [0]

    def attack(category, name, method, url, expect, headers=None, **kw):
        n[0] += 1
        h = {"X-Request-ID": f"atk{n[0]:04d}", **(headers or {})}
        t0 = time.time()
        try:
            r = c.request(method, url, headers=h, **kw)
            status, body = r.status_code, r.text[:160]
        except httpx.HTTPError as exc:
            status, body = 0, type(exc).__name__
        results.append({"category": category, "name": name, "status": status, "expected": list(expect),
                        "blocked": 400 <= status < 500, "as_expected": status in expect, "harmful_success": 200 <= status < 300,
                        "server_error": status >= 500 or status == 0, "t_rel": round(t0 - t_fail, 3), "body": body})
        time.sleep(0.05)
        return status

    for k in range(3):
        # T1 spoofing / broken authentication (expected 401)
        attack("authentication", "no_token", "GET", f"{gw}/students/S0002", (401,))
        attack("authentication", "forged_signature", "GET", f"{gw}/students/S0002", (401,), {"Authorization": f"Bearer {tok['forged']}"})
        attack("authentication", "alg_none", "GET", f"{gw}/students/S0002", (401,), {"Authorization": f"Bearer {tok['alg_none']}"})
        attack("authentication", "expired_token", "GET", f"{gw}/students/S0002", (401,), {"Authorization": f"Bearer {tok['expired']}"})
        attack("authentication", "tampered_claims", "POST", f"{gw}/grades", (401,), {"Authorization": f"Bearer {tok['tampered']}"},
               json={"student_id": "S0002", "course_code": f"ATK-TMP{k}", "term": "2026F", "grade": 100})
        # T2 elevation of privilege (expected 403)
        attack("privilege_escalation", "student_posts_grade", "POST", f"{gw}/grades", (403,), auth("S0001"),
               json={"student_id": "S0001", "course_code": f"ATK-VG{k}", "term": "2026F", "grade": 100})
        attack("privilege_escalation", "student_registers_student", "POST", f"{gw}/students", (403,), auth("S0001"),
               json={"full_name": "Fake Student", "email": f"atk-reg{k}@evil.test"})
        attack("privilege_escalation", "student_batch_grades", "POST", f"{gw}/grades/batch", (403,), auth("S0001"),
               json={"records": [{"student_id": "S0001", "course_code": f"ATK-VB{k}", "term": "2026F", "grade": 100}]})
        # T3 information disclosure / BOLA (expected 403)
        attack("bola", "read_other_student", "GET", f"{gw}/students/S{k + 2:04d}", (403,), auth("S0001"))
        attack("bola", "read_other_transcript", "GET", f"{gw}/students/S{k + 5:04d}/transcript", (403,), auth("S0001"))
        attack("bola", "read_other_account", "GET", f"{gw}/accounts/S{k + 8:04d}", (403,), auth("S0001"))
        attack("bola", "pay_for_other_student", "POST", f"{gw}/payments", (403,),
               {"Idempotency-Key": f"ATK-BOLA-{k}-{run.rep}", **auth("S0001")},
               json={"student_id": "S0011", "invoice_id": f"INV-ATK-BOLA-{k}", "amount": 10})
        # T4 bypass of the gateway / forged service identity / admin backdoor (expected 401)
        attack("gateway_bypass", "direct_backend_read", "GET", f"{be['payment']}/accounts/S0002", (401,))
        attack("gateway_bypass", "direct_backend_grade_write", "POST", f"{be['academic']}/grades", (401,),
               json={"student_id": "S0002", "course_code": f"ATK-DIR{k}", "term": "2026F", "grade": 100})
        attack("gateway_bypass", "forged_internal_auth", "GET", f"{be['student']}/students/S0002", (401,),
               {"X-Internal-Auth": f"v1;payment-service;{time.time():.3f};{'0' * 64}"})
        # T5 tampering / injection / invalid input (expected 4xx)
        attack("invalid_input", "sqli_in_path", "GET", f"{gw}/students/S0001' OR '1'='1", (422, 403, 404), auth("FAC01"))
        attack("invalid_input", "xss_in_name", "POST", f"{gw}/students", (422,), auth("REG01"),
               json={"full_name": "<script>alert(1)</script>", "email": f"atk-xss{k}-{run.rep}@evil.test"})
        attack("invalid_input", "negative_amount", "POST", f"{gw}/payments", (422,),
               {"Idempotency-Key": f"ATK-NEG-{k}-{run.rep}", **auth("S0001")},
               json={"student_id": "S0001", "invoice_id": f"INV-ATK-NEG-{k}", "amount": -5000})
        attack("invalid_input", "grade_out_of_range", "POST", f"{gw}/grades", (422,), auth("FAC01"),
               json={"student_id": "S0001", "course_code": f"ATK-G150-{k}", "term": "2026F", "grade": 150})
        attack("invalid_input", "oversized_body", "POST", f"{gw}/grades", (413,), {**auth("FAC01"), "content-type": "application/json"},
               content=json.dumps({"student_id": "S0001", "course_code": f"ATK-BIG{k}", "term": "2026F", "grade": 50,
                                   "pad": "A" * 65536}).encode())
        attack("invalid_input", "malformed_json", "POST", f"{gw}/payments", (400, 403, 422),
               {"Idempotency-Key": f"ATK-MAL-{k}-{run.rep}", "content-type": "application/json", **auth("S0001")},
               content=b'{"student_id": "S0001", "invoice_id": "INV-ATK-MAL", "amount": ')
        # idempotency tampering: a legitimate payment, then the same key with a modified amount (expected 409)
        key = f"TMP-{k}-{run.rep}-{int(t_fail)}"
        body = {"student_id": "S0001", "invoice_id": f"INV-TMP-{k}-{int(t_fail)}", "amount": 1000}
        c.post(f"{gw}/payments", json=body, headers={"Idempotency-Key": key, **auth("S0001")})
        attack("invalid_input", "idempotency_key_tamper", "POST", f"{gw}/payments", (409, 422),
               {"Idempotency-Key": key, **auth("S0001")}, json={**body, "amount": 50000})

    # T7 admin backdoor: switch a service into fail-all mode via the fault-injection endpoint
    t_bd = time.time()
    status_bd = attack("gateway_bypass", "admin_backdoor_fail_all", "POST", f"{be['student']}/admin/fault", (401,),
                       json={"fail_all": True})
    if 200 <= status_bd < 300:          # attack worked (baseline): the operator notices and clears it after 2 s
        time.sleep(2.0)
        st.set_fault("student")
        run.notes.append("Admin backdoor succeeded: student-1 answered 503 to everybody until the operator cleared it (2 s).")
    # T6 credential brute force against the token endpoint
    brute = []
    for i in range(15):
        try:
            r = c.post(f"{gw}/auth/token", json={"username": "S0010", "password": f"guess{i}"})
            brute.append(r.status_code)
        except httpx.HTTPError:
            brute.append(0)
    t_battery_end = time.time()

    # T8 flood by one authenticated principal
    flood = {"sent": 0, "status": defaultdict(int)}
    time.sleep(1.0)
    t_flood = time.time()

    def flood_worker(k):
        fc = httpx.Client(timeout=5.0, limits=httpx.Limits(max_connections=4))
        h = auth("S0049")
        end = t_flood + flood_s
        i = 0
        per = flood_rps / 8
        while time.time() < end:
            t_s = t_flood + i / per
            d = t_s - time.time()
            if d > 0:
                time.sleep(d)
            try:
                code = fc.get(f"{gw}/students/S0049", headers=h).status_code
            except httpx.HTTPError:
                code = 0
            flood["status"][code] += 1
            i += 1
        fc.close()

    ths = [threading.Thread(target=flood_worker, args=(k,)) for k in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    t_flood_end = time.time()
    th.join()
    t_end = time.time()

    # ---------------------------------------------------------------- evaluation
    recs = run.recorder.snapshot()

    def legit(a, b):
        sub = [r for r in recs if a <= r.t0 < b]
        s = summarize(sub)
        return {"total": s["total"], "success_ratio": round(s["success"] / s["total"], 4) if s["total"] else None,
                "p95_ms": s["p95_ms"], "p50_ms": s["p50_ms"]}

    by_cat = defaultdict(lambda: {"attempts": 0, "blocked": 0, "as_expected": 0, "harmful_success": 0, "server_error": 0})
    for x in results:
        d = by_cat[x["category"]]
        d["attempts"] += 1
        for f in ("blocked", "as_expected", "harmful_success", "server_error"):
            d[f] += int(x[f])
    total_att = len(results)
    flood_total = sum(flood["status"].values())
    throttled = flood["status"].get(429, 0)
    # audit / tamper evidence (final only)
    audit = None
    if sec:
        dsn = st.database_url
        with psycopg.connect(dsn, autocommit=True) as conn:
            deny = conn.execute("SELECT action, count(*) FROM security.audit_events WHERE outcome='DENY' "
                                "AND epoch_ms >= %s GROUP BY action ORDER BY 2 DESC", (int(t_fail * 1000),)).fetchall()
            sample = conn.execute("SELECT chain_id, seq, service, actor, role, action, resource, outcome, status_code, detail, "
                                  "prev_hash, event_hash FROM security.audit_events WHERE outcome='DENY' AND epoch_ms >= %s "
                                  "ORDER BY epoch_ms LIMIT 3", (int(t_fail * 1000),)).fetchall()
        audit = {"deny_by_action": {a: n_ for a, n_ in deny}, "sample_deny_records": [list(r) for r in sample]}
    rec = run.finalize(
        failure_injected=f"{total_att} misuse requests in 8 categories + 15 brute-force logins + "
                         f"{flood_rps:.0f} req/s flood for {flood_s:.0f}s by one principal",
        t_fail=t_fail, t_rec_start=None, t_rec_end=None,
        initial_state={"replicas_per_service": st.replicas, "db": "up", "legit_rate_rps": rate,
                       "security_controls": bool(sec)},
        extra={"attacks": results, "attack_summary": dict(by_cat), "attack_total": total_att,
               "attacks_blocked": sum(x["blocked"] for x in results),
               "attacks_as_expected": sum(x["as_expected"] for x in results),
               "attacks_harmful_success": sum(x["harmful_success"] for x in results),
               "attacks_server_error": sum(x["server_error"] for x in results),
               "admin_backdoor_status": status_bd, "admin_backdoor_rel_s": round(t_bd - t_fail, 2),
               "bruteforce_statuses": brute,
               "flood": {"sent": flood_total, "throttled_429": throttled, "served_200": flood["status"].get(200, 0),
                         "other": {str(k): v for k, v in flood["status"].items() if k not in (200, 429)},
                         "throttled_ratio": round(throttled / flood_total, 4) if flood_total else None},
               "legit_before": legit(run.t_start, t_fail), "legit_during_battery": legit(t_fail, t_battery_end),
               "legit_during_flood": legit(t_flood, t_flood_end), "legit_after": legit(t_flood_end, t_end),
               "audit": audit})
    # tamper-evidence check on the audit chain (final only): modify one stored row, verify, report
    if sec and rec["consistency"].get("audit_events"):
        rec["audit_tamper_test"] = _tamper_test(st.database_url)
    return rec


def _tamper_test(dsn: str) -> dict:
    """Edit one audit row behind the application's back and check that verification detects it."""
    import sys
    from .pg import ROOT
    sys.path.insert(0, str(ROOT / "shared"))
    from ftlib.security import verify_chain
    cols = ("chain_id", "seq", "epoch_ms", "service", "request_id", "actor", "role", "action", "resource", "outcome",
            "status_code", "detail", "prev_hash", "event_hash")
    with psycopg.connect(dsn, autocommit=True) as c:
        blocked = False
        try:
            c.execute("UPDATE security.audit_events SET detail='edited' WHERE seq=1")
        except psycopg.Error:
            blocked = True                                # append-only trigger refused the edit
        with c.transaction():
            c.execute("SET LOCAL uis.allow_audit_tamper = 'on'")   # simulate a privileged attacker / DBA
            victim = c.execute("SELECT chain_id, seq FROM security.audit_events ORDER BY epoch_ms LIMIT 1 OFFSET 5").fetchone()
            c.execute("UPDATE security.audit_events SET detail = detail || ' (edited)' WHERE chain_id=%s AND seq=%s", victim)
        rows = [dict(zip(cols, r)) for r in c.execute(f"SELECT {', '.join(cols)} FROM security.audit_events").fetchall()]
        v = verify_chain(rows)
    return {"update_blocked_by_trigger": blocked, "tampered_row": {"chain_id": victim[0], "seq": victim[1]},
            "detected": v["broken_links"] > 0, "broken_links": v["broken_links"], "first_broken": v["first_broken"]}


SCENARIOS_FINAL = {
    "S0": ("Steady-state overhead (no fault)", s0_overhead, {}, {}),
    "S8": ("Monitoring and alert trigger", s8_observability, {}, {}),
    "S9": ("Unauthorized or invalid operations", s9_security, {}, {}),
}

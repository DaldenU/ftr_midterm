"""Reliability campaign (accelerated fault-injection life test).

Every application instance is killed at random with the SAME per-instance
failure rate (exponential inter-failure times, mean ``mttf_c`` seconds) in both
system versions. Repair is performed by the supervisor (ft: restart delay) or by
an operator (baseline: ``operator_delay`` s). Black-box probes through the gateway
measure what the USER sees: per-service outages -> failure count, uptime, downtime.

Because baseline has 1 instance per service and ft has 2, the same component
failure process produces very different service-level reliability - which is
the point of the experiment, and what analysis/reliability.py quantifies.
"""
from __future__ import annotations

import datetime as dt
import random
import threading
import time
from pathlib import Path

from ..workload.generator import Client, Recorder, Workload, summarize
from .pg import PgCluster, ROOT
from .probe import Prober
from .stack import Stack

SERVICES = ("student", "payment", "academic")


def run_campaign(mode: str, pg: PgCluster, duration: float = 240.0, mttf_c: float = 15.0, operator_delay: float = 10.0,
                 restart_delay: float = 1.0, rate: float = 20.0, seed: int = 7) -> dict:
    stack = Stack(mode, pg, restart_delay_s=restart_delay, env={"PAYMENT_ROLLBACK_AFTER_S": 4})
    pg.reset_db(stack.db_name, ROOT / f"database/schema_{mode}.sql")
    stack.start()
    time.sleep(0.5)
    rec = Recorder()
    sec = stack.security
    wl = Workload(Client(stack.gateway_url, timeout=5.0, seed=seed, security=sec), rec, seed=seed)
    prober = Prober(stack.gateway_url, headers=sec.auth("S0001") if sec else None)
    prober.start()
    t0 = time.time()
    wl_thread = threading.Thread(target=wl.run_open_loop, args=(duration, rate), daemon=True)
    wl_thread.start()

    # --- pre-computed kill schedule: superposition of independent per-instance Poisson processes
    rng = random.Random(seed)
    inst_names = [n for n, i in stack.instances.items() if i.service in SERVICES]
    schedule = []
    for n in inst_names:
        t = rng.expovariate(1.0 / mttf_c)
        while t < duration - 12:
            schedule.append((t, n))
            t += rng.expovariate(1.0 / mttf_c)
    schedule.sort()
    kills = []          # {"instance","t_kill","t_ready"}
    lock = threading.Lock()

    repairing = set()   # instances an operator is currently repairing (baseline): cannot fail again until repaired

    def operator(name, t_kill):
        try:
            time.sleep(operator_delay)
            stack.restart_instance(name)
        finally:
            repairing.discard(name)

    for t_rel, name in schedule:
        delay = t0 + t_rel - time.time()
        if delay > 0:
            time.sleep(delay)
        inst = stack.instances[name]
        if not inst.alive() or inst.restarting or name in repairing:
            continue                        # a component that is already down cannot fail again
        t_kill = stack.kill_instance(name)
        kills.append({"instance": name, "service": inst.service, "t_kill": t_kill})
        if mode == "baseline":
            repairing.add(name)
            threading.Thread(target=operator, args=(name, t_kill), daemon=True).start()
    wl_thread.join()
    time.sleep(operator_delay + 3 if mode == "baseline" else 4)    # let the last repairs finish
    t_end = time.time()
    prober.stop()
    # repair times from stack events
    for k in kills:
        ready = next((e["ts"] for e in stack.events if e["type"] == "restart_ready" and e["name"] == k["instance"] and e["ts"] >= k["t_kill"]), None)
        k["t_ready"] = ready
        k["repair_s"] = round(ready - k["t_kill"], 3) if ready else None
    # per-service outages as seen by users
    window_end = t0 + duration
    svc = {}
    for s in SERVICES:
        outs = prober.outages(s, t0, window_end, strict=False)
        svc[s] = {"outages": [(round(a - t0, 2), round(b - t0, 2)) for a, b in outs],
                  "downtime_s": round(sum(b - a for a, b in outs), 3),
                  "availability_lenient": round(prober.availability(s, t0, window_end, strict=False), 5),
                  "availability_strict": round(prober.availability(s, t0, window_end, strict=True), 5)}
    summary = summarize(rec.snapshot())
    stack.stop()
    return {"mode": mode, "datetime": dt.datetime.fromtimestamp(t0, dt.timezone.utc).isoformat(timespec="seconds"),
            "duration_s": duration, "mttf_component_injected_s": mttf_c, "operator_delay_s": operator_delay if mode == "baseline" else None,
            "restart_delay_s": restart_delay if mode != "baseline" else None, "instances": len(inst_names), "seed": seed,
            "kills": [{**k, "t_kill_rel": round(k["t_kill"] - t0, 2)} for k in kills],
            "services": svc, "requests": summary}

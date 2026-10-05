#!/usr/bin/env python3
"""Run failure-injection experiments and store machine-readable + human-readable results.

Examples
  python experiments/run_experiments.py --scenarios S1,S2 --modes baseline,ft --reps 1
  python experiments/run_experiments.py --all --reps 3
Outputs (under experiments/results/):
  experiments.csv          one row per run (flat, required assignment fields)
  json/<id>.json           full record incl. per-op stats, consistency, stack events
  raw/<id>.requests.jsonl  every client request with timestamps
  summaries/<id>.md        human-readable summary
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.harness.pg import PgCluster
from experiments.harness.scenarios import Run, SCENARIOS
from experiments.harness.stack import default_workdir

RESULTS = Path(__file__).resolve().parent / "results"
CSV_FIELDS = ["experiment_id", "scenario", "repetition", "datetime", "system_version", "failure_injected", "failure_start",
              "detection_time_s", "detection_monitor_s", "detection_internal_s", "detection_client_s",
              "recovery_start", "recovery_end", "recovery_duration_s", "repair_action_s",
              "total_requests", "successful_requests", "failed_requests", "shed_requests", "hard_failed_requests", "recovered_requests", "degraded_requests",
              "duplicate_requests", "duplicates_prevented", "window_failed", "window_total", "consistent", "consistency_violations"]


def write_record(rec: dict, run: Run, results: Path = RESULTS):
    for d in ("json", "raw", "summaries"):
        (results / d).mkdir(parents=True, exist_ok=True)
    rid = rec["experiment_id"]
    (results / "json" / f"{rid}.json").write_text(json.dumps(rec, indent=2, default=str))
    run.recorder.dump_jsonl(results / "raw" / f"{rid}.requests.jsonl")
    row = {k: rec.get(k) for k in CSV_FIELDS}
    row["consistency_violations"] = json.dumps(rec["consistency"]["violations"])
    csv_path = results / "experiments.csv"
    new = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    (results / "summaries" / f"{rid}.md").write_text(summary_md(rec))


def fmt(x, unit="s"):
    return "n/a" if x is None else f"{x}{unit}"


def summary_md(r: dict) -> str:
    c = r["consistency"]
    lines = [f"# {r['experiment_id']}", "",
             f"* **Scenario:** {r['scenario']} - {SCENARIOS[r['scenario']][0]}",
             f"* **System version:** {r['system_version']}",
             f"* **Date/time (UTC):** {r['datetime']}",
             f"* **Failure injected:** {r['failure_injected']}",
             f"* **Initial state:** `{json.dumps(r['initial_state'])}`",
             "", "| Metric | Value |", "|---|---:|",
             f"| Failure start (t+) | {r['failure_start_rel_s']} s after run start |",
             f"| Detection time (earliest signal) | {fmt(r['detection_time_s'])} |",
             f"| - external monitor | {fmt(r['detection_monitor_s'])} |",
             f"| - system-internal (gateway/breaker) | {fmt(r['detection_internal_s'])} |",
             f"| - first client-visible error | {fmt(r['detection_client_s'])} |",
             f"| Recovery duration (failure -> full service) | {fmt(r['recovery_duration_s'])} |",
             f"| Repair action duration | {fmt(r['repair_action_s'])} |",
             f"| Total / successful / failed requests | {r['total_requests']} / {r['successful_requests']} / {r['failed_requests']} (of which deliberately shed: {r['shed_requests']}) |",
             f"| Failed requests inside failure window | {r['window_failed']} of {r['window_total']} |",
             f"| Recovered (masked) requests | {r['recovered_requests']} |",
             f"| Degraded responses | {r['degraded_requests']} |",
             f"| Duplicate requests sent / replays served | {r['duplicate_requests']} / {r['duplicates_prevented']} |",
             f"| User-visible downtime per service (any non-OK probe) | `{json.dumps(r['user_visible_downtime_s'])}` |",
             f"| Latency p50 / p95 / p99 (successful) | {r['latency']['p50_ms']} / {r['latency']['p95_ms']} / {r['latency']['p99_ms']} ms |",
             f"| **Data consistency** | **{'CONSISTENT' if r['consistent'] else 'VIOLATIONS: ' + json.dumps(c['violations'])}** |",
             "", "## Consistency details", "```json", json.dumps(c, indent=2), "```", "", "## Observations"]
    lines += [f"* {o}" for o in r["observations"]] or ["* (none)"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--modes", default="baseline,ft")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--results", default=str(RESULTS))
    ap.add_argument("--clean", action="store_true", help="delete previous results first")
    a = ap.parse_args()
    results = Path(a.results)
    if a.clean and results.exists():
        import shutil
        for d in ("json", "raw", "summaries"):
            shutil.rmtree(results / d, ignore_errors=True)
        (results / "experiments.csv").unlink(missing_ok=True)
    sids = list(SCENARIOS) if a.all else [s.strip().upper() for s in a.scenarios.split(",") if s.strip()]
    pg = PgCluster(default_workdir())
    pg.start()
    try:
        for rep in range(1, a.reps + 1):
            for sid in sids:
                for mode in a.modes.split(","):
                    name, fn, kw, senv = SCENARIOS[sid]
                    t0 = time.time()
                    print(f"[{time.strftime('%H:%M:%S')}] {sid} {name} / {mode} / rep {rep} ...", flush=True)
                    if not pg.is_up():
                        pg.start()
                    with Run(sid, mode, rep, pg, results, env=senv) as run:
                        rec = fn(run, **kw)
                    write_record(rec, run, results)
                    if sid == "S7":                       # S7 destroys the cluster: start from a clean slate
                        pg.destroy_all()
                    print(f"    done in {time.time() - t0:.0f}s  failed={rec['failed_requests']}/{rec['total_requests']} "
                          f"detect={rec['detection_time_s']} recover={rec['recovery_duration_s']} consistent={rec['consistent']}", flush=True)
    finally:
        pg.stop()


if __name__ == "__main__":
    main()

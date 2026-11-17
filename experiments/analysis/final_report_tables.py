#!/usr/bin/env python3
"""Aggregate the final-project runs (experiments/results_final/json/*.json) into
summary.json (machine-readable, used for the report) and comparison.md (human-readable).

Every number is the mean over repetitions with (min-max); nothing is estimated.
Usage:  python3 experiments/analysis/final_report_tables.py [--results DIR]
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXP = {"S0": "E0", "S1": "E1", "S2": "E2", "S3": "E3", "S5": "E4", "S6": "E5", "S8": "E6", "S9": "E7", "S7": "E8", "S4": "E9"}
TITLE = {"S0": "Steady-state overhead", "S1": "Application instance failure", "S2": "Database unavailable",
         "S3": "Network delay / timeout (gray failure)", "S5": "Interrupted multi-step transaction",
         "S6": "High load / traffic spike", "S8": "Monitoring and alert trigger", "S9": "Unauthorized / invalid operations",
         "S7": "Recovery / failover (primary DB loss)", "S4": "Node failure"}


def agg(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return {"mean": round(statistics.fmean(xs), 4), "min": min(xs), "max": max(xs), "n": len(xs),
            "sd": round(statistics.stdev(xs), 4) if len(xs) > 1 else 0.0}


def get(d, path):
    for p in path.split("."):
        if d is None:
            return None
        d = d.get(p) if isinstance(d, dict) else None
    return d


COMMON = {
    "total_requests": "total_requests", "successful_requests": "successful_requests", "failed_requests": "failed_requests",
    "shed_requests": "shed_requests", "hard_failed_requests": "hard_failed_requests", "recovered_requests": "recovered_requests",
    "degraded_requests": "degraded_requests", "duplicates_prevented": "duplicates_prevented",
    "detection_time_s": "detection_time_s", "detection_internal_s": "detection_internal_s",
    "detection_client_s": "detection_client_s", "detection_by_alert_s": "alerts.detection_by_alert_s",
    "recovery_duration_s": "recovery_duration_s", "repair_action_s": "repair_action_s",
    "window_failed": "window_failed", "window_total": "window_total",
    "p50_ms": "latency.p50_ms", "p95_ms": "latency.p95_ms", "p99_ms": "latency.p99_ms",
    "downtime_student_s": "user_visible_downtime_s.student", "downtime_payment_s": "user_visible_downtime_s.payment",
    "downtime_academic_s": "user_visible_downtime_s.academic",
    "avail_student": "probe_availability_lenient.student", "avail_payment": "probe_availability_lenient.payment",
    "avail_academic": "probe_availability_lenient.academic",
    "dup_payments": "consistency.duplicate_payments", "ledger_mismatch": "consistency.ledger_mismatch_students",
    "stuck_pending": "consistency.stuck_pending_payments", "dup_records": "consistency.duplicate_academic_records",
    "lost_payments": "consistency.lost_acknowledged_payments", "lost_grades": "consistency.lost_acknowledged_grades",
    "partial_batches": "consistency.partial_batches", "batches_dup": "consistency.batches_with_duplicates",
    "rolled_back": "consistency.rolled_back_payments", "attack_rows": "consistency.attack_rows",
    "audit_events": "consistency.audit_events", "audit_broken": "consistency.audit_broken_links",
}


def per_scenario_extra(sid, r):
    e = {}
    if sid == "S0":
        for k in ("p50_ms", "p95_ms", "p99_ms"):
            e["m_" + k] = r["measured"][k]
        e["cpu_ms_per_request"] = r["cpu_ms_per_request"]
        e["rss_mb_total"] = r["rss_mb_total"]
        e["cpu_seconds_total"] = r["cpu_seconds_total"]
    if sid == "S3":
        for op in ("post_payment", "get_transcript", "get_student"):
            po = r["per_op"].get(op, {})
            e[f"{op}_failed"] = po.get("failed")
            e[f"{op}_p95_ms"] = po.get("p95_all_ms")
        e["breaker_closed_after_clear_s"] = r.get("breaker_closed_after_clear_s")
    if sid == "S5":
        for k in ("logical_payments_acknowledged", "logical_batches_acknowledged", "mean_client_recovery_s",
                  "p95_client_recovery_s", "interrupted_payments", "abandoned_payments", "double_submitted_payments",
                  "interrupted_batches"):
            e[k] = r.get(k)
        e["duplicate_requests"] = r.get("duplicate_requests")
        e["ledger_mismatch_amount"] = r["consistency"].get("ledger_mismatch_amount")
    if sid == "S6":
        for ph, s in r["per_phase"].items():
            e[f"{ph}_goodput_rps"] = s["throughput_rps"]
            e[f"{ph}_p95_ms"] = s["p95_ms"]          # successful (accepted) requests
            e[f"{ph}_p95_all_ms"] = s["p95_all_ms"]
            e[f"{ph}_hard_error_pct"] = round(100 * s["hard_error_rate"], 3)
            e[f"{ph}_shed_pct"] = round(100 * s["shed"] / s["total"], 3) if s["total"] else 0
        e["slo_breach_after_s"] = r.get("slo_breach_after_s")
        e["recovery_after_peak_s"] = r.get("recovery_after_peak_s")
    if sid == "S7":
        e["rpo_lost_acknowledged_writes"] = r.get("rpo_lost_acknowledged_writes")
        e["db_available_again_s"] = r.get("db_available_again_s")
    if sid == "S8":
        for pf in r["per_fault"]:
            f = pf["fault"]
            e[f"{f}_detection_s"] = pf["detection_s"]
            e[f"{f}_resolved_after_clear_s"] = pf["resolved_after_clear_s"]
            e[f"{f}_first_client_error_s"] = pf["first_client_error_s"]
            e[f"{f}_detected"] = 1 if pf["detection_s"] is not None else 0
        e["detected_faults"] = r["detected_faults"]
        e["false_alerts_warmup"] = len(r["false_alerts_during_warmup"])
        d = r["diagnosability"]
        e["incident_requests"] = d["incident_requests"]
        e["found_ratio"] = d["found_ratio"]
        e["localized_ratio"] = d["localized_ratio"]
        e["multi_hop_ratio"] = d["multi_hop_ratio"]
    if sid == "S9":
        e["attack_total"] = r["attack_total"]
        e["attacks_blocked"] = r["attacks_blocked"]
        e["attacks_harmful_success"] = r["attacks_harmful_success"]
        e["attacks_server_error"] = r["attacks_server_error"]
        e["block_rate"] = r["attacks_blocked"] / r["attack_total"]
        for cat, s in r["attack_summary"].items():
            e[f"{cat}_blocked"] = s["blocked"]
            e[f"{cat}_attempts"] = s["attempts"]
        e["flood_throttled_ratio"] = r["flood"]["throttled_ratio"]
        e["flood_sent"] = r["flood"]["sent"]
        e["bruteforce_429"] = sum(1 for c in r["bruteforce_statuses"] if c == 429)
        e["bruteforce_401"] = sum(1 for c in r["bruteforce_statuses"] if c == 401)
        for ph in ("legit_before", "legit_during_battery", "legit_during_flood", "legit_after"):
            e[f"{ph}_success"] = r[ph]["success_ratio"]
            e[f"{ph}_p95_ms"] = r[ph]["p95_ms"]
        e["security_alert_s"] = get(r, "alerts.first_firing_after_fault_s.SecurityDenialSpike")
        e["audit_deny"] = r["consistency"].get("audit_deny_events")
        t = r.get("audit_tamper_test")
        if t:
            e["tamper_detected"] = 1 if t["detected"] else 0
            e["tamper_blocked_by_trigger"] = 1 if t["update_blocked_by_trigger"] else 0
        e["admin_backdoor_status"] = r["admin_backdoor_status"]
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "experiments" / "results_final"))
    a = ap.parse_args()
    res = Path(a.results)
    runs = defaultdict(list)
    for f in sorted(glob.glob(str(res / "json" / "*.json"))):
        r = json.loads(Path(f).read_text())
        runs[(r["scenario"], r["mode"])].append(r)
    summary = {}
    for (sid, mode), rs in sorted(runs.items()):
        row = {k: agg([get(r, p) for r in rs]) for k, p in COMMON.items()}
        row["success_ratio"] = agg([r["successful_requests"] / r["total_requests"] for r in rs if r["total_requests"]])
        row["window_error_rate"] = agg([r["window_failed"] / r["window_total"] for r in rs if r.get("window_total")])
        row["consistent_runs"] = sum(1 for r in rs if r["consistent"])
        row["runs"] = len(rs)
        row["violations"] = [r["consistency"]["violations"] for r in rs]
        extras = defaultdict(list)
        for r in rs:
            for k, v in per_scenario_extra(sid, r).items():
                extras[k].append(v)
        for k, v in extras.items():
            row[k] = agg(v) if all(isinstance(x, (int, float)) or x is None for x in v) else v
        alerts = defaultdict(int)
        for r in rs:
            for n in (get(r, "alerts.fired_after_fault") or []):
                alerts[n] += 1
        row["alerts_fired_runs"] = dict(alerts)
        row["false_alerts_before_fault"] = [get(r, "alerts.false_alerts_before_fault") for r in rs]
        row["experiment_ids"] = [r["experiment_id"] for r in rs]
        summary.setdefault(sid, {})[mode] = row
    out = res / "summary.json"
    out.write_text(json.dumps(summary, indent=1, default=str))

    def m(row, k, d=1, pct=False):
        v = row.get(k)
        if not isinstance(v, dict):
            return "n/a"
        f = (lambda x: f"{100 * x:.{d}f} %") if pct else (lambda x: f"{x:.{d}f}")
        return f"{f(v['mean'])} ({f(v['min'])}-{f(v['max'])})" if v["n"] > 1 and v["min"] != v["max"] else f(v["mean"])

    L = ["# Final project - consolidated results (generated)", "",
         f"Source: `{res.relative_to(ROOT) if res.is_relative_to(ROOT) else res}/json/*.json`; mean (min-max) over repetitions.", "",
         "| Exp | Scenario | Mode | Runs | Failed / total | Success | Detect (s) | Alert (s) | Recovery (s) | p95 (ms) | Consistent |",
         "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    order = ["S1", "S2", "S3", "S5", "S6", "S8", "S9", "S7", "S4", "S0"]
    for sid in order:
        for mode in ("baseline", "ft", "final"):
            row = summary.get(sid, {}).get(mode)
            if not row:
                continue
            L.append(f"| {EXP[sid]} | {TITLE[sid]} | {mode} | {row['runs']} | {m(row, 'failed_requests', 0)} / {m(row, 'total_requests', 0)} | "
                     f"{m(row, 'success_ratio', 2, True)} | {m(row, 'detection_time_s', 2)} | {m(row, 'detection_by_alert_s', 2)} | "
                     f"{m(row, 'recovery_duration_s', 1)} | {m(row, 'p95_ms', 0)} | {row['consistent_runs']}/{row['runs']} |")
    (res / "comparison.md").write_text("\n".join(L) + "\n")
    print(f"wrote {out} and {res / 'comparison.md'} ({sum(len(v) for v in runs.values())} runs)")


if __name__ == "__main__":
    main()

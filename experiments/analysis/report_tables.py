#!/usr/bin/env python3
"""Aggregate experiment records into comparison tables (Markdown) and charts (PNG).

Input : <results>/json/*.json   (written by run_experiments.py)  [+ campaign_*.json / reliability.json]
Output: <results>/comparison.md, <results>/charts/*.png
Every number comes from the measured records; with several repetitions the tables show  mean (min-max).
"""
from __future__ import annotations

import glob
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

NAMES = {"S1": "Application crash", "S2": "Database failure (restart)", "S3": "Network/service timeout (gray failure)",
         "S4": "Node/hardware failure", "S5": "Corrupted/lost transaction", "S6": "High load",
         "S7": "Primary database loss (replication vs backup)"}
BASE, FT = "baseline", "ft"


def load(results: Path):
    runs = defaultdict(list)
    for f in sorted(glob.glob(str(results / "json" / "*.json"))):
        r = json.load(open(f))
        runs[(r["scenario"], r["mode"])].append(r)
    return runs


def vals(rs, getter):
    out = []
    for r in rs:
        try:
            v = getter(r)
        except (KeyError, TypeError):
            v = None
        if v is not None:
            out.append(v)
    return out


def mean(xs):
    return st.mean(xs) if xs else None


def fmt(xs, d=1, unit=""):
    """mean (min-max) over repetitions; 'n/a' if no sample."""
    if not xs:
        return "n/a"
    m = mean(xs)
    s = f"{m:.{d}f}{unit}"
    if len(xs) > 1 and (max(xs) - min(xs)) > 10 ** -(d + 1):
        s += f" ({min(xs):.{d}f}-{max(xs):.{d}f})"
    return s


def improvement(b, f, lower_is_better=True):
    if b is None or f is None:
        return "n/a"
    if b == f == 0:
        return "no difference (both 0)"
    if b == 0:
        return "n/a (baseline 0)" if lower_is_better else "n/a"
    ch = (b - f) / b * 100 if lower_is_better else (f - b) / b * 100
    return f"{ch:+.0f} %".replace("+-", "-")


def row(label, runs, sid, getter, d=1, unit="", lower=True):
    b = vals(runs.get((sid, BASE), []), getter)
    f = vals(runs.get((sid, FT), []), getter)
    return f"| {label} | {fmt(b, d, unit)} | {fmt(f, d, unit)} | {improvement(mean(b), mean(f), lower)} |"


def op_failed(op_names):
    return lambda r: sum(r["per_op"][o]["failed"] for o in op_names if o in r["per_op"])


def build_markdown(runs, results: Path) -> str:
    L = ["# Baseline vs fault-tolerant: measured comparison", ""]
    reps = {k: len(v) for k, v in runs.items()}
    L.append("Repetitions per cell: " + ", ".join(f"{s}/{m}: {n}" for (s, m), n in sorted(reps.items())) + ". "
             "Cells show mean (min-max) over repetitions. Improvement = relative change of the mean "
             "(negative = fault-tolerant version worse). Hardware: 2 vCPU shared by all processes (see report, limitations).")
    L.append("")
    L += ["## 1. Headline comparison", "", "| Experiment | Metric | Baseline | Fault-tolerant | Improvement |", "|---|---|---:|---:|---:|"]

    def hl(sid, label, getter, d=1, unit="", lower=True):
        r = row(label, runs, sid, getter, d, unit, lower)
        L.append(f"| {sid} {NAMES[sid]} " + r)

    hl("S1", "failed requests", lambda r: r["failed_requests"], 0)
    hl("S1", "user-visible downtime of payment service (s)", lambda r: r["user_visible_downtime_s"]["payment"], 1)
    hl("S2", "failed requests", lambda r: r["failed_requests"], 0)
    hl("S2", "requests served degraded/recovered instead of failing", lambda r: r["degraded_requests"] + r["recovered_requests"], 0, lower=False)
    hl("S3", "failed payment + transcript requests (dependants of the slow service)", op_failed(["post_payment", "get_transcript"]), 0)
    hl("S3", "p95 latency of payments (ms)", lambda r: r["per_op"]["post_payment"]["p95_ms"], 0, " ms")
    hl("S4", "failed requests", lambda r: r["failed_requests"], 0)
    hl("S4", "user-visible downtime, any service (s)", lambda r: max(r["user_visible_downtime_s"].values()), 1)
    hl("S5", "duplicate payments", lambda r: r["consistency"]["duplicate_payments"], 0)
    hl("S5", "ledger mismatches (students)", lambda r: r["consistency"]["ledger_mismatch_students"], 0)
    hl("S5", "duplicate academic records", lambda r: r["consistency"]["duplicate_academic_records"], 0)
    hl("S6", "p95 latency at peak load, 150 clients (ms)", lambda r: r["per_phase"]["c150"]["p95_all_ms"], 0, " ms")
    hl("S6", "goodput at peak load (successful req/s)", lambda r: r["per_phase"]["c150"]["throughput_rps"], 0, lower=False)
    hl("S6", "hard (non-shed) error rate at peak", lambda r: r["per_phase"]["c150"]["hard_error_rate"] * 100, 2, " %")
    hl("S7", "failed requests", lambda r: r["failed_requests"], 0)
    hl("S7", "acknowledged writes lost (RPO)", lambda r: r.get("rpo_lost_acknowledged_writes"), 0)
    L.append("")

    L += ["## 2. Experiment table (assignment format)", "",
          "| Experiment | System | Failure | Detection time | Recovery time | Failed requests | Recovered | Consistent |",
          "|---|---|---|---:|---:|---:|---:|:--:|"]
    for sid in sorted(NAMES):
        for mode, lab in ((BASE, "baseline"), (FT, "fault-tolerant")):
            rs = runs.get((sid, mode), [])
            if not rs:
                continue
            L.append(f"| {sid} {NAMES[sid]} | {lab} | {rs[0]['failure_injected'][:70]} | "
                     f"{fmt(vals(rs, lambda r: r['detection_time_s']), 2, ' s')} | {fmt(vals(rs, lambda r: r['recovery_duration_s']), 1, ' s')} | "
                     f"{fmt(vals(rs, lambda r: r['failed_requests']), 0)} / {fmt(vals(rs, lambda r: r['total_requests']), 0)} | "
                     f"{fmt(vals(rs, lambda r: r['recovered_requests']), 0)} | "
                     f"{sum(1 for r in rs if r['consistent'])}/{len(rs)} |")
    L.append("")

    # ------------------------------------------------------------------ per scenario detail
    L += ["## 3. Per-scenario detail", ""]
    for sid in sorted(NAMES):
        if not (runs.get((sid, BASE)) or runs.get((sid, FT))):
            continue
        L += [f"### {sid} - {NAMES[sid]}", "", "| Metric | Baseline | Fault-tolerant | Improvement |", "|---|---:|---:|---:|"]
        spec = [("Detection time, earliest signal (s)", lambda r: r["detection_time_s"], 2, True),
                ("  - external /ready monitor (s)", lambda r: r["detection_monitor_s"], 2, True),
                ("  - system-internal: gateway/breaker (s)", lambda r: r["detection_internal_s"], 2, True),
                ("  - first client-visible error (s)", lambda r: r["detection_client_s"], 2, True),
                ("Recovery duration, failure -> full service (s)", lambda r: r["recovery_duration_s"], 1, True),
                ("Repair action duration (s)", lambda r: r["repair_action_s"], 1, True),
                ("Total requests", lambda r: r["total_requests"], 0, None),
                ("Successful requests", lambda r: r["successful_requests"], 0, False),
                ("Failed requests", lambda r: r["failed_requests"], 0, True),
                ("  - deliberately shed (503 + Retry-After)", lambda r: r["shed_requests"], 0, None),
                ("Failed requests inside failure window", lambda r: r["window_failed"], 0, True),
                ("Recovered requests (masked by retry/failover/client retry)", lambda r: r["recovered_requests"], 0, False),
                ("Degraded responses (reduced functionality)", lambda r: r["degraded_requests"], 0, None),
                ("Duplicate requests sent", lambda r: r["duplicate_requests"], 0, None),
                ("Duplicates answered from stored result", lambda r: r["duplicates_prevented"], 0, None),
                ("Latency p50 (ms)", lambda r: r["latency"]["p50_ms"], 0, True),
                ("Latency p95 (ms)", lambda r: r["latency"]["p95_ms"], 0, True),
                ("Latency p99 (ms)", lambda r: r["latency"]["p99_ms"], 0, True)]
        for label, g, d, low in spec:
            b = vals(runs.get((sid, BASE), []), g)
            f = vals(runs.get((sid, FT), []), g)
            L.append(f"| {label} | {fmt(b, d)} | {fmt(f, d)} | {improvement(mean(b), mean(f), low) if low is not None else ''} |")
        for svc in ("student", "payment", "academic"):
            b = vals(runs.get((sid, BASE), []), lambda r: r["user_visible_downtime_s"][svc])
            f = vals(runs.get((sid, FT), []), lambda r: r["user_visible_downtime_s"][svc])
            L.append(f"| User-visible downtime of {svc} (any non-OK probe, s) | {fmt(b, 1)} | {fmt(f, 1)} | {improvement(mean(b), mean(f))} |")
        for svc in ("student", "payment", "academic"):
            b = vals(runs.get((sid, BASE), []), lambda r: r["probe_availability_lenient"][svc] * 100)
            f = vals(runs.get((sid, FT), []), lambda r: r["probe_availability_lenient"][svc] * 100)
            L.append(f"| Probe availability of {svc} after failure start (%) | {fmt(b, 2)} | {fmt(f, 2)} | |")
        L.append("")
        # per-op failures
        ops = sorted({o for m in (BASE, FT) for r in runs.get((sid, m), []) for o in r["per_op"]})
        L += ["Per-operation failures (count) and p95 latency (ms):", "", "| Operation | Baseline failed | FT failed | Baseline p95 | FT p95 | FT degraded | FT recovered |", "|---|---:|---:|---:|---:|---:|---:|"]
        for o in ops:
            g = lambda k: (lambda r: r["per_op"][o][k])
            L.append(f"| {o} | {fmt(vals(runs.get((sid, BASE), []), g('failed')), 0)} | {fmt(vals(runs.get((sid, FT), []), g('failed')), 0)} | "
                     f"{fmt(vals(runs.get((sid, BASE), []), g('p95_ms')), 0)} | {fmt(vals(runs.get((sid, FT), []), g('p95_ms')), 0)} | "
                     f"{fmt(vals(runs.get((sid, FT), []), g('degraded')), 0)} | {fmt(vals(runs.get((sid, FT), []), g('recovered')), 0)} |")
        L.append("")
        # consistency
        keys = ["duplicate_payments", "ledger_mismatch_students", "ledger_mismatch_amount", "stuck_pending_payments", "rolled_back_payments",
                "duplicate_academic_records", "lost_acknowledged_payments", "lost_acknowledged_grades", "partial_batches", "batches_with_duplicates"]
        L += ["Data-consistency invariants checked directly in PostgreSQL after recovery:", "", "| Invariant violation | Baseline | Fault-tolerant |", "|---|---:|---:|"]
        for k in keys:
            b = vals(runs.get((sid, BASE), []), lambda r: r["consistency"][k])
            f = vals(runs.get((sid, FT), []), lambda r: r["consistency"][k])
            L.append(f"| {k} | {fmt(b, 0)} | {fmt(f, 0)} |")
        L.append(f"| **consistent (runs passing all invariants)** | {sum(1 for r in runs.get((sid, BASE), []) if r['consistent'])}/{len(runs.get((sid, BASE), []))} | "
                 f"{sum(1 for r in runs.get((sid, FT), []) if r['consistent'])}/{len(runs.get((sid, FT), []))} |")
        L.append("")
        if sid == "S5":
            L += ["Transaction-level outcome:", "", "| Metric | Baseline | Fault-tolerant |", "|---|---:|---:|"]
            for lab, k in (("logical payments planned", "logical_payments_planned"), ("logical payments acknowledged", "logical_payments_acknowledged"),
                           ("interrupted payments", "interrupted_payments"), ("abandoned payments (never retried)", "abandoned_payments"),
                           ("double-submitted payments", "double_submitted_payments"), ("interrupted batches", "interrupted_batches"),
                           ("mean client-side recovery time (s)", "mean_client_recovery_s"), ("p95 client-side recovery time (s)", "p95_client_recovery_s")):
                b = vals(runs.get((sid, BASE), []), lambda r: r[k]); f = vals(runs.get((sid, FT), []), lambda r: r[k])
                L.append(f"| {lab} | {fmt(b, 2 if 'time' in lab else 0)} | {fmt(f, 2 if 'time' in lab else 0)} |")
            L.append("")
        if sid == "S6":
            L += ["Per load step (closed loop, N concurrent clients):", "",
                  "| Clients | Baseline goodput (req/s) | FT goodput | Baseline p50 / p95 / p99 (ms) | FT p50 / p95 / p99 (ms) | Baseline error % | FT hard error % | FT shed % |", "|---:|---:|---:|---|---|---:|---:|---:|"]
            for ph in ("c5", "c25", "c75", "c150", "cooldown"):
                def g(m, k):
                    return fmt(vals(runs.get((sid, m), []), lambda r: r["per_phase"][ph][k]), 0)
                def e(m, k):
                    return fmt(vals(runs.get((sid, m), []), lambda r: r["per_phase"][ph][k] * 100), 1)
                shed = fmt(vals(runs.get((sid, FT), []), lambda r: r["per_phase"][ph]["shed"] / max(r["per_phase"][ph]["total"], 1) * 100), 1)
                L.append(f"| {ph} | {g(BASE, 'throughput_rps')} | {g(FT, 'throughput_rps')} | {g(BASE, 'p50_ms')} / {g(BASE, 'p95_ms')} / {g(BASE, 'p99_ms')} | "
                         f"{g(FT, 'p50_ms')} / {g(FT, 'p95_ms')} / {g(FT, 'p99_ms')} | {e(BASE, 'error_rate')} | {e(FT, 'hard_error_rate')} | {shed} |")
            L.append("")
        if sid == "S7":
            L += ["| Metric | Baseline (restore from backup) | Fault-tolerant (streaming standby + failover) |", "|---|---:|---:|"]
            for lab, k, d in (("DB available again after (s)", "db_available_again_s", 1), ("acknowledged writes lost (RPO)", "rpo_lost_acknowledged_writes", 0)):
                b = vals(runs.get((sid, BASE), []), lambda r: r[k]); f = vals(runs.get((sid, FT), []), lambda r: r[k])
                L.append(f"| {lab} | {fmt(b, d)} | {fmt(f, d)} |")
            L.append("")
        obs = sorted({o for m in (BASE, FT) for r in runs.get((sid, m), []) for o in r["observations"]})
        L += ["Observations (generated by the experiment, not edited):", ""] + [f"* {o}" for o in obs] + [""]
    return "\n".join(L)


# --------------------------------------------------------------------------- charts
def charts(runs, results: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out = results / "charts"
    out.mkdir(exist_ok=True)
    C = {BASE: "#c0504d", FT: "#1f6f8b"}
    sids = [s for s in sorted(NAMES) if runs.get((s, BASE)) and runs.get((s, FT))]

    def bars(fname, title, ylabel, getter, sel=sids, log=False):
        fig, ax = plt.subplots(figsize=(8, 4))
        w = 0.38
        for i, m in enumerate((BASE, FT)):
            ys = [mean(vals(runs[(s, m)], getter(s))) or 0 for s in sel]
            err = [[(mean(vals(runs[(s, m)], getter(s))) or 0) - min(vals(runs[(s, m)], getter(s)) or [0]) for s in sel],
                   [max(vals(runs[(s, m)], getter(s)) or [0]) - (mean(vals(runs[(s, m)], getter(s))) or 0) for s in sel]]
            ax.bar([x + (i - 0.5) * w for x in range(len(sel))], ys, w, yerr=err, color=C[m], label="baseline" if m == BASE else "fault-tolerant", capsize=2)
        ax.set_xticks(range(len(sel)))
        ax.set_xticklabels(sel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        if log:
            ax.set_yscale("symlog")
        ax.legend(frameon=False)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(out / fname, dpi=140)
        plt.close(fig)

    bars("failed_requests.png", "Failed requests per experiment (mean, min-max over repetitions)", "failed requests (symlog)", lambda s: (lambda r: r["failed_requests"]), log=True)
    bars("recovery_time.png", "Time to recover: failure start -> full service", "seconds", lambda s: (lambda r: r["recovery_duration_s"]), sel=[s for s in sids if s not in ("S5", "S6")])
    bars("detection_time.png", "Detection time (earliest signal)", "seconds", lambda s: (lambda r: r["detection_time_s"]), sel=[s for s in sids if s not in ("S5", "S6")])

    if runs.get(("S6", BASE)) and runs.get(("S6", FT)):
        phases = ["c5", "c25", "c75", "c150"]
        fig, axs = plt.subplots(1, 3, figsize=(12, 3.8))
        for m in (BASE, FT):
            lab = "baseline" if m == BASE else "fault-tolerant"
            axs[0].plot([5, 25, 75, 150], [mean(vals(runs[("S6", m)], lambda r: r["per_phase"][p]["throughput_rps"])) for p in phases], "o-", color=C[m], label=lab)
            axs[1].plot([5, 25, 75, 150], [mean(vals(runs[("S6", m)], lambda r: r["per_phase"][p]["p95_all_ms"])) for p in phases], "o-", color=C[m], label=lab)
            axs[2].plot([5, 25, 75, 150], [mean(vals(runs[("S6", m)], lambda r: r["per_phase"][p]["error_rate"] * 100)) for p in phases], "o-", color=C[m], label=lab)
        for ax, t, y in zip(axs, ("Goodput", "p95 latency (all responses)", "Error / shed rate"), ("successful req/s", "ms", "% of requests")):
            ax.set_title(t); ax.set_xlabel("concurrent clients"); ax.set_ylabel(y); ax.spines[["top", "right"]].set_visible(False)
        axs[1].set_yscale("log")
        axs[0].legend(frameon=False)
        fig.tight_layout(); fig.savefig(out / "high_load.png", dpi=140); plt.close(fig)

    # per-second success/failure timelines from the raw request logs (first repetition)
    for sid in ("S1", "S2", "S4", "S7"):
        files = {}
        for m in (BASE, FT):
            rs = runs.get((sid, m), [])
            if rs:
                files[m] = rs[0]
        if len(files) < 2:
            continue
        fig, axs = plt.subplots(1, 2, figsize=(11, 3.4), sharey=True)
        for ax, m in zip(axs, (BASE, FT)):
            r = files[m]
            raw = results / "raw" / f"{r['experiment_id']}.requests.jsonl"
            if not raw.exists():
                continue
            t0 = r["failure_start"] - r["failure_start_rel_s"]
            ok, bad = defaultdict(int), defaultdict(int)
            for line in open(raw):
                q = json.loads(line)
                sec = int(q["t1"] - t0)
                (ok if 200 <= q["status"] < 300 else bad)[sec] += 1
            xs = range(0, max(list(ok) + list(bad) + [1]) + 1)
            ax.bar(xs, [ok[x] for x in xs], color="#6aa84f", label="ok")
            ax.bar(xs, [bad[x] for x in xs], bottom=[ok[x] for x in xs], color="#cc0000", label="failed")
            ax.axvline(r["failure_start_rel_s"], color="k", ls="--", lw=1)
            ax.set_title(f"{sid} {'baseline' if m == BASE else 'fault-tolerant'}")
            ax.set_xlabel("seconds since start")
            ax.spines[["top", "right"]].set_visible(False)
        axs[0].set_ylabel("requests / s"); axs[0].legend(frameon=False)
        fig.tight_layout(); fig.savefig(out / f"timeline_{sid}.png", dpi=140); plt.close(fig)

    rel = results / "reliability.json"
    if rel.exists():
        d = json.load(open(rel))
        fig, ax = plt.subplots(figsize=(7, 3.8))
        svcs = ["student", "payment", "academic"]
        for i, m in enumerate((BASE, FT)):
            ax.bar([x + (i - 0.5) * 0.38 for x in range(3)], [d[m]["services"][s]["availability_observed"] * 100 for s in svcs], 0.38, color=C[m], label="baseline" if m == BASE else "fault-tolerant")
        ax.set_xticks(range(3)); ax.set_xticklabels(svcs); ax.set_ylabel("availability (%)")
        ax.set_title("Reliability campaign: user-visible availability under random instance kills")
        ax.legend(frameon=False, loc="lower right"); ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout(); fig.savefig(out / "campaign_availability.png", dpi=140); plt.close(fig)


def main(results: Path):
    runs = load(results)
    if not runs:
        sys.exit("no results found in " + str(results / "json"))
    (results / "comparison.md").write_text(build_markdown(runs, results))
    charts(runs, results)
    print("wrote", results / "comparison.md", "and charts")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "results")

"""Reliability-engineering calculations (MTTF, MTBF, MTTR, availability, failure rate).

Definitions (repairable system, observation window T)
  n          number of failures observed in T
  U          total up time,  D  total down time,  U + D = T
  MTTF       mean time to failure   = U / n            (mean UP time per failure)
  MTTR       mean time to recover   = D / n_repairs    (mean DOWN time per failure)
  MTBF       mean time between failures = MTTF + MTTR = T / n
  Availability (steady state, theory)  A = MTTF / (MTTF + MTTR) = MTBF-based form  MTBF_up/(MTBF)
             (the often-quoted  A = MTBF / (MTBF + MTTR)  is only equal to this if "MTBF" there means
              the mean UP time, i.e. MTTF; with MTBF = MTTF + MTTR that expression would double-count
              the repair time. We therefore use MTTF/(MTTF+MTTR) and state the MTBF explicitly.)
  Availability (measured)  A_obs = U / T
  Failure rate  lambda = n / U   [1/s]  (failures per unit of UP time; for exponential lifetimes 1/MTTF)
  Reliability   R(t) = exp(-lambda t)   (only valid if lifetimes are exponential - the injected
                                         failures are, because inter-kill times are drawn from Exp)
  Redundancy
     parallel (any-of-n)  A = 1 - (1 - A_c)^n     assumes independent failures and instantaneous,
                                                  perfect failover (optimistic)
     series  (all-of-n)   A = prod(A_i)
  Zero failures observed: MTTF is only bounded below (MTTF >= U, right-censored) and the 95 % upper
  confidence bound of the failure rate is 3/U ("rule of three").
"""
from __future__ import annotations

import json
import math
from pathlib import Path

SERVICES = ("student", "payment", "academic")


# --------------------------------------------------------------------------- formulas
def mttf(uptime: float, n_failures: int):
    return uptime / n_failures if n_failures else None


def mttr(downtime: float, n_repairs: int):
    return downtime / n_repairs if n_repairs else None


def mtbf(observation: float, n_failures: int):
    return observation / n_failures if n_failures else None


def availability_theory(mttf_: float, mttr_: float) -> float:
    return mttf_ / (mttf_ + mttr_)


def availability_observed(uptime: float, total: float) -> float:
    return uptime / total


def failure_rate(n_failures: int, uptime: float):
    return n_failures / uptime if uptime > 0 else None


def reliability(rate: float, t: float) -> float:
    return math.exp(-rate * t)


def parallel(a: float, n: int) -> float:
    return 1.0 - (1.0 - a) ** n


def series(avails) -> float:
    p = 1.0
    for a in avails:
        p *= a
    return p


def rule_of_three(uptime: float):
    return 3.0 / uptime if uptime > 0 else None


def nines(a: float) -> float:
    return float("inf") if a >= 1 else -math.log10(1 - a)


# --------------------------------------------------------------------------- campaign analysis
def analyse_campaign(c: dict) -> dict:
    T = c["duration_s"]
    n_inst = c["instances"]
    kills = c["kills"]
    repairs = [k["repair_s"] for k in kills if k.get("repair_s") is not None]
    n_k = len(kills)
    inst_down = sum(repairs)
    inst_up = n_inst * T - inst_down
    comp = {"instances": n_inst, "failures_injected": n_k,
            "MTTF_component_s": mttf(inst_up, n_k), "MTTR_component_s": mttr(inst_down, len(repairs)),
            "failure_rate_component_per_s": failure_rate(n_k, inst_up),
            "injected_mttf_s": c["mttf_component_injected_s"]}
    A_c = availability_theory(comp["MTTF_component_s"], comp["MTTR_component_s"]) if n_k and repairs else None
    comp["availability_component_theory"] = A_c
    replicas = 1 if c["mode"] == "baseline" else 2
    out = {"mode": c["mode"], "observation_s": T, "component": comp, "services": {}}
    all_outages = []
    for s in SERVICES:
        sv = c["services"][s]
        outs = [tuple(o) for o in sv["outages"]]
        all_outages += outs
        n = len(outs)
        D = sum(b - a for a, b in outs)
        U = T - D
        row = {"replicas": replicas, "failures": n, "downtime_s": round(D, 3), "uptime_s": round(U, 3),
               "MTTF_s": mttf(U, n), "MTTR_s": mttr(D, n), "MTBF_s": mtbf(T, n),
               "failure_rate_per_s": failure_rate(n, U),
               "availability_observed": availability_observed(U, T),
               "availability_theory_ideal_redundancy": parallel(A_c, replicas) if A_c is not None else None}
        if n == 0:
            row["MTTF_note"] = f"no failure observed: MTTF >= {round(U,1)} s (censored); failure rate < {rule_of_three(U):.4f}/s (95 %)"
        out["services"][s] = row
    # any-service-down (system) view: union of the per-service outages
    merged = []
    for a, b in sorted(all_outages):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    D = sum(b - a for a, b in merged)
    n = len(merged)
    out["system_any_service_down"] = {"failures": n, "downtime_s": round(D, 3), "MTTF_s": mttf(T - D, n), "MTTR_s": mttr(D, n),
                                      "MTBF_s": mtbf(T, n), "availability_observed": (T - D) / T}
    if A_c is not None:
        out["system_theory_series_of_3_services"] = series([parallel(A_c, replicas)] * 3)
    out["requests"] = c.get("requests")
    return out


def pct(x):
    return "n/a" if x is None else f"{100 * x:.2f} %"


def fnum(x, d=1):
    return "n/a" if x is None else f"{x:.{d}f}"


def campaign_markdown(res_b: dict, res_f: dict) -> str:
    L = ["# Reliability campaign results", "",
         f"Observation window T = {res_b['observation_s']:.0f} s per system version; every application instance is killed with "
         f"exponential inter-failure times (injected mean {res_b['component']['injected_mttf_s']} s per instance, accelerated life test). "
         "Failures/outages below are what a user sees through the gateway (>= 2 consecutive failed 10 Hz probes).", "",
         "## Component level (application instances)", "",
         "| Metric | Baseline (1 replica/service) | Fault-tolerant (2 replicas/service) |", "|---|---:|---:|",
         f"| Instances | {res_b['component']['instances']} | {res_f['component']['instances']} |",
         f"| Failures injected | {res_b['component']['failures_injected']} | {res_f['component']['failures_injected']} |",
         f"| MTTF_c (observed, s) | {fnum(res_b['component']['MTTF_component_s'])} | {fnum(res_f['component']['MTTF_component_s'])} |",
         f"| MTTR_c (observed kill->ready, s) | {fnum(res_b['component']['MTTR_component_s'],2)} | {fnum(res_f['component']['MTTR_component_s'],2)} |",
         f"| Failure rate lambda_c (1/s) | {fnum(res_b['component']['failure_rate_component_per_s'],4)} | {fnum(res_f['component']['failure_rate_component_per_s'],4)} |",
         f"| A_c = MTTF/(MTTF+MTTR) | {pct(res_b['component']['availability_component_theory'])} | {pct(res_f['component']['availability_component_theory'])} |",
         "", "## Service level (user-visible, per service)", ""]
    for s in SERVICES:
        b, f = res_b["services"][s], res_f["services"][s]
        L += [f"### {s}", "", "| Metric | Baseline | Fault-tolerant |", "|---|---:|---:|",
              f"| Outages (failures) | {b['failures']} | {f['failures']} |",
              f"| Downtime (s) | {b['downtime_s']} | {f['downtime_s']} |",
              f"| MTTF (s) | {fnum(b['MTTF_s'])} | {fnum(f['MTTF_s']) if f['MTTF_s'] else '>= ' + fnum(f['uptime_s']) + ' (censored)'} |",
              f"| MTTR (s) | {fnum(b['MTTR_s'],2)} | {fnum(f['MTTR_s'],2)} |",
              f"| MTBF = T/n (s) | {fnum(b['MTBF_s'])} | {fnum(f['MTBF_s'])} |",
              f"| Failure rate (1/s of uptime) | {fnum(b['failure_rate_per_s'],4)} | {fnum(f['failure_rate_per_s'],4)} |",
              f"| Availability observed | {pct(b['availability_observed'])} | {pct(f['availability_observed'])} |",
              f"| Availability theory (A_c, ideal redundancy) | {pct(b['availability_theory_ideal_redundancy'])} | {pct(f['availability_theory_ideal_redundancy'])} |", ""]
    L += ["## Whole system (any service down)", "", "| Metric | Baseline | Fault-tolerant |", "|---|---:|---:|"]
    b, f = res_b["system_any_service_down"], res_f["system_any_service_down"]
    L += [f"| Failures | {b['failures']} | {f['failures']} |", f"| Downtime (s) | {fnum(b['downtime_s'],2)} | {fnum(f['downtime_s'],2)} |",
          f"| MTTF (s) | {fnum(b['MTTF_s'])} | {fnum(f['MTTF_s'])} |", f"| MTTR (s) | {fnum(b['MTTR_s'],2)} | {fnum(f['MTTR_s'],2)} |",
          f"| Availability observed | {pct(b['availability_observed'])} | {pct(f['availability_observed'])} |",
          f"| Availability theory (3 services in series) | {pct(res_b.get('system_theory_series_of_3_services'))} | {pct(res_f.get('system_theory_series_of_3_services'))} |", ""]
    return "\n".join(L)


def main(results_dir: Path):
    b = json.loads((results_dir / "campaign_baseline.json").read_text())
    f = json.loads((results_dir / "campaign_ft.json").read_text())
    rb, rf = analyse_campaign(b), analyse_campaign(f)
    (results_dir / "reliability.json").write_text(json.dumps({"baseline": rb, "ft": rf}, indent=2))
    (results_dir / "reliability.md").write_text(campaign_markdown(rb, rf))
    print((results_dir / "reliability.md").read_text())


if __name__ == "__main__":
    import sys
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "results")

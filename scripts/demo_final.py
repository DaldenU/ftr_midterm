#!/usr/bin/env python3
"""Live demonstration for the final presentation: four scenarios, final vs baseline, 1 run each.

  1. E1 application instance crash  (failover + self-healing)
  2. E4 interrupted payments        (idempotency + checkpoint recovery)
  3. E6 fault sequence + alerts     (Prometheus alert rules, trace ids)
  4. E7 misuse battery + flood      (authentication, RBAC/BOLA, validation, rate limiting, audit chain)

Usage: python3 scripts/demo_final.py [--only 1,3]   (results go to experiments/results_demo/)
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMOS = {"1": ("S1", "Application instance crash"), "2": ("S5", "Interrupted multi-step transactions"),
         "3": ("S8", "Monitoring and alert trigger"), "4": ("S9", "Unauthorized / invalid operations")}

ap = argparse.ArgumentParser()
ap.add_argument("--only", default="1,2,3,4")
a = ap.parse_args()
for k in a.only.split(","):
    sid, title = DEMOS[k]
    print(f"\n=== Demo {k}: {title} ({sid}) - baseline then final ===", flush=True)
    subprocess.run([sys.executable, str(ROOT / "experiments/run_experiments.py"), "--scenarios", sid,
                    "--modes", "baseline,final", "--reps", "1", "--results", str(ROOT / "experiments/results_demo")], check=True)
subprocess.run([sys.executable, str(ROOT / "experiments/analysis/final_report_tables.py"), "--results",
                str(ROOT / "experiments/results_demo")], check=True)
print((ROOT / "experiments/results_demo/comparison.md").read_text())

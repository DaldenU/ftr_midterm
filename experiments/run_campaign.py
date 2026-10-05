#!/usr/bin/env python3
"""Run the reliability campaign for both system versions -> results/campaign_<mode>.json"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.harness.campaign import run_campaign
from experiments.harness.pg import PgCluster
from experiments.harness.stack import default_workdir

ap = argparse.ArgumentParser()
ap.add_argument("--modes", default="baseline,ft")
ap.add_argument("--duration", type=float, default=240.0)
ap.add_argument("--mttf-c", type=float, default=15.0, help="injected mean time to failure of EACH instance (s)")
ap.add_argument("--operator-delay", type=float, default=10.0)
ap.add_argument("--seed", type=int, default=7)
ap.add_argument("--results", default=str(Path(__file__).resolve().parent / "results"))
a = ap.parse_args()
pg = PgCluster(default_workdir())
pg.start()
try:
    for mode in a.modes.split(","):
        print(f"campaign {mode} ({a.duration}s) ...", flush=True)
        res = run_campaign(mode, pg, duration=a.duration, mttf_c=a.mttf_c, operator_delay=a.operator_delay, seed=a.seed)
        out = Path(a.results); out.mkdir(parents=True, exist_ok=True)
        (out / f"campaign_{mode}.json").write_text(json.dumps(res, indent=2, default=str))
        print(f"  kills={len(res['kills'])} services=" + str({s: (len(v['outages']), v['downtime_s']) for s, v in res['services'].items()}), flush=True)
finally:
    pg.stop()

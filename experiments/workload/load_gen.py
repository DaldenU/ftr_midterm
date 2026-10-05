#!/usr/bin/env python3
"""Workload generator CLI - works against ANY gateway URL (local stack, Docker Compose, Kubernetes).

  python experiments/workload/load_gen.py --url http://localhost:8000 --rate 20 --duration 30
  python experiments/workload/load_gen.py --url http://localhost:8000 --concurrency 100 --duration 20   # closed loop

Prints one line per second (ok / failed / p95 / degraded) so failures and recoveries are visible live, then a summary.
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from experiments.workload.generator import Client, Recorder, Workload, summarize  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--rate", type=float, default=20, help="open-loop requests/s")
    ap.add_argument("--concurrency", type=int, default=0, help="closed-loop clients (overrides --rate)")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--jsonl", help="write every request record to this file")
    a = ap.parse_args()
    rec = Recorder()
    wl = Workload(Client(a.url, timeout=5.0), rec)
    th = threading.Thread(target=(lambda: wl.run_closed_loop_async(a.concurrency, a.duration)) if a.concurrency
                          else (lambda: wl.run_open_loop(a.duration, a.rate)), daemon=True)
    th.start()
    last, t0 = 0, time.time()
    while th.is_alive():
        time.sleep(1)
        recs = rec.snapshot()
        window = [r for r in recs if r.t1 >= time.time() - 1]
        s = summarize(window)
        print(f"t={time.time() - t0:5.0f}s  ok={s['success']:4d} failed={s['failed']:4d} degraded={s['degraded']:4d} "
              f"recovered={s['recovered']:3d} p95={s['p95_all_ms']:7.1f}ms", flush=True)
    s = summarize(rec.snapshot())
    print("SUMMARY", s)
    if a.jsonl:
        rec.dump_jsonl(a.jsonl)


if __name__ == "__main__":
    main()

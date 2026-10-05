#!/usr/bin/env python3
"""Failure-injection CLI for a RUNNING deployment (Docker Compose, Kubernetes or any HTTP-reachable stack).

HTTP faults work everywhere (they use the /admin/fault endpoints, enabled by ENABLE_FAULT_INJECTION=1):
  inject.py latency  --target http://localhost:8000-service-url --ms 3000 [--error-rate 0.3]
  inject.py clear    --target <service url>
Infrastructure faults use the container runtime:
  inject.py kill     --backend docker --name payment-1           # docker kill (SIGKILL) of one instance
  inject.py kill     --backend k8s    --name payment-0           # kubectl delete pod --force
  inject.py node     --backend docker --node A                   # kill every container labelled uni.node=A
  inject.py db-stop  --backend docker|k8s    /   inject.py db-start ...
Every command prints the injection timestamp (epoch + ISO) so it can be correlated with logs/metrics.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time

import httpx


def stamp(what):
    t = time.time()
    print(json.dumps({"event": what, "epoch": round(t, 3), "iso": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()}))


def sh(cmd):
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["latency", "clear", "kill", "node", "db-stop", "db-start"])
    ap.add_argument("--backend", choices=["docker", "k8s"], default="docker")
    ap.add_argument("--target", help="base URL of ONE service instance (latency/clear)")
    ap.add_argument("--ms", type=float, default=3000)
    ap.add_argument("--error-rate", type=float, default=0.0)
    ap.add_argument("--name", help="container / pod name")
    ap.add_argument("--node", help="logical node label (A/B) for docker")
    ap.add_argument("--project", default="uni-ft", help="docker compose project name")
    ap.add_argument("--namespace", default="uni")
    a = ap.parse_args()

    if a.action in ("latency", "clear"):
        if not a.target:
            ap.error("--target is required")
        if a.action == "latency":
            stamp("inject_latency")
            print(httpx.post(a.target + "/admin/fault", json={"latency_ms": a.ms, "error_rate": a.error_rate}).json())
        else:
            stamp("clear_fault")
            print(httpx.delete(a.target + "/admin/fault").json())
    elif a.action == "kill":
        stamp("kill_instance")
        sh(["docker", "kill", f"{a.project}-{a.name}-1"] if a.backend == "docker" else
           ["kubectl", "-n", a.namespace, "delete", "pod", a.name, "--grace-period=0", "--force"])
    elif a.action == "node":
        if a.backend != "docker":
            ap.error("for Kubernetes use: kubectl drain <node>")
        ids = sh(["docker", "ps", "-q", "--filter", f"label=uni.node={a.node}"]).split()
        stamp("kill_node")
        sh(["docker", "kill", *ids])
    elif a.action == "db-stop":
        stamp("db_stop")
        sh(["docker", "stop", "-t", "0", f"{a.project}-postgres-1"] if a.backend == "docker" else
           ["kubectl", "-n", a.namespace, "delete", "pod", "postgres-0", "--grace-period=0", "--force"])
    elif a.action == "db-start":
        stamp("db_start")
        if a.backend == "docker":
            sh(["docker", "start", f"{a.project}-postgres-1"])
        else:
            print("StatefulSet recreates postgres-0 automatically")


if __name__ == "__main__":
    main()

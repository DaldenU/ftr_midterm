#!/usr/bin/env python3
"""Container experiment C1: does the container engine really self-heal the deployment?

Runs the SAME application code as containers on a real Docker engine (one container per
replica, user-defined bridge network, read-only root filesystem, non-root user, Docker
healthchecks) and injects:

  kill  - the container's main process is SIGKILLed from the host (a real crash; `docker kill`
          would count as a manual stop and disable the restart policy)
          final:    restart: unless-stopped, 2 replicas  -> engine restarts it, gateway fails over
          baseline: restart: no, 1 replica               -> stays down until an operator runs
                                                            `docker start` (assumed 10 s delay)
  hang  - the main process is SIGSTOPped for 15 s (alive but not answering), final only:
          the Docker healthcheck marks it unhealthy, but Compose/Docker do NOT restart unhealthy
          containers (Kubernetes liveness probes would) - the gateway's readiness checks must eject it.

Image: in this sandbox no registry is reachable, so the containers use a minimal local image
(`uis-hostruntime:1`, symlinks only) and bind-mount the host's /usr (Python, packages) and the
repository read-only. The production Dockerfiles are unchanged; this only replaces the base layer.
PostgreSQL runs on the host and is reached through the bridge gateway address.

Usage: python3 experiments/container/c1_restart.py --reps 3
Output: experiments/results_final/container/C1-<case>-r<n>.json and summary.json
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "shared")]
from experiments.harness.pg import PgCluster  # noqa: E402
from experiments.harness.stack import SecurityFixture  # noqa: E402
from experiments.workload.generator import Client, Recorder, Workload, summarize  # noqa: E402

IMAGE = "uis-hostruntime:1"
NET = "uis-c1"
GW_PORT = 18900
OUT = ROOT / "experiments" / "results_final" / "container"


def sh(*args, check=True, capture=True):
    r = subprocess.run(list(args), capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def ensure_image():
    if sh("docker", "images", "-q", IMAGE):
        return
    d = Path(tempfile.mkdtemp())
    for sub in ("usr", "etc", "tmp", "proc", "sys", "dev", "app", "run"):
        (d / sub).mkdir()
    for name, target in (("bin", "usr/bin"), ("lib", "usr/lib"), ("lib64", "usr/lib64"), ("sbin", "usr/sbin")):
        os.symlink(target, d / name)
    subprocess.run(f"tar -C {d} -c . | docker import - {IMAGE}", shell=True, check=True, capture_output=True)


class Deployment:
    def __init__(self, mode: str, pg: PgCluster, workdir: Path):
        self.mode = mode
        self.replicas = 2 if mode == "final" else 1
        self.policy = "unless-stopped" if mode == "final" else "no"
        self.sec = SecurityFixture(workdir) if mode == "final" else None
        self.db = f"uni_{mode}"
        self.pg = pg
        self.names: list[str] = []

    def env(self, name):
        gw_ip = sh("docker", "network", "inspect", NET, "-f", "{{(index .IPAM.Config 0).Gateway}}")
        e = {"FT_MODE": self.mode, "DATABASE_URL": f"postgresql://uni@{gw_ip}:{self.pg.port}/{self.db}",
             "INSTANCE_ID": name, "PYTHONDONTWRITEBYTECODE": "1", "FTLIB_PATH": "/app/shared", "ENABLE_FAULT_INJECTION": "0",
             "STUDENT_SERVICE_URLS": ",".join(f"http://student-{i + 1}:8000" for i in range(self.replicas)),
             "STUDENT_URLS": ",".join(f"http://student-{i + 1}:8000" for i in range(self.replicas)),
             "PAYMENT_URLS": ",".join(f"http://payment-{i + 1}:8000" for i in range(self.replicas)),
             "ACADEMIC_URLS": ",".join(f"http://academic-{i + 1}:8000" for i in range(self.replicas))}
        if self.sec:
            e.update(self.sec.env())
            e["USERS_FILE"] = "/run/users.json"
        return e

    def run(self, name, appdir, publish=None):
        args = ["docker", "run", "-d", "--name", name, "--hostname", name, "--network", NET,
                "--restart", self.policy, "--read-only", "--tmpfs", "/tmp", "--user", "10001:10001",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "256m", "--cpus", "0.5",
                "-v", "/usr:/usr:ro", "-v", f"{ROOT}:/app:ro",
                "--health-cmd", "python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=1)\"",
                "--health-interval", "2s", "--health-timeout", "1s", "--health-retries", "2", "--health-start-period", "5s",
                "--label", f"uni.node={'A' if name.endswith('-1') else 'B'}"]
        if self.sec and name == "gateway":
            args += ["-v", f"{self.sec.users_file}:/run/users.json:ro"]
        if publish:
            args += ["-p", f"127.0.0.1:{publish}:8000"]
        for k, v in self.env(name).items():
            args += ["-e", f"{k}={v}"]
        args += [IMAGE, "/usr/local/bin/python3", "-m", "uvicorn", "app:app", "--app-dir", appdir,
                 "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning", "--no-access-log"]
        sh(*args)
        self.names.append(name)

    def up(self):
        self.down()
        sh("docker", "network", "create", NET, check=False)
        self.pg.reset_db(self.db, ROOT / f"database/schema_{self.mode}.sql")
        for svc in ("student", "payment", "academic"):
            for i in range(self.replicas):
                self.run(f"{svc}-{i + 1}", f"/app/services/{svc}-service")
        self.run("gateway", "/app/gateway", publish=GW_PORT)
        t0 = time.time()
        while time.time() - t0 < 60:
            if all(self.inspect(n).get("health") == "healthy" for n in self.names):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("containers did not become healthy: " + json.dumps({n: self.inspect(n) for n in self.names}))
        time.sleep(1.5)

    def down(self):
        names = sh("docker", "ps", "-aq", "--filter", f"network={NET}", check=False).split()
        if names:
            sh("docker", "rm", "-f", *names, check=False)
        sh("docker", "network", "rm", NET, check=False)

    def inspect(self, name) -> dict:
        try:
            j = json.loads(sh("docker", "inspect", name))[0]
        except Exception:
            return {}
        st = j["State"]
        return {"status": st["Status"], "pid": st["Pid"], "restarts": j.get("RestartCount", 0),
                "health": (st.get("Health") or {}).get("Status")}


def watch(dep: Deployment, name: str, samples: list, stop: threading.Event):
    while not stop.is_set():
        s = dep.inspect(name)
        s["t"] = time.time()
        samples.append(s)
        time.sleep(0.2)


def first(samples, pred, after):
    return next((s["t"] for s in samples if s["t"] >= after and pred(s)), None)


def run_case(case: str, mode: str, rep: int, pg: PgCluster, workdir: Path, operator_delay=10.0, duration=36.0,
             rate=20.0, hang_s=15.0) -> dict:
    dep = Deployment(mode, pg, workdir)
    dep.up()
    target = "payment-1" if case == "kill" else "student-1"
    rec = Recorder()
    client = Client(f"http://127.0.0.1:{GW_PORT}", timeout=5.0, seed=rep, security=dep.sec)
    wl = Workload(client, rec, seed=rep)
    samples, stop = [], threading.Event()
    wt = threading.Thread(target=watch, args=(dep, target, samples, stop), daemon=True)
    wt.start()
    th = threading.Thread(target=wl.run_open_loop, args=(duration, rate), daemon=True)
    t_start = time.time()
    th.start()
    time.sleep(8.0)
    before = dep.inspect(target)
    t_fail = time.time()
    if case == "kill":
        os.kill(before["pid"], signal.SIGKILL)               # real crash of the container's main process
        if mode == "baseline":
            time.sleep(operator_delay)
            sh("docker", "start", target)                    # operator repair
    else:
        os.kill(before["pid"], signal.SIGSTOP)               # hung process: alive, not answering
        time.sleep(hang_s)
        os.kill(before["pid"], signal.SIGCONT)
    t_clear = time.time() if case == "hang" else None
    th.join()
    time.sleep(3.0)
    stop.set()
    wt.join()
    recs = rec.snapshot()
    after_fail = [r for r in recs if r.t0 >= t_fail]
    s_all, s_win = summarize(recs), summarize(after_fail)
    t_down = first(samples, lambda s: s.get("status") != "running" or s.get("pid") != before["pid"], t_fail)
    t_running = first(samples, lambda s: s.get("status") == "running" and s.get("pid") not in (0, before["pid"]), t_fail)
    t_unhealthy = first(samples, lambda s: s.get("health") == "unhealthy", t_fail)
    t_healthy = first(samples, lambda s: s.get("health") == "healthy" and (case == "hang" or s.get("pid") != before["pid"]),
                      (t_running or t_clear or t_fail))
    end = dep.inspect(target)
    gw_events = []
    try:
        h = {"X-Admin-Token": dep.sec.admin_token} if dep.sec else {}
        gw_events = httpx.get(f"http://127.0.0.1:{GW_PORT}/admin/events", headers=h, timeout=2).json().get("events", [])
    except Exception:
        pass
    ej = next((e["ts"] for e in gw_events if e["state"] == "down" and target in e["backend"] and e["ts"] >= t_fail), None)
    back = next((e["ts"] for e in gw_events if e["state"] == "up" and target in e["backend"] and e["ts"] >= t_fail), None)
    rel = lambda t: round(t - t_fail, 2) if t else None
    rec_out = {
        "case": case, "mode": mode, "rep": rep, "restart_policy": dep.policy, "replicas": dep.replicas, "target": target,
        "fault": "SIGKILL of the container's main process" if case == "kill" else f"SIGSTOP of the main process for {hang_s:.0f}s",
        "requests_total": s_all["total"], "requests_failed": s_all["failed"], "failed_after_fault": s_win["failed"],
        "success_ratio": round(s_all["success"] / s_all["total"], 4) if s_all["total"] else None,
        "p95_ms": s_all["p95_ms"], "recovered_requests": s_all["recovered"],
        "restart_count_before": before["restarts"], "restart_count_after": end.get("restarts"),
        "container_exited_s": rel(t_down), "container_running_again_s": rel(t_running),
        "docker_unhealthy_s": rel(t_unhealthy), "docker_healthy_again_s": rel(t_healthy),
        "gateway_ejected_s": rel(ej), "gateway_restored_s": rel(back),
        "operator_delay_s": operator_delay if (mode == "baseline" and case == "kill") else None,
        "hang_cleared_s": rel(t_clear), "state_samples": len(samples), "t_start": t_start,
    }
    dep.down()
    return rec_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    ensure_image()
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path("/var/tmp/ftuis-c1")
    pg = PgCluster(work, port=58432, standby_port=58433)
    pg.init()
    conf = pg.data / "postgresql.conf"
    if "listen_addresses = '*'" not in conf.read_text():
        conf.write_text(conf.read_text() + "\nlisten_addresses = '*'\n")
        (pg.data / "pg_hba.conf").write_text((pg.data / "pg_hba.conf").read_text() + "\nhost all all 172.16.0.0/12 trust\n")
    pg.start()
    results = []
    try:
        for rep in range(1, a.reps + 1):
            for case, mode in (("kill", "baseline"), ("kill", "final"), ("hang", "final")):
                print(f"[{time.strftime('%H:%M:%S')}] C1 {case}/{mode} rep {rep}", flush=True)
                r = run_case(case, mode, rep, pg, work)
                (OUT / f"C1-{case}-{mode}-r{rep}.json").write_text(json.dumps(r, indent=2))
                print("   ", {k: r[k] for k in ("requests_failed", "requests_total", "container_running_again_s",
                                                "docker_unhealthy_s", "docker_healthy_again_s", "gateway_ejected_s",
                                                "restart_count_after")}, flush=True)
                results.append(r)
    finally:
        pg.stop()
    summ = {}
    for case, mode in (("kill", "baseline"), ("kill", "final"), ("hang", "final")):
        rs = [r for r in results if r["case"] == case and r["mode"] == mode]
        row = {}
        for k in ("requests_failed", "requests_total", "success_ratio", "p95_ms", "container_running_again_s",
                  "docker_unhealthy_s", "docker_healthy_again_s", "gateway_ejected_s", "gateway_restored_s", "restart_count_after"):
            xs = [r[k] for r in rs if r[k] is not None]
            row[k] = {"mean": round(statistics.fmean(xs), 3), "min": min(xs), "max": max(xs), "n": len(xs)} if xs else None
        summ[f"{case}-{mode}"] = row
    (OUT / "summary.json").write_text(json.dumps(summ, indent=2))
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

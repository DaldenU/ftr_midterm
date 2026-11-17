"""Real Prometheus server per experiment run + alert timeline recorder.

The harness starts the official Prometheus binary (PROMETHEUS_BIN, default
/opt/prom/prometheus) with a generated scrape config for the gateway and every
service instance, the project's alert rules (monitoring/prometheus/alerts.yml),
1 s scrape and evaluation intervals and a throw-away TSDB. An ``AlertWatcher``
polls ``/api/v1/alerts`` at 4 Hz and records, per alert name, when it became
pending, firing and resolved. This is the *system's own* fault detection; the
experiment compares it with the instant the fault was injected.

If the binary is missing the runner is disabled and experiments still run
(records then carry ``alerts: null``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from .pg import ROOT

PROM_BIN = os.getenv("PROMETHEUS_BIN", "/opt/prom/prometheus")
RULES = ROOT / "monitoring" / "prometheus" / "alerts.yml"


class PromRunner:
    def __init__(self, targets: dict[str, list[tuple[str, str]]], port: int = 19090, interval: str = "1s"):
        """targets: job -> [(name, 'host:port')]"""
        self.targets, self.port, self.interval = targets, port, interval
        self.enabled = Path(PROM_BIN).exists()
        self.proc: subprocess.Popen | None = None
        self.dir: Path | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _config(self) -> str:
        lines = ["global:", f"  scrape_interval: {self.interval}", f"  evaluation_interval: {self.interval}",
                 f"  scrape_timeout: {self.interval}", f"rule_files: [{RULES}]", "scrape_configs:"]
        for job, items in self.targets.items():
            lines.append(f"  - job_name: {job}")
            lines.append("    static_configs:")
            for name, hostport in items:
                lines.append(f"      - targets: ['{hostport}']")
                lines.append(f"        labels: {{name: '{name}'}}")
        return "\n".join(lines) + "\n"

    def start(self, timeout: float = 20.0) -> bool:
        if not self.enabled:
            return False
        self.dir = Path(tempfile.mkdtemp(prefix="prom-"))
        (self.dir / "prometheus.yml").write_text(self._config())
        log = open(self.dir / "prometheus.log", "wb")
        self.proc = subprocess.Popen(
            [PROM_BIN, f"--config.file={self.dir / 'prometheus.yml'}", f"--storage.tsdb.path={self.dir / 'data'}",
             f"--web.listen-address=127.0.0.1:{self.port}", "--storage.tsdb.retention.time=2h", "--log.level=warn"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if httpx.get(self.url + "/-/ready", timeout=0.5).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        return False

    def query_range(self, expr: str, t0: float, t1: float, step: float = 1.0):
        try:
            r = httpx.get(self.url + "/api/v1/query_range",
                          params={"query": expr, "start": t0, "end": t1, "step": step}, timeout=5)
            return r.json().get("data", {}).get("result", [])
        except Exception:
            return []

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)


class AlertWatcher:
    """Records alert state transitions (aggregated per alert name)."""

    def __init__(self, prom: PromRunner, interval: float = 0.25):
        self.prom, self.interval = prom, interval
        self.transitions: list[dict] = []          # {ts, alert, state: pending|firing|resolved}
        self._state: dict[str, str] = {}
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self):
        if not self.prom.enabled:
            return
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(2)

    def _loop(self):
        c = httpx.Client(timeout=1.0)
        while not self._stop.is_set():
            now = time.time()
            try:
                alerts = c.get(self.prom.url + "/api/v1/alerts").json()["data"]["alerts"]
            except Exception:
                time.sleep(self.interval)
                continue
            cur: dict[str, str] = {}
            for a in alerts:
                name = a["labels"]["alertname"]
                st = a["state"]                      # pending | firing
                if cur.get(name) != "firing":
                    cur[name] = st
            for name, st in cur.items():
                if self._state.get(name) != st:
                    self.transitions.append({"ts": now, "alert": name, "state": st})
            for name in list(self._state):
                if name not in cur and self._state[name] in ("pending", "firing"):
                    self.transitions.append({"ts": now, "alert": name, "state": "resolved"})
            self._state = cur
            time.sleep(max(0.0, self.interval - (time.time() - now)))
        c.close()

    # ------------------------------------------------------------ analysis
    def first(self, alert: str, state: str = "firing", after: float = 0.0):
        return next((t["ts"] for t in self.transitions if t["alert"] == alert and t["state"] == state and t["ts"] >= after), None)

    def fired_between(self, t0: float, t1: float) -> list[str]:
        return sorted({t["alert"] for t in self.transitions if t["state"] == "firing" and t0 <= t["ts"] <= t1})

    def summary(self, t_ref: float) -> dict:
        out = {}
        for name in sorted({t["alert"] for t in self.transitions}):
            f = self.first(name, "firing")
            r = self.first(name, "resolved", f or 0) if f else None
            out[name] = {"first_firing_rel_s": round(f - t_ref, 2) if f else None,
                         "resolved_rel_s": round(r - t_ref, 2) if r else None,
                         "times_fired": sum(1 for t in self.transitions if t["alert"] == name and t["state"] == "firing")}
        return out

"""Local process-based deployment of the whole system (used when Docker is unavailable).

It starts the SAME application code as the containers: gateway + N replicas of
each service as OS processes, against a real PostgreSQL cluster. Replica 1 of
every service is placed on logical "node A", replica 2 on "node B", so that a
node failure (kill every process of one node) can be injected.

Orchestration is modelled explicitly:
  * ft mode       - a supervisor thread restarts dead processes after
                    ``restart_delay_s`` (equivalent of Docker ``restart:
                    unless-stopped`` / a Kubernetes ReplicaSet).
  * baseline mode - nothing restarts a dead process; recovery happens only when
                    the experiment performs an *operator restart* after a
                    configured human reaction delay.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .pg import PgCluster, ROOT

if str(ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared"))

SERVICE_DIRS = {"student": "services/student-service", "payment": "services/payment-service",
                "academic": "services/academic-service"}
SERVICE_BASE_PORT = {"student": 100, "payment": 200, "academic": 300}


SIGKILL = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 9))
SIGTERM = getattr(signal, "SIGTERM", 15)


def _set_nice(n: int):
    if hasattr(os, "nice"):
        try:
            os.nice(n)
        except OSError:
            pass   # not permitted (non-root): continue with default priority


def default_workdir() -> Path:
    env = os.getenv("FTUIS_WORKDIR")
    if env:
        return Path(env)
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    return Path("/var/tmp/ftuis-work") if is_root else ROOT / ".work"


@dataclass
class Instance:
    name: str
    service: str            # student | payment | academic | gateway
    idx: int
    node: str
    port: int
    proc: subprocess.Popen | None = None
    stopped_by_operator: bool = False
    restarting: bool = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class SecurityFixture:
    """Per-deployment secrets and synthetic principals for FT_MODE=final.

    Nothing is committed to the repository: secrets and demo passwords are generated
    at start-up, PBKDF2 hashes are written to a users file inside the work directory,
    and the harness (as deployment owner) can mint tokens for its load generators.
    """

    ROLES = ([(f"S{i:04d}", "student") for i in range(1, 51)] + [(f"FAC{i:02d}", "faculty") for i in range(1, 21)]
             + [("REG01", "registrar"), ("FIN01", "finance"), ("ADM01", "admin")])

    def __init__(self, workdir: Path):
        from ftlib import security as sec
        self.sec = sec
        self.jwt_secret = secrets.token_urlsafe(48)
        self.internal_secret = secrets.token_urlsafe(48)
        self.admin_token = secrets.token_urlsafe(48)
        self.passwords = {u: secrets.token_urlsafe(12) for u, _ in self.ROLES}
        self.roles = dict(self.ROLES)
        self.users_file = Path(workdir) / f"users-{secrets.token_hex(4)}.json"
        self.users_file.parent.mkdir(parents=True, exist_ok=True)
        users = {u: {"role": r, "hash": sec.hash_password(self.passwords[u], iterations=20000)} for u, r in self.ROLES}
        self.users_file.write_text(json.dumps(users))
        self._tokens: dict[str, str] = {}

    def env(self) -> dict:
        return {"JWT_SECRET": self.jwt_secret, "INTERNAL_AUTH_SECRET": self.internal_secret,
                "ADMIN_TOKEN": self.admin_token, "USERS_FILE": str(self.users_file)}

    def token(self, user: str, ttl_s: int = 3600) -> str:
        if user not in self._tokens:
            self._tokens[user] = self.sec.issue_token(self.jwt_secret, user, self.roles[user], ttl_s=ttl_s)
        return self._tokens[user]

    def auth(self, user: str) -> dict:
        return {"Authorization": f"Bearer {self.token(user)}"}


class Stack:
    def __init__(self, mode: str, pg: PgCluster, replicas: int | None = None, base_port: int = 18000,
                 restart_delay_s: float = 1.0, env: dict | None = None, workdir: Path | None = None,
                 database_url: str | None = None, ha_db: bool = False):
        assert mode in ("baseline", "ft", "final")
        self.mode, self.pg = mode, pg
        self.is_ft = mode in ("ft", "final")
        self.replicas = replicas if replicas is not None else (2 if self.is_ft else 1)
        self.base_port = base_port
        self.restart_delay_s = restart_delay_s
        self.extra_env = env or {}
        self.workdir = Path(workdir or default_workdir())
        self.logdir = self.workdir / "logs" / mode
        self.db_name = f"uni_{mode}"
        self.database_url = database_url or (pg.dsn_ha(self.db_name) if ha_db else pg.dsn(self.db_name))
        self.events: list[dict] = []
        self.instances: dict[str, Instance] = {}
        self._stop = threading.Event()
        self._sup: threading.Thread | None = None
        self.security = SecurityFixture(self.workdir) if mode == "final" else None
        self.admin_headers = {"X-Admin-Token": self.security.admin_token} if self.security else {}
        self.http = httpx.Client(timeout=3.0, headers=self.admin_headers)
        self.nice = int(os.getenv("FTUIS_SERVICE_NICE", "5"))
        self.gateway_nice = int(os.getenv("FTUIS_GATEWAY_NICE", "-5"))
        self.supervise = self.is_ft
        for svc, off in SERVICE_BASE_PORT.items():
            for i in range(self.replicas):
                name = f"{svc}-{i + 1}"
                self.instances[name] = Instance(name, svc, i, "AB"[i % 2], base_port + off + i + 1)
        self.gateway = Instance("gateway", "gateway", 0, "edge", base_port)
        self.instances["gateway"] = self.gateway

    # ------------------------------------------------------------------ helpers
    def log_event(self, type_: str, name: str = "", **kw):
        self.events.append({"ts": time.time(), "type": type_, "name": name, **kw})

    @property
    def gateway_url(self) -> str:
        return self.gateway.url

    def service_urls(self, svc: str) -> list[str]:
        return [i.url for i in self.instances.values() if i.service == svc]

    def _env_for(self, inst: Instance) -> dict:
        env = dict(os.environ)
        env.update({
            "FT_MODE": self.mode, "DATABASE_URL": self.database_url, "INSTANCE_ID": inst.name,
            "ENABLE_FAULT_INJECTION": "1", "FTLIB_PATH": str(ROOT / "shared"), "PYTHONUNBUFFERED": "1",
            "STUDENT_SERVICE_URLS": ",".join(self.service_urls("student")),
            "STUDENT_URLS": ",".join(self.service_urls("student")),
            "PAYMENT_URLS": ",".join(self.service_urls("payment")),
            "ACADEMIC_URLS": ",".join(self.service_urls("academic")),
        })
        if self.security:
            env.update(self.security.env())
        env.update({k: str(v) for k, v in self.extra_env.items()})
        return env

    def _spawn(self, inst: Instance):
        self.logdir.mkdir(parents=True, exist_ok=True)
        logf = open(self.logdir / f"{inst.name}.log", "ab")
        appdir = str(ROOT / (SERVICE_DIRS[inst.service] if inst.service != "gateway" else "gateway"))
        kwargs = {
            "env": self._env_for(inst), "stdout": logf, "stderr": subprocess.STDOUT, "start_new_session": True,
        }
        if hasattr(os, "nice") and sys.platform != "win32":
            kwargs["preexec_fn"] = lambda n=(self.gateway_nice if inst.service == 'gateway' else self.nice): _set_nice(n)
        inst.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--app-dir", appdir, "--host", "127.0.0.1",
             "--port", str(inst.port), "--log-level", "warning", "--no-access-log"],
            **kwargs)
        inst.stopped_by_operator = False

    def wait_ready(self, inst: Instance, timeout: float = 30.0) -> float:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not inst.alive():
                raise RuntimeError(f"{inst.name} exited with {inst.proc.returncode if inst.proc else None}")
            try:
                if self.http.get(inst.url + "/health", timeout=0.5).status_code == 200:
                    return time.time()
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise TimeoutError(f"{inst.name} not healthy after {timeout}s")

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._stop.clear()
        for inst in self.instances.values():
            if inst.name != "gateway":
                self._spawn(inst)
        for inst in self.instances.values():
            if inst.name != "gateway":
                self.wait_ready(inst)
        self._spawn(self.gateway)
        self.wait_ready(self.gateway)
        if self.is_ft:                              # wait until the gateway sees every backend healthy
            t0 = time.time()
            while time.time() - t0 < 15:
                try:
                    if self.http.get(self.gateway_url + "/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        if self.supervise:
            self._sup = threading.Thread(target=self._supervisor, name="supervisor", daemon=True)
            self._sup.start()
        self.log_event("stack_started")

    def stop(self):
        self._stop.set()
        for inst in self.instances.values():
            self._terminate(inst, SIGTERM)
        deadline = time.time() + 5
        for inst in self.instances.values():
            if inst.proc:
                try:
                    inst.proc.wait(max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    self._terminate(inst, SIGKILL)
        self.http.close()

    def _terminate(self, inst: Instance, sig):
        if inst.proc and inst.proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(inst.proc.pid, sig)
                elif hasattr(inst.proc, "kill") and sig == SIGKILL:
                    inst.proc.kill()
                else:
                    inst.proc.terminate()
            except (ProcessLookupError, OSError):
                pass

    # ------------------------------------------------------------------ orchestration (self-healing)
    def _supervisor(self):
        while not self._stop.is_set():
            for inst in list(self.instances.values()):
                if inst.proc is not None and not inst.alive() and not inst.stopped_by_operator and not inst.restarting:
                    inst.restarting = True
                    self.log_event("supervisor_detected_exit", inst.name)
                    threading.Thread(target=self._restart_later, args=(inst, self.restart_delay_s), daemon=True).start()
            time.sleep(0.1)

    def _restart_later(self, inst: Instance, delay: float):
        try:
            time.sleep(delay)
            if self._stop.is_set():
                return
            self.restart_instance(inst.name)
        finally:
            inst.restarting = False

    def restart_instance(self, name: str) -> float:
        inst = self.instances[name]
        self.log_event("restart_start", name)
        self._spawn(inst)
        t = self.wait_ready(inst)
        self.log_event("restart_ready", name)
        return t

    # ------------------------------------------------------------------ failure injection
    def kill_instance(self, name: str, sig=SIGKILL) -> float:
        inst = self.instances[name]
        t = time.time()
        self.log_event("fault_kill_instance", name)
        self._terminate(inst, sig)
        return t

    def kill_node(self, node: str) -> tuple[float, list[str]]:
        names = [i.name for i in self.instances.values() if i.node == node and i.service != "gateway"]
        t = time.time()
        self.log_event("fault_kill_node", node, instances=names)
        for n in names:
            self._terminate(self.instances[n], SIGKILL)
        return t, names

    def operator_restart(self, names: list[str]):
        """Baseline recovery: a human restarts the dead processes."""
        for n in names:
            self.log_event("operator_restart_start", n)
        threads = [threading.Thread(target=self.restart_instance, args=(n,)) for n in names]
        [t.start() for t in threads]
        [t.join() for t in threads]

    def set_fault(self, service: str, latency_ms: float = 0, error_rate: float = 0, fail_all: bool = False):
        t = time.time()
        for inst in self.instances.values():
            if inst.service == service and inst.alive():
                self.http.post(inst.url + "/admin/fault", json={"latency_ms": latency_ms, "error_rate": error_rate, "fail_all": fail_all})
        self.log_event("fault_set" if (latency_ms or error_rate or fail_all) else "fault_cleared", service,
                       latency_ms=latency_ms, error_rate=error_rate, fail_all=fail_all)
        return t

    def clear_faults(self):
        for svc in ("student", "payment", "academic"):
            self.set_fault(svc)

    # ------------------------------------------------------------------ resource use
    def cpu_seconds(self) -> dict:
        """utime+stime (s) of every live process of the stack, read from /proc (Linux)."""
        tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        out = {}
        for n, inst in self.instances.items():
            if inst.proc and inst.alive():
                try:
                    f = open(f"/proc/{inst.proc.pid}/stat").read().rsplit(")", 1)[1].split()
                    out[n] = (int(f[11]) + int(f[12])) / tck
                except (OSError, IndexError, ValueError):
                    pass
        return out

    def rss_mb(self) -> dict:
        out = {}
        for n, inst in self.instances.items():
            if inst.proc and inst.alive():
                try:
                    for line in open(f"/proc/{inst.proc.pid}/status"):
                        if line.startswith("VmRSS:"):
                            out[n] = round(int(line.split()[1]) / 1024, 1)
                except OSError:
                    pass
        return out

    # ------------------------------------------------------------------ introspection
    def gateway_events(self) -> dict:
        try:
            return self.http.get(self.gateway_url + "/admin/events").json()
        except Exception:
            return {"events": [], "backends": {}}

    def admin_state(self, name: str) -> dict | None:
        try:
            return self.http.get(self.instances[name].url + "/admin/state", timeout=1.0).json()
        except Exception:
            return None

    def service_metric(self, name: str, metric: str) -> float:
        """Sum of a Prometheus counter on one instance (0 if unreachable)."""
        try:
            txt = self.http.get(self.instances[name].url + "/metrics", timeout=2.0).text
        except Exception:
            return 0.0
        total = 0.0
        for line in txt.splitlines():
            if line.startswith(metric) and not line.startswith("#"):
                try:
                    total += float(line.rsplit(" ", 1)[1])
                except ValueError:
                    pass
        return total

"""Local PostgreSQL cluster manager used by the experiment harness.

Creates a private cluster (primary + optional streaming-replication standby)
under a work directory, so that database failures can be injected for real with
``pg_ctl`` (graceful stop, immediate/crash stop, kill -9, promote).
PostgreSQL refuses to run as root, so when the harness runs as root every
postgres command is executed through ``runuser -u postgres``.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]


def find_pg_bin() -> Path | None:
    env = os.getenv("PG_BIN")
    if env and (Path(env) / "initdb").exists():
        return Path(env)
    for cand in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if (Path(cand) / "initdb").exists():
            return Path(cand)
    w = shutil.which("initdb")
    return Path(w).parent if w else None


class PgCluster:
    def __init__(self, workdir: Path, port: int = 55432, standby_port: int = 55433):
        self.workdir = Path(workdir)
        self.port, self.standby_port = port, standby_port
        self.data = self.workdir / "pgdata"
        self.sb_data = self.workdir / "pgdata_standby"
        self.sock = self.workdir / "sock"
        self.bin = find_pg_bin()
        if self.bin is None:
            raise RuntimeError("PostgreSQL binaries not found (set PG_BIN)")
        self.as_root = hasattr(os, "geteuid") and os.geteuid() == 0

    # ------------------------------------------------------------------ helpers
    def _run(self, args: list[str], check=True, timeout=120, **kw):
        cmd = [str(self.bin / args[0])] + args[1:]
        if self.as_root:
            cmd = ["runuser", "-u", "postgres", "--"] + cmd
        return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=timeout, **kw)

    def dsn(self, db: str, port: int | None = None) -> str:
        return f"postgresql://uni@127.0.0.1:{port or self.port}/{db}"

    def dsn_ha(self, db: str) -> str:
        """Multi-host DSN: libpq connects to whichever host is currently read-write (primary or promoted standby)."""
        return (f"postgresql://uni@127.0.0.1:{self.port},127.0.0.1:{self.standby_port}/{db}"
                f"?target_session_attrs=read-write")

    # ------------------------------------------------------------------ lifecycle
    def init(self):
        if (self.data / "PG_VERSION").exists():
            return
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.sock.mkdir(exist_ok=True)
        if self.as_root:
            shutil.chown(self.workdir, "postgres", "postgres")
            shutil.chown(self.sock, "postgres", "postgres")
            os.chmod(self.workdir, 0o755)
        self._run(["initdb", "-D", str(self.data), "-U", "uni", "-A", "trust", "-E", "UTF8"])
        with open(self.data / "postgresql.conf", "a") as f:
            f.write(f"""
listen_addresses = '127.0.0.1'
port = {self.port}
unix_socket_directories = '{self.sock}'
max_connections = 300
shared_buffers = 128MB
wal_level = replica
max_wal_senders = 5
hot_standby = on
fsync = on
synchronous_commit = on
log_min_messages = warning
""")

    def start(self, wait: bool = True):
        self.init()
        self._run(["pg_ctl", "-D", str(self.data), "-l", str(self.workdir / "pg_primary.log"), "-w" if wait else "-W", "start"])

    def stop(self, mode: str = "fast"):
        """mode: smart|fast|immediate. 'immediate' = crash (no shutdown checkpoint)."""
        self._run(["pg_ctl", "-D", str(self.data), "-m", mode, "-w", "stop"], check=False)

    def kill9(self):
        """SIGKILL the postmaster (simulated power loss of the DB host)."""
        pid_file = self.data / "postmaster.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().splitlines()[0])
            os.kill(pid, 9)
        # children die with the postmaster's shared memory; wait for socket release
        for _ in range(50):
            if not self.is_up():
                break
            time.sleep(0.1)

    def is_up(self, port: int | None = None) -> bool:
        try:
            with psycopg.connect(self.dsn("postgres", port), connect_timeout=1, autocommit=True) as c:
                c.execute("SELECT 1")
            return True
        except Exception:
            return False

    def wait_up(self, timeout=30.0, port: int | None = None) -> float:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.is_up(port):
                return time.time()
            time.sleep(0.05)
        raise TimeoutError("PostgreSQL did not come up")

    # ------------------------------------------------------------------ data
    def reset_db(self, db: str, schema_file: Path, students: int = 50):
        with psycopg.connect(self.dsn("postgres"), autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{db}"')
        with psycopg.connect(self.dsn(db), autocommit=True) as c:
            c.execute(Path(schema_file).read_text())
            with c.transaction():
                for i in range(1, students + 1):
                    c.execute("INSERT INTO student.students(student_id, full_name, email, program) VALUES (%s,%s,%s,%s)",
                              (f"S{i:04d}", f"Student {i}", f"s{i:04d}@uni.test", "CS"))

    def table_counts(self, db: str) -> dict:
        with psycopg.connect(self.dsn(db), autocommit=True) as c:
            return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                    for t in ("student.students", "payment.payments", "payment.accounts", "academic.records")}

    # ------------------------------------------------------------------ backup / restore
    def backup(self, db: str, out: Path) -> Path:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if self.as_root:
            tmp = self.workdir / f"{out.name}"
            self._run(["pg_dump", "-h", "127.0.0.1", "-p", str(self.port), "-U", "uni", "-Fc", "-f", str(tmp), db])
            shutil.copy(tmp, out)
        else:
            self._run(["pg_dump", "-h", "127.0.0.1", "-p", str(self.port), "-U", "uni", "-Fc", "-f", str(out), db])
        return out

    def restore(self, db: str, dump: Path):
        with psycopg.connect(self.dsn("postgres"), autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{db}"')
        src = Path(dump)
        if self.as_root:
            tmp = self.workdir / src.name
            shutil.copy(src, tmp)
            shutil.chown(tmp, "postgres", "postgres")
            src = tmp
        self._run(["pg_restore", "-h", "127.0.0.1", "-p", str(self.port), "-U", "uni", "-d", db, str(src)])

    # ------------------------------------------------------------------ disaster simulation
    def _clean_sockets(self):
        """Remove stale unix-socket files/locks left behind by a SIGKILLed postmaster."""
        for f in self.sock.glob(".s.PGSQL.*"):
            f.unlink(missing_ok=True)

    def destroy_primary(self):
        """Simulate loss of the primary DB host: SIGKILL + the data directory is gone."""
        self.kill9()
        shutil.rmtree(self.data, ignore_errors=True)
        self._clean_sockets()

    def destroy_all(self):
        """Stop everything and delete both data directories (clean slate for the next experiment)."""
        for d in (self.data, self.sb_data):
            pid_file = d / "postmaster.pid"
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text().splitlines()[0]), 9)
                except (ProcessLookupError, ValueError):
                    pass
        time.sleep(0.5)
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.sb_data, ignore_errors=True)
        self._clean_sockets()

    def rebuild_from_backup(self, db: str, dump: Path):
        """Baseline recovery path: new cluster + pg_restore of the last logical backup."""
        self.start()
        self.restore(db, dump)

    # ------------------------------------------------------------------ streaming replication
    def start_standby(self):
        """Create (if needed) and start a hot standby that streams WAL from the primary."""
        if not (self.sb_data / "PG_VERSION").exists():
            if self.sb_data.exists():
                shutil.rmtree(self.sb_data)
            self._run(["pg_basebackup", "-h", "127.0.0.1", "-p", str(self.port), "-U", "uni",
                       "-D", str(self.sb_data), "-R", "-X", "stream", "-c", "fast"])
            with open(self.sb_data / "postgresql.conf", "a") as f:
                f.write(f"\nport = {self.standby_port}\n")
        self._run(["pg_ctl", "-D", str(self.sb_data), "-l", str(self.workdir / "pg_standby.log"), "-w", "start"])

    def stop_standby(self, mode="fast"):
        self._run(["pg_ctl", "-D", str(self.sb_data), "-m", mode, "-w", "stop"], check=False)

    def promote_standby(self):
        self._run(["pg_ctl", "-D", str(self.sb_data), "-w", "promote"])

    def drop_standby(self):
        self.stop_standby("immediate")
        shutil.rmtree(self.sb_data, ignore_errors=True)

    def wait_standby_caught_up(self, timeout=20.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            lag = self.replication_lag_bytes()
            if lag == 0:
                return True
            time.sleep(0.2)
        return False

    def replication_lag_bytes(self, db: str = "postgres") -> int | None:
        try:
            with psycopg.connect(self.dsn(db), autocommit=True) as c:
                r = c.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) FROM pg_stat_replication").fetchone()
                return int(r[0]) if r and r[0] is not None else None
        except Exception:
            return None

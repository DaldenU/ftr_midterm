"""PostgreSQL access layer.

BASELINE mode (``Database`` with ``settings.mode == 'baseline'``)
    * opens a brand-new connection for every operation (no pool),
    * no timeout other than a long connect timeout, no retry, no breaker,
    * any error propagates straight to the caller.

FT mode
    * bounded connection pool (own implementation, see ``SimplePool``) with
      short connect timeout and statement timeout,
    * transient connection errors are retried with exponential backoff inside a
      per-request deadline (``db_retry_deadline_s``),
    * a circuit breaker around the database lets read paths fail fast (and fall
      back to cached data = graceful degradation) while write paths keep
      retrying until the deadline,
    * multi-host DSNs are supported, so a promoted standby is found
      automatically (``host=a,b port=1,2 target_session_attrs=read-write``).

``run(fn)`` re-executes ``fn`` after a connection failure, therefore ``fn`` must
be idempotent (every write path in this project is protected by a unique key).
"""
from __future__ import annotations

import collections
import contextlib
import threading
import time
from typing import Callable, TypeVar

import psycopg
from psycopg.rows import dict_row

from .config import Settings
from .logging import current_state
from .resilience import CircuitBreaker, CircuitOpenError, retry_call

T = TypeVar("T")

TRANSIENT = (psycopg.OperationalError, psycopg.InterfaceError)


class DatabaseUnavailable(Exception):
    """The database could not be reached within the retry budget."""


class PoolExhausted(Exception):
    pass


class SimplePool:
    """Small thread-safe connection pool.

    Written by hand (instead of psycopg_pool) so that reconnection behaviour is
    explicit and measurable: a broken connection is dropped on use and the next
    borrower opens a fresh one with a short ``connect_timeout``.
    """

    def __init__(self, dsn: str, size: int, borrow_timeout: float, connect_timeout: float = 2.0,
                 statement_timeout_ms: int = 5000):
        self._dsn = dsn
        self._sem = threading.BoundedSemaphore(size)
        self._idle: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._borrow_timeout = borrow_timeout
        self._kw = dict(connect_timeout=connect_timeout, row_factory=dict_row,
                        options=f"-c statement_timeout={statement_timeout_ms}")

    def _new(self):
        return psycopg.connect(self._dsn, **self._kw)

    @contextlib.contextmanager
    def connection(self):
        if not self._sem.acquire(timeout=self._borrow_timeout):
            raise PoolExhausted("no free database connection")
        conn = None
        try:
            with self._lock:
                while self._idle:
                    c = self._idle.popleft()
                    if not c.closed and not c.broken:
                        conn = c
                        break
            if conn is None:
                conn = self._new()
            yield conn
            conn.commit()
            with self._lock:
                self._idle.append(conn)
        except BaseException:
            if conn is not None:
                try:
                    if conn.closed or conn.broken:
                        raise RuntimeError
                    conn.rollback()
                    with self._lock:
                        self._idle.append(conn)
                except Exception:
                    with contextlib.suppress(Exception):
                        conn.close()
            raise
        finally:
            self._sem.release()

    def close(self):
        with self._lock:
            while self._idle:
                with contextlib.suppress(Exception):
                    self._idle.popleft().close()


class Database:
    def __init__(self, settings: Settings, metrics, logger, db_breaker_reset_s: float = 1.0):
        self.s = settings
        self.m = metrics
        self.log = logger
        self.last_ok = 0.0
        if settings.is_ft:
            self.pool = SimplePool(settings.database_url, settings.db_pool_size, settings.db_pool_timeout_s)
            self.breaker = CircuitBreaker("database", failure_threshold=3, reset_timeout=db_breaker_reset_s,
                                          on_state_change=self._breaker_changed)
        else:
            self.pool = None
            self.breaker = None

    # ------------------------------------------------------------------
    def _breaker_changed(self, name, old, new):
        self.m.breaker_state.labels(self.s.service_name, self.s.instance_id, name).set(
            {"closed": 0, "half_open": 1, "open": 2}[new])
        if old != "closed" and new == "closed":
            self.m.recovery_events.labels(self.s.service_name, self.s.instance_id, "db_reconnected").inc()

    def _mark(self, ok: bool):
        self.m.db_up.labels(self.s.service_name, self.s.instance_id).set(1 if ok else 0)
        if ok:
            self.last_ok = time.time()

    # ------------------------------------------------------------------
    def run(self, fn: Callable[[psycopg.Connection], T], *, deadline: float | None = None,
            fail_fast: bool = False, autocommit: bool = False) -> T:
        """Execute ``fn(conn)`` in one transaction (commit on success).

        ``deadline``   FT only; retry budget in seconds (default settings value).
        ``fail_fast``  FT only; raise DatabaseUnavailable at once if the DB
                       breaker is open (used by degradable read paths).
        ``autocommit`` baseline only; run statements without a transaction.
        """
        if not self.s.is_ft:
            return self._run_baseline(fn, autocommit)
        return self._run_ft(fn, deadline if deadline is not None else self.s.db_retry_deadline_s, fail_fast)

    def _run_baseline(self, fn, autocommit):
        conn = psycopg.connect(self.s.database_url, connect_timeout=30, row_factory=dict_row, autocommit=autocommit)
        try:
            res = fn(conn)
            if not autocommit:
                conn.commit()
            self._mark(True)
            return res
        except TRANSIENT:
            self._mark(False)
            raise
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def _run_ft(self, fn, deadline, fail_fast):
        st = current_state()

        def attempt():
            if not self.breaker.allow():
                if fail_fast:
                    raise DatabaseUnavailable("database breaker open")
                raise CircuitOpenError("database breaker open")
            try:
                with self.pool.connection() as conn:
                    res = fn(conn)
            except TRANSIENT:
                self.breaker.record_failure()
                self._mark(False)
                raise
            self.breaker.record_success()
            self._mark(True)
            return res

        def on_retry(n, exc, delay):
            st.retries += 1
            self.m.retries.labels(self.s.service_name, self.s.instance_id, "database").inc()

        try:
            return retry_call(
                attempt, attempts=0, base_delay=0.1, max_delay=0.5, jitter=0.2,
                retry_on=TRANSIENT + (CircuitOpenError,), give_up_on=(DatabaseUnavailable, PoolExhausted),
                deadline=deadline, on_retry=on_retry)
        except TRANSIENT + (CircuitOpenError,) as exc:
            raise DatabaseUnavailable(str(exc)) from exc

    # ------------------------------------------------------------------
    def ping(self, timeout: float = 1.0) -> bool:
        """Single, non-retried liveness probe of the database (used by /ready)."""
        try:
            with psycopg.connect(self.s.database_url, connect_timeout=max(1, int(timeout)), autocommit=True) as c:
                c.execute("SELECT 1")
            self._mark(True)
            return True
        except Exception:
            self._mark(False)
            return False

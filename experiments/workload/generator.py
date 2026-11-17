"""Workload generator (open-loop and closed-loop) and per-request recorder.

Every request is recorded with timestamps so that failed / successful /
recovered requests can be computed per time window afterwards.

Definitions used throughout the project
  success    HTTP 2xx
  failed     HTTP >= 400 (except as stated), connection error or client timeout
  recovered  a request that SUCCEEDED although a fault had to be masked on its
             path: server-side retry (X-Retry-Count>0), gateway failover
             (X-Gateway-Attempts>1) or a client retry that finally succeeded
  degraded   success served with reduced functionality (X-Degraded header)
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import httpx

DEFAULT_MIX = {"get_student": 45, "get_transcript": 20, "post_payment": 15, "post_grade": 15, "get_account": 5}


@dataclass
class Rec:
    op: str
    t0: float                 # scheduled start (epoch)
    t1: float = 0.0           # completion (epoch)
    status: int = 0           # HTTP status, 0 = connection error, -1 = client timeout
    latency_ms: float = 0.0
    retries: int = 0          # server-side retries (X-Retry-Count)
    gw_attempts: int = 1
    degraded: str = ""
    replay: bool = False
    served_by: str = ""
    attempt: int = 1          # client attempt number for this logical operation
    logical_id: str = ""
    phase: str = ""
    error: str = ""
    payment_id: str = ""
    grade_key: str = ""
    shed: bool = False         # deliberate load shedding: 503 + Retry-After
    request_id: str = ""       # client-generated X-Request-ID (trace id), known even if the request fails

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Recorder:
    def __init__(self):
        self.recs: list[Rec] = []
        self._lock = threading.Lock()

    def add(self, r: Rec):
        with self._lock:
            self.recs.append(r)

    def snapshot(self) -> list[Rec]:
        with self._lock:
            return list(self.recs)

    def dump_jsonl(self, path):
        with open(path, "w") as f:
            for r in self.snapshot():
                f.write(json.dumps(asdict(r)) + "\n")


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarize(recs: list[Rec]) -> dict:
    """Counts + latency percentiles for a list of records."""
    total = len(recs)
    ok = [r for r in recs if r.ok]
    failed = total - len(ok)
    recovered = [r for r in ok if r.retries > 0 or r.gw_attempts > 1 or r.attempt > 1]
    lat_ok = [r.latency_ms for r in ok]
    lat_all = [r.latency_ms for r in recs]
    return {
        "total": total, "success": len(ok), "failed": failed,
        "error_rate": round(failed / total, 4) if total else 0.0,
        "shed": sum(1 for r in recs if r.shed),
        "hard_failed": failed - sum(1 for r in recs if r.shed),
        "hard_error_rate": round((failed - sum(1 for r in recs if r.shed)) / total, 4) if total else 0.0,
        "recovered": len(recovered),
        "degraded": sum(1 for r in ok if r.degraded),
        "replays": sum(1 for r in recs if r.replay),
        "p50_ms": round(pct(lat_ok, 50), 1), "p95_ms": round(pct(lat_ok, 95), 1), "p99_ms": round(pct(lat_ok, 99), 1),
        "p95_all_ms": round(pct(lat_all, 95), 1),
    }


class Client:
    """Thread-safe-ish helper: one httpx.Client per thread.

    ``security`` (a SecurityFixture) makes every request carry the JWT of a principal that is
    legitimately allowed to perform it (the student for own records/payments, a random
    faculty member for grades). In baseline/ft mode the header is simply not sent.
    """

    def __init__(self, base_url: str, timeout: float = 5.0, students: int = 50, seed: int | None = None,
                 security=None):
        self.base_url, self.timeout, self.students = base_url, timeout, students
        self.security = security
        self._tl = threading.local()
        self.rng = random.Random(seed)
        self._seq = 0
        self._lock = threading.Lock()
        self.run_tag = uuid.uuid4().hex[:6]

    def http(self) -> httpx.Client:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = httpx.Client(base_url=self.base_url, timeout=self.timeout,
                             limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
            self._tl.c = c
        return c

    def next_id(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def sid(self) -> str:
        return f"S{self.rng.randint(1, self.students):04d}"

    def auth_for(self, op: str, sid: str, rng: random.Random | None = None) -> dict:
        if self.security is None:
            return {}
        if op in ("post_grade",):
            return self.security.auth(f"FAC{(rng or self.rng).randint(1, 20):02d}")
        if op in ("post_batch",):
            return self.security.auth("REG01")
        return self.security.auth(sid)

    # ---------------------------------------------------------------- one HTTP exchange
    def call(self, rec: Rec, method: str, path: str, **kw) -> Rec:
        rec.request_id = rec.request_id or uuid.uuid4().hex[:12]
        kw["headers"] = {**(kw.get("headers") or {}), "X-Request-ID": rec.request_id}
        try:
            r = self.http().request(method, path, **kw)
            rec.status = r.status_code
            rec.retries = int(r.headers.get("x-retry-count", 0))
            rec.gw_attempts = int(r.headers.get("x-gateway-attempts", 1))
            rec.degraded = r.headers.get("x-degraded", "")
            rec.replay = r.headers.get("idempotent-replay") == "true"
            rec.served_by = r.headers.get("x-served-by", "")
            rec.shed = r.status_code == 503 and "retry-after" in r.headers
            if 200 <= r.status_code < 300 and method == "POST":
                try:
                    j = r.json()
                    rec.payment_id = j.get("payment_id", "") if isinstance(j, dict) else ""
                except ValueError:
                    pass
            elif r.status_code >= 400:
                rec.error = f"HTTP {r.status_code}"
        except httpx.TimeoutException:
            rec.status, rec.error = -1, "client_timeout"
        except httpx.HTTPError as exc:
            rec.status, rec.error = 0, type(exc).__name__
        rec.t1 = time.time()
        rec.latency_ms = round((rec.t1 - rec.t0) * 1000, 2)   # includes any scheduling delay (open loop)
        return rec

    # ---------------------------------------------------------------- operations
    def do_op(self, op: str, rec: Rec) -> Rec:
        n = self.next_id()
        sid = self.sid()
        h = self.auth_for(op, sid)
        if op == "get_student":
            return self.call(rec, "GET", f"/students/{sid}", headers=h)
        if op == "get_transcript":
            return self.call(rec, "GET", f"/students/{sid}/transcript", headers=h)
        if op == "get_account":
            return self.call(rec, "GET", f"/accounts/{sid}", headers=h)
        if op == "post_payment":
            rec.logical_id = f"INV-{self.run_tag}-{n}"
            body = {"student_id": sid, "invoice_id": rec.logical_id, "amount": 1000.0}
            return self.call(rec, "POST", "/payments", json=body,
                             headers={"Idempotency-Key": f"key-{self.run_tag}-{n}", **h})
        if op == "post_grade":
            course, term = f"C{self.run_tag}{n}", "2026F"
            rec.grade_key = f"{sid}|{course}|{term}"
            rec.logical_id = rec.grade_key
            return self.call(rec, "POST", "/grades", json={"student_id": sid, "course_code": course, "term": term, "grade": 75.0},
                             headers=h)
        raise ValueError(op)


class Workload:
    def __init__(self, client: Client, recorder: Recorder, mix: dict | None = None, seed: int = 1):
        self.client, self.recorder = client, recorder
        self.mix = mix or DEFAULT_MIX
        self.rng = random.Random(seed)
        self.phase = ""
        self._ops, self._w = list(self.mix), list(self.mix.values())
        self._stop = threading.Event()

    def pick(self) -> str:
        return self.rng.choices(self._ops, self._w)[0]

    def _one(self, op: str, t0: float):
        rec = Rec(op=op, t0=t0, phase=self.phase)
        self.client.do_op(op, rec)
        self.recorder.add(rec)

    # ------------------------------------------------------------ open loop
    def run_open_loop(self, duration: float, rate: float, workers: int = 300, on_tick=None):
        """Issue ``rate`` requests/s for ``duration`` s regardless of response times."""
        start = time.time()
        n = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wl") as pool:
            while not self._stop.is_set():
                t_sched = start + n / rate
                if t_sched - start >= duration:
                    break
                delay = t_sched - time.time()
                if delay > 0:
                    time.sleep(delay)
                pool.submit(self._one, self.pick(), t_sched)
                n += 1
                if on_tick:
                    on_tick(time.time() - start)
        return n

    # ------------------------------------------------------------ closed loop
    def run_closed_loop(self, concurrency: int, duration: float):
        end = time.time() + duration

        def worker(seed):
            rng = random.Random(seed)
            while time.time() < end and not self._stop.is_set():
                op = rng.choices(self._ops, self._w)[0]
                self._one(op, time.time())

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(concurrency)]
        [t.start() for t in threads]
        [t.join() for t in threads]

    # ------------------------------------------------------------ closed loop (asyncio)
    def run_closed_loop_async(self, concurrency: int, duration: float):
        """Same as run_closed_loop but with one event loop instead of N threads, so the load generator
        itself (GIL contention) does not become the bottleneck at high concurrency."""
        c = self.client

        import aiohttp

        async def one(ac: "aiohttp.ClientSession", op: str, rng: random.Random):
            rec = Rec(op=op, t0=time.time(), phase=self.phase)
            n = c.next_id()
            sid = f"S{rng.randint(1, c.students):04d}"
            method, path, kw = "GET", "", {"headers": c.auth_for(op, sid, rng)}
            if op == "get_student":
                path = f"/students/{sid}"
            elif op == "get_transcript":
                path = f"/students/{sid}/transcript"
            elif op == "get_account":
                path = f"/accounts/{sid}"
            elif op == "post_payment":
                rec.logical_id = f"INV-{c.run_tag}-{n}"
                method, path = "POST", "/payments"
                kw = dict(json={"student_id": sid, "invoice_id": rec.logical_id, "amount": 1000.0},
                          headers={"Idempotency-Key": f"key-{c.run_tag}-{n}", **kw["headers"]})
            else:
                rec.grade_key = rec.logical_id = f"{sid}|C{c.run_tag}{n}|2026F"
                method, path = "POST", "/grades"
                kw = dict(json={"student_id": sid, "course_code": f"C{c.run_tag}{n}", "term": "2026F", "grade": 75.0},
                          headers=kw["headers"])
            retry_after = 0.0
            rec.request_id = uuid.uuid4().hex[:12]
            kw["headers"] = {**(kw.get("headers") or {}), "X-Request-ID": rec.request_id}
            try:
                async with ac.request(method, c.base_url + path, **kw) as r:
                    body = await r.read()
                    rec.status = r.status
                    h = r.headers
                    rec.retries = int(h.get("x-retry-count", 0))
                    rec.gw_attempts = int(h.get("x-gateway-attempts", 1))
                    rec.degraded = h.get("x-degraded", "")
                    rec.replay = h.get("idempotent-replay") == "true"
                    rec.shed = r.status == 503 and "retry-after" in h
                    if method == "POST" and 200 <= r.status < 300:
                        rec.payment_id = json.loads(body).get("payment_id", "")
                    if r.status == 503 and h.get("retry-after"):
                        retry_after = min(float(h["retry-after"]), 2.0)   # well-behaved client honours Retry-After
            except asyncio.TimeoutError:
                rec.status, rec.error = -1, "client_timeout"
            except aiohttp.ClientError as exc:
                rec.status, rec.error = 0, type(exc).__name__
            rec.t1 = time.time()
            rec.latency_ms = round((rec.t1 - rec.t0) * 1000, 2)
            self.recorder.add(rec)
            if retry_after:
                await asyncio.sleep(retry_after)

        async def worker(ac, seed, end):
            rng = random.Random(seed)
            while time.time() < end and not self._stop.is_set():
                await one(ac, rng.choices(self._ops, self._w)[0], rng)

        async def main():
            conn = aiohttp.TCPConnector(limit=concurrency + 10, force_close=False)
            async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=c.timeout)) as ac:
                end = time.time() + duration
                await asyncio.gather(*(worker(ac, i, end) for i in range(concurrency)))

        asyncio.run(main())

    def stop(self):
        self._stop.set()

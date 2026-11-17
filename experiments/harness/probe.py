"""Black-box availability probes and the external readiness monitor.

Prober        user-visible availability: GET through the gateway per service at 10 Hz.
ReadyMonitor  external health monitor (stand-in for Prometheus blackbox/up checks):
              polls /ready of every instance directly at 5 Hz.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

PROBES = {"student": "/students/S0001", "payment": "/accounts/S0001", "academic": "/students/S0001/transcript"}


@dataclass
class Sample:
    t: float
    svc: str
    ok: bool
    degraded: bool
    latency_ms: float


class Prober:
    def __init__(self, gateway_url: str, interval: float = 0.1, timeout: float = 1.0, headers: dict | None = None):
        self.url, self.interval, self.timeout = gateway_url, interval, timeout
        self.headers = headers or {}
        self.samples: list[Sample] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self):
        for svc, path in PROBES.items():
            t = threading.Thread(target=self._loop, args=(svc, path), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        [t.join(2) for t in self._threads]

    def _loop(self, svc, path):
        c = httpx.Client(base_url=self.url, timeout=self.timeout, headers=self.headers)
        while not self._stop.is_set():
            t0 = time.time()
            ok, deg = False, False
            try:
                r = c.get(path)
                ok = r.status_code == 200
                deg = bool(r.headers.get("x-degraded")) or (ok and '"degraded":"' in r.text.replace(" ", ""))
            except httpx.HTTPError:
                pass
            s = Sample(t0, svc, ok, deg, (time.time() - t0) * 1000)
            with self._lock:
                self.samples.append(s)
            time.sleep(max(0.0, self.interval - (time.time() - t0)))
        c.close()

    # ------------------------------------------------------------ analysis
    def _svc(self, svc, t_from=0.0, t_to=1e18):
        with self._lock:
            return [s for s in self.samples if s.svc == svc and t_from <= s.t <= t_to]

    def availability(self, svc: str, t_from: float, t_to: float, strict: bool = True) -> float:
        xs = self._svc(svc, t_from, t_to)
        if not xs:
            return float("nan")
        good = sum(1 for s in xs if s.ok and (not strict or not s.degraded))
        return good / len(xs)

    def outages(self, svc: str, t_from: float, t_to: float, strict: bool = True, min_samples: int = 2):
        """[(start, end)] intervals during which consecutive probes were not (strictly) OK."""
        out, start, bad = [], None, 0
        xs = self._svc(svc, t_from, t_to)
        for s in xs:
            good = s.ok and (not strict or not s.degraded)
            if not good:
                if start is None:
                    start, bad = s.t, 0
                bad += 1
            elif start is not None:
                if bad >= min_samples:
                    out.append((start, s.t))
                start = None
        if start is not None and bad >= min_samples and xs:
            out.append((start, xs[-1].t))
        return out

    def downtime(self, svc, t_from, t_to, strict=True) -> float:
        return sum(e - s for s, e in self.outages(svc, t_from, t_to, strict))

    def first_failure_after(self, t: float, svcs=None, strict=True):
        with self._lock:
            xs = [s for s in self.samples if s.t >= t and (svcs is None or s.svc in svcs)
                  and not (s.ok and (not strict or not s.degraded))]
        return min((s.t for s in xs), default=None)

    def full_ok_since(self, t_after: float, svcs=None, k: int = 5):
        """First time >= t_after from which every listed service has k consecutive strictly-OK probes."""
        svcs = svcs or list(PROBES)
        result = 0.0
        for svc in svcs:
            xs = self._svc(svc, t_after)
            run, found = 0, None
            for idx, s in enumerate(xs):
                run = run + 1 if (s.ok and not s.degraded) else 0
                if run >= k:
                    found = xs[idx - k + 1].t
                    break
            if found is None:
                return None
            result = max(result, found)
        return result


class ReadyMonitor:
    """Polls /ready of every instance; keeps transition timestamps."""

    def __init__(self, urls: dict[str, str], interval: float = 0.2, timeout: float = 0.4):
        self.urls, self.interval, self.timeout = urls, interval, timeout
        self.samples: list[tuple[float, str, bool]] = []
        self._stop = threading.Event()
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(2)

    def _loop(self):
        c = httpx.Client(timeout=self.timeout)
        while not self._stop.is_set():
            t0 = time.time()
            for name, url in self.urls.items():
                try:
                    ok = c.get(url + "/ready").status_code == 200
                except httpx.HTTPError:
                    ok = False
                self.samples.append((time.time(), name, ok))
            time.sleep(max(0.0, self.interval - (time.time() - t0)))
        c.close()

    def first_unhealthy_after(self, t: float, names=None):
        return min((s[0] for s in self.samples if s[0] >= t and not s[2] and (names is None or s[1] in names)), default=None)

    def first_healthy_after(self, t: float, names=None):
        """First time after t at which ALL listed names were healthy in a poll round."""
        names = names or list(self.urls)
        latest = None
        for name in names:
            h = next((s[0] for s in self.samples if s[0] >= t and s[2] and s[1] == name), None)
            if h is None:
                return None
            latest = max(latest or 0, h)
        return latest

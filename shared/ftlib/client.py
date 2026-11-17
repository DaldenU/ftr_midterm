"""Service-to-service HTTP client.

BASELINE: first URL only, 30 s timeout, no retry, no breaker - a failing or
slow dependency propagates to the caller unchanged.

FT: per-attempt timeout, retry with exponential backoff, failover across the
replica URLs, and a circuit breaker so a dead dependency is not hammered.
4xx answers are *business* results, not failures, and neither trip the breaker
nor are retried.
"""
from __future__ import annotations

import itertools
import threading
import uuid

import httpx

from .config import Settings
from .logging import current_state
from .resilience import CircuitBreaker, CircuitOpenError, retry_call
from . import security as sec


class ServiceUnavailable(Exception):
    """Dependency failed after all fault-tolerance measures (or breaker open)."""


class _Transient(Exception):
    def __init__(self, msg):
        super().__init__(msg)


class ServiceClient:
    def __init__(self, name: str, urls: list[str], settings: Settings, metrics, logger):
        self.name, self.urls, self.s, self.m, self.log = name, urls, settings, metrics, logger
        self._rr = itertools.count()
        self._lock = threading.Lock()
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
        timeout = settings.http_timeout_s if settings.is_ft else 30.0
        self.http = httpx.Client(timeout=timeout, limits=limits)
        self.breaker = None
        if settings.is_ft:
            self.breaker = CircuitBreaker(
                name, failure_threshold=settings.breaker_failure_threshold,
                reset_timeout=settings.breaker_reset_timeout_s, on_state_change=self._changed)

    def _changed(self, name, old, new):
        self.m.breaker_state.labels(self.s.service_name, self.s.instance_id, name).set(
            {"closed": 0, "half_open": 1, "open": 2}[new])
        self.log.warning("breaker_state_change", extra={"fields": {
            "operation": "circuit_breaker", "target": name, "from": old, "to": new}})
        if old != "closed" and new == "closed":
            self.m.recovery_events.labels(self.s.service_name, self.s.instance_id, "breaker_closed").inc()

    def _headers(self, request_id: str) -> dict:
        st = current_state()
        rid = request_id or st.request_id or uuid.uuid4().hex[:12]   # background jobs get their own trace id
        h = {"X-Request-ID": rid}
        if st.span_id:
            h["X-Parent-Span-ID"] = st.span_id            # distributed trace: caller span -> callee parent
        if self.s.is_secure:                              # C3: authenticated service identity
            h["X-Internal-Auth"] = sec.sign_internal(self.s.internal_auth_secret, self.s.service_name, rid or "")
        return h

    def get(self, path: str, request_id: str = "") -> httpx.Response:
        headers = self._headers(request_id)
        if not self.s.is_ft:
            # naive: single endpoint, long timeout, exceptions propagate
            return self.http.get(self.urls[0] + path, headers=headers)

        st = current_state()
        start_idx = next(self._rr)
        counter = itertools.count(start_idx)

        def one_attempt() -> httpx.Response:
            url = self.urls[next(counter) % len(self.urls)]   # failover to next replica on retry
            if self.s.is_secure:                                # fresh timestamp per attempt (replay window)
                headers["X-Internal-Auth"] = sec.sign_internal(self.s.internal_auth_secret, self.s.service_name,
                                                               headers["X-Request-ID"])
            try:
                r = self.http.get(url + path, headers=headers)
            except httpx.HTTPError as exc:
                raise _Transient(f"{type(exc).__name__}") from exc
            if r.status_code >= 500:
                raise _Transient(f"HTTP {r.status_code}")
            return r

        def on_retry(n, exc, delay):
            st.retries += 1
            self.m.retries.labels(self.s.service_name, self.s.instance_id, self.name).inc()

        def guarded_attempt() -> httpx.Response:
            # the breaker sits INSIDE the retry loop: every attempt is counted, and an open
            # breaker aborts the remaining retries immediately (fail fast, protect the dependency)
            return self.breaker.call(one_attempt, failure_types=(_Transient,))

        try:
            return retry_call(
                guarded_attempt, attempts=self.s.retry_attempts, base_delay=self.s.retry_base_delay_s,
                max_delay=self.s.retry_max_delay_s, jitter=0.2, retry_on=(_Transient,),
                give_up_on=(CircuitOpenError,), deadline=self.s.downstream_deadline_s, on_retry=on_retry)
        except (_Transient, CircuitOpenError) as exc:
            raise ServiceUnavailable(f"{self.name}: {exc}") from exc

    def state(self) -> dict:
        return self.breaker.snapshot() if self.breaker else {"name": self.name, "state": "n/a (baseline)"}

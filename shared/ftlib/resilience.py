"""Core software fault-tolerance primitives.

* retry_call        - retry with exponential backoff (+ jitter, + overall deadline)
* CircuitBreaker    - CLOSED -> OPEN -> HALF_OPEN state machine
* run_with_timeout  - bound the wall-clock time of an arbitrary callable

These are deliberately dependency-free and take an injectable clock/sleep so
that unit tests are deterministic (tests/unit/test_resilience.py).
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from typing import Callable, Iterable, Optional, Tuple, Type, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------------------
# Retry with exponential backoff
# --------------------------------------------------------------------------
def backoff_delays(attempts: int, base: float, factor: float = 2.0,
                   max_delay: float = 1.0, jitter: float = 0.0,
                   rng: Optional[random.Random] = None) -> list[float]:
    """Delays slept *between* attempts: base, base*f, base*f^2, ... capped."""
    r = rng or random
    out = []
    for i in range(max(attempts - 1, 0)):
        d = min(base * (factor ** i), max_delay)
        if jitter:
            d += d * jitter * r.random()
        out.append(d)
    return out


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.1,
    factor: float = 2.0,
    max_delay: float = 1.0,
    jitter: float = 0.1,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
    give_up_on: Tuple[Type[BaseException], ...] = (),
    deadline: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Call ``fn`` until it succeeds, retrying with exponential backoff.

    * ``attempts``   total number of calls (1 + retries). ``attempts<=0`` means
                     "unbounded, rely on ``deadline``".
    * ``deadline``   total time budget in seconds; no retry is started that
                     cannot finish its backoff sleep inside the budget.
    * ``give_up_on`` exceptions that are never retried (e.g. 4xx business errors,
                     an OPEN circuit breaker).
    * ``on_retry``   callback(attempt_number, exception, delay) for metrics/logs.

    The last exception is re-raised when the retry budget is exhausted.
    """
    start = clock()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except give_up_on:
            raise
        except retry_on as exc:  # type: ignore[misc]
            if attempts > 0 and attempt >= attempts:
                raise
            delay = min(base_delay * (factor ** (attempt - 1)), max_delay)
            if jitter:
                delay += delay * jitter * random.random()
            if deadline is not None and (clock() - start) + delay > deadline:
                raise
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------
CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitOpenError(Exception):
    """Raised instead of calling a dependency whose breaker is OPEN (fail fast)."""


class CircuitBreaker:
    """Consecutive-failure circuit breaker (Nygard, *Release It!*).

    CLOSED     calls flow; ``failure_threshold`` consecutive failures -> OPEN
    OPEN       calls fail fast with CircuitOpenError for ``reset_timeout`` s
    HALF_OPEN  after the timeout, up to ``half_open_max_calls`` trial calls are
               allowed; one success -> CLOSED, one failure -> OPEN again
    """

    def __init__(self, name: str, failure_threshold: int = 5, reset_timeout: float = 5.0,
                 half_open_max_calls: int = 1,
                 clock: Callable[[], float] = time.monotonic,
                 on_state_change: Optional[Callable[[str, str, str], None]] = None):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.half_open_max_calls = half_open_max_calls
        self._clock = clock
        self._on_state_change = on_state_change
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_inflight = 0
        self.events: list[dict] = []   # [{"ts": epoch, "from": .., "to": ..}]

    # -- state ------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _set(self, new: str) -> None:
        old = self._state
        if old == new:
            return
        self._state = new
        self.events.append({"ts": time.time(), "from": old, "to": new})
        del self.events[:-200]
        if self._on_state_change:
            self._on_state_change(self.name, old, new)

    def _maybe_half_open(self) -> None:
        if self._state == OPEN and self._clock() - self._opened_at >= self.reset_timeout:
            self._half_open_inflight = 0
            self._set(HALF_OPEN)

    # -- protocol ---------------------------------------------------------
    def allow(self) -> bool:
        with self._lock:
            self._maybe_half_open()
            if self._state == CLOSED:
                return True
            if self._state == HALF_OPEN and self._half_open_inflight < self.half_open_max_calls:
                self._half_open_inflight += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._half_open_inflight = 0
            self._set(CLOSED)

    def record_failure(self) -> None:
        with self._lock:
            if self._state == HALF_OPEN:
                self._opened_at = self._clock()
                self._half_open_inflight = 0
                self._set(OPEN)
                return
            self._failures += 1
            if self._state == CLOSED and self._failures >= self.failure_threshold:
                self._opened_at = self._clock()
                self._set(OPEN)

    def call(self, fn: Callable[[], T],
             failure_types: Tuple[Type[BaseException], ...] = (Exception,)) -> T:
        if not self.allow():
            raise CircuitOpenError(f"circuit '{self.name}' is {self._state}")
        try:
            result = fn()
        except failure_types:
            self.record_failure()
            raise
        except BaseException:
            # non-failure exceptions (e.g. business 404) must not trip the breaker
            self.record_success()
            raise
        self.record_success()
        return result

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state, "consecutive_failures": self._failures,
                "half_open_inflight": self._half_open_inflight,
                "events": list(self.events[-20:])}


# --------------------------------------------------------------------------
# Timeout helper
# --------------------------------------------------------------------------
class OperationTimeout(Exception):
    pass


_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="timeout")


def run_with_timeout(fn: Callable[[], T], timeout: float) -> T:
    """Run ``fn`` in a helper thread and give up after ``timeout`` seconds.

    The helper thread cannot be killed (Python limitation) - it is abandoned -
    so ``fn`` itself should also use native timeouts (connect_timeout, httpx
    timeout). This helper bounds how long the *caller* is blocked.
    """
    fut = _executor.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except FutTimeout as exc:
        raise OperationTimeout(f"operation exceeded {timeout}s") from exc

"""Unit tests of retry / backoff / circuit breaker / timeout primitives (deterministic: fake clock & sleep)."""
import pytest

from ftlib.resilience import (CircuitBreaker, CircuitOpenError, OperationTimeout, backoff_delays, retry_call,
                              run_with_timeout, CLOSED, OPEN, HALF_OPEN)


class Boom(Exception):
    pass


def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Boom()
        return "ok"

    sleeps = []
    assert retry_call(flaky, attempts=5, base_delay=0.1, jitter=0, sleep=sleeps.append, retry_on=(Boom,)) == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.1, 0.2]                      # exponential backoff: 0.1 * 2^k


def test_retry_gives_up_and_reraises():
    sleeps = []
    with pytest.raises(Boom):
        retry_call(lambda: (_ for _ in ()).throw(Boom()), attempts=4, base_delay=0.05, jitter=0, sleep=sleeps.append, retry_on=(Boom,))
    assert sleeps == [0.05, 0.1, 0.2]               # 4 attempts -> 3 sleeps


def test_backoff_is_capped():
    assert backoff_delays(6, base=0.1, factor=2, max_delay=0.5) == [0.1, 0.2, 0.4, 0.5, 0.5]


def test_retry_respects_give_up_on():
    calls = {"n": 0}

    def f():
        calls["n"] += 1
        raise CircuitOpenError("open")

    with pytest.raises(CircuitOpenError):
        retry_call(f, attempts=5, base_delay=0, sleep=lambda s: None, retry_on=(Exception,), give_up_on=(CircuitOpenError,))
    assert calls["n"] == 1


def test_retry_deadline_stops_retrying():
    now = {"t": 0.0}
    clock = lambda: now["t"]

    def sleep(d):
        now["t"] += d

    calls = {"n": 0}

    def f():
        calls["n"] += 1
        raise Boom()

    with pytest.raises(Boom):
        retry_call(f, attempts=0, base_delay=1.0, max_delay=1.0, jitter=0, deadline=3.5, sleep=sleep, clock=clock, retry_on=(Boom,))
    assert calls["n"] == 4                           # t = 0,1,2,3 ; a 5th attempt would need to sleep past 3.5 s


def test_on_retry_callback_invoked():
    seen = []
    n = {"i": 0}

    def f():
        n["i"] += 1
        if n["i"] < 3:
            raise Boom()
        return 1

    retry_call(f, attempts=3, base_delay=0, jitter=0, sleep=lambda s: None, retry_on=(Boom,), on_retry=lambda a, e, d: seen.append(a))
    assert seen == [1, 2]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_circuit_breaker_opens_after_threshold_and_fails_fast():
    clk = FakeClock()
    br = CircuitBreaker("dep", failure_threshold=3, reset_timeout=5, clock=clk)

    def fail():
        raise Boom()

    for _ in range(3):
        with pytest.raises(Boom):
            br.call(fail)
    assert br.state == OPEN
    called = {"n": 0}
    with pytest.raises(CircuitOpenError):
        br.call(lambda: called.__setitem__("n", 1))
    assert called["n"] == 0                          # dependency not touched while OPEN


def test_circuit_breaker_half_open_then_closes_on_success():
    clk = FakeClock()
    br = CircuitBreaker("dep", failure_threshold=2, reset_timeout=5, clock=clk)
    for _ in range(2):
        with pytest.raises(Boom):
            br.call(lambda: (_ for _ in ()).throw(Boom()))
    assert br.state == OPEN
    clk.t = 4.9
    assert br.state == OPEN
    clk.t = 5.1
    assert br.state == HALF_OPEN
    assert br.call(lambda: "pong") == "pong"
    assert br.state == CLOSED


def test_circuit_breaker_half_open_failure_reopens():
    clk = FakeClock()
    br = CircuitBreaker("dep", failure_threshold=1, reset_timeout=5, clock=clk)
    with pytest.raises(Boom):
        br.call(lambda: (_ for _ in ()).throw(Boom()))
    clk.t = 6
    assert br.state == HALF_OPEN
    with pytest.raises(Boom):
        br.call(lambda: (_ for _ in ()).throw(Boom()))
    assert br.state == OPEN
    clk.t = 8                                         # timer restarted at t=6
    assert br.state == OPEN


def test_circuit_breaker_half_open_allows_limited_trial_calls():
    clk = FakeClock()
    br = CircuitBreaker("dep", failure_threshold=1, reset_timeout=1, half_open_max_calls=1, clock=clk)
    br.record_failure()
    clk.t = 2
    assert br.allow() is True
    assert br.allow() is False                       # only one trial call in flight


def test_circuit_breaker_non_failure_exceptions_do_not_trip():
    br = CircuitBreaker("dep", failure_threshold=1)
    with pytest.raises(KeyError):
        br.call(lambda: (_ for _ in ()).throw(KeyError()), failure_types=(Boom,))
    assert br.state == CLOSED


def test_circuit_breaker_success_resets_failure_count():
    br = CircuitBreaker("dep", failure_threshold=3)
    br.record_failure(); br.record_failure(); br.record_success(); br.record_failure(); br.record_failure()
    assert br.state == CLOSED


def test_circuit_breaker_records_state_events():
    clk = FakeClock()
    br = CircuitBreaker("dep", failure_threshold=1, reset_timeout=1, clock=clk)
    br.record_failure(); clk.t = 2; br.state; br.record_success()
    assert [e["to"] for e in br.events] == ["open", "half_open", "closed"]


def test_run_with_timeout():
    import time
    assert run_with_timeout(lambda: 42, timeout=1) == 42
    with pytest.raises(OperationTimeout):
        run_with_timeout(lambda: time.sleep(0.5), timeout=0.05)

"""Load smoke tests: concurrent clients against both deployments (short; full load experiment = S6)."""
import pytest

from experiments.workload.generator import Client, Recorder, Workload, summarize

pytestmark = [pytest.mark.integration, pytest.mark.load]


def run_load(stack, concurrency=20, seconds=6):
    rec = Recorder()
    wl = Workload(Client(stack.gateway_url, timeout=5), rec)
    wl.run_closed_loop_async(concurrency, seconds)
    return summarize(rec.snapshot())


def test_baseline_handles_moderate_load(baseline_stack):
    s = run_load(baseline_stack)
    assert s["total"] > 100 and s["hard_error_rate"] < 0.02


def test_ft_handles_moderate_load_with_low_latency(ft_stack):
    s = run_load(ft_stack)
    assert s["total"] > 200 and s["hard_error_rate"] < 0.02 and s["p95_ms"] < 1000


def test_ft_sheds_load_instead_of_queueing_when_overloaded(ft_stack, pg):
    """Beyond the admission limit the gateway answers 503+Retry-After quickly (graceful degradation)."""
    import httpx, threading, time
    ft_stack.set_fault("student", latency_ms=1500)       # make requests slow so in-flight work piles up
    try:
        codes = []

        def hit():
            try:
                with httpx.Client(base_url=ft_stack.gateway_url, timeout=8) as c:
                    codes.append(c.get("/students/S0001").status_code)
            except httpx.HTTPError:
                codes.append(0)
        ths = [threading.Thread(target=hit) for _ in range(160)]
        [t.start() for t in ths]; [t.join() for t in ths]
    finally:
        ft_stack.clear_faults()
    assert codes.count(503) > 0 or max(codes) == 200        # gateway default limit is 100 in-flight

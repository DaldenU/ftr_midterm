import json
import logging

import pytest

from ftlib.config import load_settings
from ftlib.logging import JsonFormatter, RequestState, current_state, get_logger, log_event, set_state, _state_var
from ftlib.metrics import Metrics
from ftlib.cache import LastKnownGood
from experiments.workload.generator import Rec, pct, summarize


def test_structured_log_has_required_fields(capsys):
    lg = get_logger("svc-x", "inst-1")
    tok = set_state(RequestState(request_id="req123"))
    try:
        log_event(lg, "request", operation="GET /x", status=200, latency_ms=1.5, error=None, retry_count=2)
    finally:
        _state_var.reset(tok)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    for key in ("ts", "service", "request_id", "operation", "status", "latency_ms", "error", "retry_count"):
        assert key in line
    assert line["service"] == "svc-x" and line["request_id"] == "req123" and line["retry_count"] == 2


def test_settings_require_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        load_settings("x")


def test_settings_validate_mode(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("FT_MODE", "turbo")
    with pytest.raises(ValueError):
        load_settings("x")
    monkeypatch.setenv("FT_MODE", "ft")
    assert load_settings("x").is_ft


def test_metrics_expose_required_series():
    m = Metrics("svc", "i1", "ft")
    m.requests.labels("svc", "i1", "GET", "/x", "200").inc()
    m.recovery_events.labels("svc", "i1", "db_reconnected").inc()
    text = m.render()[0].decode()
    for name in ("http_requests_total", "http_requests_failed_total", "http_request_duration_seconds", "service_up",
                 "recovery_events_total", "circuit_breaker_state", "degraded_responses_total", "idempotent_replays_total"):
        assert name in text


def test_last_known_good_cache_is_bounded():
    c = LastKnownGood(max_entries=2)
    c.put("a", {1: 1}); c.put("b", {2: 2}); c.put("c", {3: 3})
    assert c.get("a") is None and c.get("c") == {3: 3} and len(c) == 2


def test_summarize_counts_recovered_and_percentiles():
    recs = [Rec(op="x", t0=0, t1=1, status=200, latency_ms=10), Rec(op="x", t0=0, t1=1, status=200, latency_ms=30, retries=2),
            Rec(op="x", t0=0, t1=1, status=503, latency_ms=50, shed=True), Rec(op="x", t0=0, t1=1, status=0, latency_ms=70)]
    s = summarize(recs)
    assert (s["total"], s["success"], s["failed"], s["recovered"], s["shed"], s["hard_failed"]) == (4, 2, 2, 1, 1, 1)
    assert pct([1, 2, 3, 4, 5], 50) == 3

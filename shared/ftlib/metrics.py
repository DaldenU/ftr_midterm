"""Prometheus metrics shared by all services (one registry per process)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST


class Metrics:
    def __init__(self, service: str, instance: str, mode: str):
        self.service, self.instance, self.mode = service, instance, mode
        self.registry = CollectorRegistry()
        lbl = ["service", "instance"]
        self.requests = Counter("http_requests_total", "HTTP requests", lbl + ["method", "route", "status"], registry=self.registry)
        self.success = Counter("http_requests_success_total", "Requests answered with status < 500", lbl, registry=self.registry)
        self.failed = Counter("http_requests_failed_total", "Requests answered with status >= 500", lbl, registry=self.registry)
        self.latency = Histogram("http_request_duration_seconds", "Request latency", lbl + ["route"],
                                 buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10), registry=self.registry)
        self.up = Gauge("service_up", "1 when the process is serving", lbl, registry=self.registry)
        self.db_up = Gauge("db_up", "1 when the last DB operation succeeded", lbl, registry=self.registry)
        self.retries = Counter("retries_total", "Retries performed", lbl + ["target"], registry=self.registry)
        self.breaker_state = Gauge("circuit_breaker_state", "0=closed 1=half_open 2=open", lbl + ["target"], registry=self.registry)
        self.recovery_events = Counter("recovery_events_total", "Recovery actions taken", lbl + ["type"], registry=self.registry)
        self.degraded = Counter("degraded_responses_total", "Responses served in degraded mode", lbl + ["reason"], registry=self.registry)
        self.shed = Counter("load_shed_total", "Requests rejected by load shedding", lbl, registry=self.registry)
        self.replays = Counter("idempotent_replays_total", "Duplicate requests answered from stored result", lbl, registry=self.registry)
        self.security_events = Counter("security_events_total", "Security-relevant events (denials, auth failures)", lbl + ["type"], registry=self.registry)
        self.audit_records = Counter("audit_records_total", "Hash-chained audit records written", lbl + ["outcome"], registry=self.registry)
        self.inflight = Gauge("inflight_requests", "Requests currently in flight", lbl, registry=self.registry)
        self.up.labels(service, instance).set(1)
        self.db_up.labels(service, instance).set(1)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST

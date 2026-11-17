"""Structured JSON logging and request-scoped state.

One JSON object per line on stdout, so Docker/Kubernetes/Loki can ingest it and
the experiment harness can parse it. Fields: ts, service, instance, level,
request_id, operation, status, latency_ms, error, retry_count, plus extras.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from dataclasses import dataclass, field


@dataclass
class RequestState:
    """Mutable per-request record shared between middleware and handlers."""
    request_id: str = ""
    retries: int = 0
    degraded: str = ""          # non-empty reason when the response is degraded
    replay: bool = False        # response replays an earlier idempotent result
    error: str = ""
    extra: dict = field(default_factory=dict)
    span_id: str = ""           # this hop (request_id doubles as the trace id)
    parent_span_id: str = ""    # caller's span (gateway or calling service)
    principal: object = None    # security.Principal in FT_MODE=final
    security_event: str = ""    # e.g. "deny:bola" - copied into the access log


_state_var: contextvars.ContextVar[RequestState | None] = contextvars.ContextVar("req_state", default=None)


def current_state() -> RequestState:
    st = _state_var.get()
    if st is None:               # outside a request (background thread)
        st = RequestState()
    return st


def set_state(st: RequestState):
    return _state_var.set(st)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, instance: str):
        super().__init__()
        self.service, self.instance = service, instance

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "epoch": round(record.created, 3),
            "service": self.service,
            "instance": self.instance,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["error"] = payload.get("error") or repr(record.exc_info[1])
        return json.dumps(payload, default=str)


def get_logger(service: str, instance: str) -> logging.Logger:
    logger = logging.getLogger(f"ft.{service}.{instance}")
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter(service, instance))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    st = _state_var.get()
    if st and "request_id" not in fields:
        fields["request_id"] = st.request_id
    logger.log(level, msg, extra={"fields": fields})

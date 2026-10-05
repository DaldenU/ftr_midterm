"""FastAPI application factory shared by the three services.

Provides: structured access logging, Prometheus metrics, /health, /ready,
/metrics, request-id propagation, error -> HTTP mapping, runtime fault
injection (/admin/fault, gated by ENABLE_FAULT_INJECTION) and - in FT mode -
load shedding with a soft "degrade" threshold.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx
import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .cache import LastKnownGood
from .config import Settings, load_settings
from .db import Database, DatabaseUnavailable, PoolExhausted
from .logging import RequestState, current_state, get_logger, log_event, set_state, _state_var
from .metrics import Metrics
from .resilience import CircuitOpenError
from .client import ServiceUnavailable

EXEMPT = ("/health", "/ready", "/metrics", "/admin")
STARTED = time.time()


class SimulatedInterruption(Exception):
    """Injected fault: the operation is interrupted half-way (simulated crash)."""


@dataclass
class FaultState:
    latency_ms: float = 0.0
    error_rate: float = 0.0       # probability of answering 503 immediately
    fail_all: bool = False

    def active(self) -> bool:
        return bool(self.latency_ms or self.error_rate or self.fail_all)


@dataclass
class AppContext:
    settings: Settings
    metrics: Metrics
    log: object
    db: Database
    cache: LastKnownGood
    faults: FaultState = field(default_factory=FaultState)
    inflight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    clients: dict = field(default_factory=dict)       # name -> ServiceClient (for /admin/state)
    notes: dict = field(default_factory=dict)         # service-specific counters

    @property
    def overloaded(self) -> bool:
        """True when FT soft threshold is exceeded -> handlers skip optional work."""
        return self.settings.is_ft and self.inflight > self.settings.degrade_inflight

    def mark_degraded(self, reason: str):
        st = current_state()
        if not st.degraded:
            st.degraded = reason
        self.metrics.degraded.labels(self.settings.service_name, self.settings.instance_id, reason).inc()

    def maybe_fault(self, header_value: str | None, point: str):
        """Injected mid-operation faults: header ``X-Fault: <interrupt|crash>:<point>``."""
        if not self.settings.enable_fault_injection or not header_value:
            return
        try:
            action, where = header_value.split(":", 1)
        except ValueError:
            return
        if where != point:
            return
        log_event(self.log, "fault_injected", level=40, operation="fault", point=point, action=action)
        if action == "crash":
            os._exit(137)        # hard process death, nothing is flushed or released
        raise SimulatedInterruption(f"{action}:{point}")


def map_exception(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, (DatabaseUnavailable, PoolExhausted, psycopg.OperationalError, psycopg.InterfaceError)):
        return 503, "database_unavailable"
    if isinstance(exc, (ServiceUnavailable, CircuitOpenError)):
        return 503, "dependency_unavailable"
    if isinstance(exc, httpx.TimeoutException):
        return 504, "dependency_timeout"
    if isinstance(exc, httpx.HTTPError):
        return 502, "dependency_failed"
    if isinstance(exc, SimulatedInterruption):
        return 500, "operation_interrupted"
    return 500, "internal_error"


class ObservabilityMiddleware:
    """Pure-ASGI middleware: request id, fault injection, load shedding, logs, metrics."""

    def __init__(self, app, ctx: AppContext):
        self.app, self.ctx = app, ctx

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        ctx, s = self.ctx, self.ctx.settings
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        path, method = scope["path"], scope["method"]
        exempt = path.startswith(EXEMPT)
        st = RequestState(request_id=headers.get("x-request-id") or uuid.uuid4().hex[:12])
        token = set_state(st)
        t0 = time.perf_counter()
        status_holder = {"status": 500, "started": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-request-id", st.request_id.encode()))
                hdrs.append((b"x-instance", s.instance_id.encode()))
                if st.retries:
                    hdrs.append((b"x-retry-count", str(st.retries).encode()))
                if st.degraded:
                    hdrs.append((b"x-degraded", st.degraded.encode()))
                if st.replay:
                    hdrs.append((b"idempotent-replay", b"true"))
                message["headers"] = hdrs
            await send(message)

        counted = False
        try:
            if not exempt:
                # ---- injected fault (gray failure): extra latency or errors ----
                f = ctx.faults
                if f.active():
                    if f.latency_ms:
                        await asyncio.sleep(f.latency_ms / 1000.0)
                    if f.fail_all or (f.error_rate and random.random() < f.error_rate):
                        st.error = "injected_fault"
                        resp = JSONResponse({"error": "injected_fault"}, status_code=503)
                        await resp(scope, receive, send_wrapper)
                        return
                # ---- FT: load shedding (admission control) ----
                if s.is_ft and ctx.inflight >= s.max_inflight:
                    ctx.metrics.shed.labels(s.service_name, s.instance_id).inc()
                    st.error = "load_shed"
                    resp = JSONResponse({"error": "overloaded"}, status_code=503, headers={"Retry-After": "1"})
                    await resp(scope, receive, send_wrapper)
                    return
                with ctx._lock:
                    ctx.inflight += 1
                counted = True
                ctx.metrics.inflight.labels(s.service_name, s.instance_id).set(ctx.inflight)
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:  # noqa: BLE001 - map every error to an HTTP response
                code, label = map_exception(exc)
                st.error = f"{type(exc).__name__}: {exc}"[:200]
                if not status_holder["started"]:
                    resp = JSONResponse({"error": label, "detail": st.error}, status_code=code)
                    await resp(scope, receive, send_wrapper)
                else:
                    raise
        finally:
            if counted:
                with ctx._lock:
                    ctx.inflight -= 1
                ctx.metrics.inflight.labels(s.service_name, s.instance_id).set(ctx.inflight)
            dur = time.perf_counter() - t0
            status = status_holder["status"]
            route = scope.get("route")
            route_path = getattr(route, "path", path if exempt else "unmatched")
            m = ctx.metrics
            m.requests.labels(s.service_name, s.instance_id, method, route_path, str(status)).inc()
            m.latency.labels(s.service_name, s.instance_id, route_path).observe(dur)
            (m.failed if status >= 500 else m.success).labels(s.service_name, s.instance_id).inc()
            if not (exempt and status < 400):
                ctx.log.log(40 if status >= 500 else 20, "request", extra={"fields": {
                    "request_id": st.request_id, "operation": f"{method} {route_path}",
                    "status": status, "latency_ms": round(dur * 1000, 2), "error": st.error or None,
                    "retry_count": st.retries, "degraded": st.degraded or None,
                    "replay": st.replay or None}})
            _state_var.reset(token)


class FaultBody(BaseModel):
    latency_ms: float = 0.0
    error_rate: float = 0.0
    fail_all: bool = False


def create_app(service_name: str, title: str) -> tuple[FastAPI, AppContext]:
    settings = load_settings(service_name)
    logger = get_logger(service_name, settings.instance_id)
    metrics = Metrics(service_name, settings.instance_id, settings.mode)
    db = Database(settings, metrics, logger)
    ctx = AppContext(settings=settings, metrics=metrics, log=logger, db=db,
                     cache=LastKnownGood(settings.cache_max_entries))
    app = FastAPI(title=title, version="1.0.0",
                  description=f"Mode: **{settings.mode}** (set FT_MODE=baseline|ft).")
    app.add_middleware(ObservabilityMiddleware, ctx=ctx)

    @app.get("/health", tags=["ops"], summary="Liveness probe (no dependencies)")
    def health():
        return {"status": "ok", "service": service_name, "instance": settings.instance_id,
                "mode": settings.mode, "uptime_s": round(time.time() - STARTED, 1)}

    @app.get("/ready", tags=["ops"], summary="Readiness probe (checks the database)")
    def ready():
        ok = db.ping(timeout=1.0)
        body = {"ready": ok, "db": ok, "service": service_name, "instance": settings.instance_id}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def metrics_endpoint():
        data, ctype = metrics.render()
        return Response(data, media_type=ctype)

    if settings.enable_fault_injection:
        @app.post("/admin/fault", tags=["fault-injection"], summary="Inject latency / errors into this instance")
        def set_fault(body: FaultBody):
            ctx.faults = FaultState(body.latency_ms, body.error_rate, body.fail_all)
            log_event(logger, "fault_state_changed", level=30, operation="admin", **body.model_dump())
            return {"faults": ctx.faults.__dict__}

        @app.delete("/admin/fault", tags=["fault-injection"])
        def clear_fault():
            ctx.faults = FaultState()
            return {"faults": ctx.faults.__dict__}

        @app.get("/admin/state", tags=["fault-injection"], summary="Internal state for experiments")
        def state():
            br = {n: c.state() for n, c in ctx.clients.items()}
            if db.breaker:
                br["database"] = db.breaker.snapshot()
            return {"instance": settings.instance_id, "mode": settings.mode, "faults": ctx.faults.__dict__,
                    "inflight": ctx.inflight, "breakers": br, "notes": ctx.notes,
                    "now": time.time()}

    return app, ctx

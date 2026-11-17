"""API Gateway / load balancer.

BASELINE: forwards each request to the *first* configured instance of the
target service, 30 s timeout, no health checks, no failover.

FT: round-robin load balancing over all replicas, active health checks
(GET /ready every HEALTH_INTERVAL_S), passive ejection on connection errors,
failover retry on another replica (GET, or POST carrying an Idempotency-Key, or
when the request provably never reached the backend), per-request timeout.
Health transitions are recorded in /admin/events - experiments use them as the
system's own *fault detection* timestamps.
"""
import asyncio
import itertools
import os
import re
import time
import uuid

import hmac
import json
import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

MODE = os.getenv("FT_MODE", "baseline").lower()
FT = MODE in ("ft", "final")          # fault-tolerance mechanisms
SECURE = MODE == "final"              # + security controls (C1, C2-edge, C4-edge, C5, C7)
HEALTH_INTERVAL = float(os.getenv("HEALTH_INTERVAL_S", "0.5"))
HEALTH_TIMEOUT = float(os.getenv("HEALTH_TIMEOUT_S", "0.5"))
FAIL_THRESHOLD = int(os.getenv("HEALTH_FAIL_THRESHOLD", "2"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_S", "4.0")) if FT else 30.0
GW_MAX_INFLIGHT = int(os.getenv("GATEWAY_MAX_INFLIGHT", "100"))   # FT admission control at the edge
ENABLE_ADMIN = os.getenv("ENABLE_FAULT_INJECTION", "0") in ("1", "true", "yes")


sys.path.insert(0, os.environ.get("FTLIB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared")))
from ftlib import security as sec  # noqa: E402

JWT_SECRET = os.getenv("JWT_SECRET", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
MAX_BODY = int(os.getenv("MAX_BODY_BYTES", "16384"))
RATE_RPS = float(os.getenv("RATE_LIMIT_RPS", "50"))
RATE_BURST = float(os.getenv("RATE_LIMIT_BURST", "100"))
USERS: dict = {}
if SECURE:
    if len(JWT_SECRET) < 32 or len(ADMIN_TOKEN) < 32:
        raise RuntimeError("JWT_SECRET and ADMIN_TOKEN must be set (>= 32 chars) in FT_MODE=final")
    _uf = os.getenv("USERS_FILE")
    if not _uf or not os.path.exists(_uf):
        raise RuntimeError("USERS_FILE (demo principals with PBKDF2 hashes) is required in FT_MODE=final")
    with open(_uf) as _f:
        USERS = json.load(_f)
LIMITER = sec.TokenBucketRateLimiter(RATE_RPS, RATE_BURST)
AUTH_LIMITER = sec.TokenBucketRateLimiter(5.0, 10.0)      # brute-force protection on /auth/token


def _log(**fields):
    fields.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z")
    fields.setdefault("epoch", round(time.time(), 3))
    fields.setdefault("service", "gateway")
    print(json.dumps(fields, default=str), flush=True)


def _urls(name):
    return [u.strip().rstrip("/") for u in os.getenv(name, "").split(",") if u.strip()]


ROUTES = [  # (compiled regex, service)  - first match wins
    (re.compile(r"^/students/[^/]+/transcript$"), "academic"),
    (re.compile(r"^/grades"), "academic"),
    (re.compile(r"^/students"), "student"),
    (re.compile(r"^/(payments|accounts)"), "payment"),
]


class Backend:
    def __init__(self, url):
        self.url, self.healthy, self.fails = url, True, 0


class Pool:
    def __init__(self, name, urls):
        self.name = name
        self.backends = [Backend(u) for u in (urls if FT else urls[:1])]
        self._rr = itertools.count()

    def order(self):
        """Healthy backends in round-robin order; if none are healthy try them all (last resort)."""
        n = len(self.backends)
        start = next(self._rr) % max(n, 1)
        rot = self.backends[start:] + self.backends[:start]
        if not FT:
            return rot
        healthy = [b for b in rot if b.healthy]
        return healthy or rot


POOLS = {"student": Pool("student", _urls("STUDENT_URLS")),
         "payment": Pool("payment", _urls("PAYMENT_URLS")),
         "academic": Pool("academic", _urls("ACADEMIC_URLS"))}
EVENTS: list[dict] = []

reg = CollectorRegistry()
M_REQ = Counter("gateway_requests_total", "Gateway requests", ["service", "status"], registry=reg)
M_LAT = Histogram("gateway_request_duration_seconds", "Gateway latency", ["service"], registry=reg,
                  buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))
M_FAILOVER = Counter("gateway_failovers_total", "Requests served by a different replica after a failure", ["service"], registry=reg)
M_UP = Gauge("gateway_backend_up", "1 if the backend is considered healthy", ["service", "backend"], registry=reg)
M_SHED = Counter("gateway_load_shed_total", "Requests rejected by edge admission control", registry=reg)
M_INFLIGHT = Gauge("gateway_inflight_requests", "Requests being proxied", registry=reg)
INFLIGHT = 0
M_RECOV = Counter("recovery_events_total", "Recovery events", ["type"], registry=reg)
M_SEC = Counter("gateway_security_events_total", "Requests denied at the edge", ["type"], registry=reg)
M_TOKENS = Counter("gateway_tokens_issued_total", "JWTs issued", ["role"], registry=reg)

app = FastAPI(title="API Gateway", version="1.0.0", description=f"Mode: **{MODE}**")
client: httpx.AsyncClient = None  # type: ignore


def _set_health(pool: Pool, b: Backend, healthy: bool, why: str):
    if b.healthy != healthy:
        b.healthy = healthy
        EVENTS.append({"ts": time.time(), "service": pool.name, "backend": b.url,
                       "state": "up" if healthy else "down", "why": why})
        del EVENTS[:-500]
        M_UP.labels(pool.name, b.url).set(1 if healthy else 0)
        if healthy:
            M_RECOV.labels("backend_restored").inc()
        _log(level="WARNING" if not healthy else "INFO", msg="backend_health", operation="health", backend=b.url,
             state="up" if healthy else "down", why=why)


async def _health_loop():
    hc = httpx.AsyncClient(timeout=HEALTH_TIMEOUT)
    while True:
        async def check(pool, b):
            try:
                r = await hc.get(b.url + "/ready")
                ok = r.status_code == 200
                why = f"ready={r.status_code}"
            except httpx.HTTPError as exc:
                ok, why = False, type(exc).__name__
            if ok:
                b.fails = 0
                _set_health(pool, b, True, why)
            else:
                b.fails += 1
                if b.fails >= FAIL_THRESHOLD:
                    _set_health(pool, b, False, why)
        await asyncio.gather(*(check(p, b) for p in POOLS.values() for b in p.backends))
        await asyncio.sleep(HEALTH_INTERVAL)


@app.on_event("startup")
async def _startup():
    global client
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT,
                               limits=httpx.Limits(max_connections=1000, max_keepalive_connections=200))
    for p in POOLS.values():
        for b in p.backends:
            M_UP.labels(p.name, b.url).set(1)
    if FT:
        asyncio.create_task(_health_loop())


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "service": "gateway", "mode": MODE}


@app.get("/ready", tags=["ops"])
async def ready():
    if not FT:
        return {"ready": True}
    missing = [p.name for p in POOLS.values() if not any(b.healthy for b in p.backends)]
    return JSONResponse({"ready": not missing, "unavailable_services": missing}, status_code=503 if missing else 200)


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)


def _admin_ok(request: Request) -> bool:
    if not SECURE:
        return True
    tok = request.headers.get("x-admin-token", "")
    return bool(tok) and hmac.compare_digest(tok, ADMIN_TOKEN)


@app.post("/auth/token", tags=["auth"], summary="Exchange demo credentials for a short-lived JWT (final mode)")
async def auth_token(request: Request):
    if not SECURE:
        return JSONResponse({"error": "authentication is only enabled in FT_MODE=final"}, status_code=404)
    ip = request.client.host if request.client else "?"
    ok, retry = AUTH_LIMITER.allow("auth:" + ip)
    if not ok:
        M_SEC.labels("auth_rate_limited").inc()
        return JSONResponse({"error": "too_many_requests"}, status_code=429, headers={"Retry-After": f"{retry:.1f}"})
    try:
        body = await request.json()
        user, pw = str(body["username"]), str(body["password"])
    except Exception:
        return JSONResponse({"error": "username and password required"}, status_code=400)
    rec = USERS.get(user)
    if not rec or not sec.check_password(pw, rec["hash"]):
        M_SEC.labels("bad_credentials").inc()
        _log(level="WARNING", msg="auth_failed", operation="POST /auth/token", actor=user, client_ip=ip,
             security_event="deny:bad_credentials")
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)
    tok = sec.issue_token(JWT_SECRET, user, rec["role"])
    M_TOKENS.labels(rec["role"]).inc()
    return {"access_token": tok, "token_type": "bearer", "expires_in": sec.TOKEN_TTL_S, "role": rec["role"]}


@app.get("/admin/events", tags=["fault-injection"])
async def events(request: Request):
    if not _admin_ok(request):
        M_SEC.labels("admin_token").inc()
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    return {"now": time.time(), "events": EVENTS,
            "backends": {p.name: [{"url": b.url, "healthy": b.healthy} for b in p.backends] for p in POOLS.values()}}


# connect-level errors: the request never reached application code, so a retry is always safe
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
_NETWORK = (httpx.TransportError,)


_RX_STUDENT = re.compile(r"^/students/([^/]+)$")
_RX_TRANSCRIPT = re.compile(r"^/students/([^/]+)/transcript$")
_RX_ACCOUNT = re.compile(r"^/accounts/([^/]+)$")


def _edge_permission(method: str, full: str, body: bytes):
    """(permission, target_student) required for a route; None => let the service decide."""
    if m := _RX_TRANSCRIPT.match(full):
        return "transcript:read", m.group(1)
    if method == "POST" and full == "/students":
        return "student:write", None
    if m := _RX_STUDENT.match(full):
        return "student:read", m.group(1)
    if method == "POST" and full == "/grades/batch":
        return "grade:batch", None
    if method == "POST" and full == "/grades":
        return "grade:write", None
    if m := _RX_ACCOUNT.match(full):
        return "account:read", m.group(1)
    if method == "POST" and full == "/payments":
        try:
            target = json.loads(body or b"{}").get("student_id")
        except (ValueError, AttributeError):
            target = None                     # malformed body: the service rejects it (422)
        return "payment:create", target if isinstance(target, str) else None
    return None


async def _security_gate(request: Request, full: str, body: bytes):
    """Edge controls in final mode. Returns (principal, denial_response_or_None)."""
    ip = request.client.host if request.client else "?"
    if len(body) > MAX_BODY:
        return None, _deny(413, "body_too_large", ip, None, full, request)
    tok = sec.bearer(request.headers.get("authorization"))
    principal = None
    if tok:
        try:
            principal = sec.decode_token(JWT_SECRET, tok)
        except sec.AuthError as exc:
            ok, _ = LIMITER.allow("ip:" + ip)
            if not ok:
                return None, _deny(429, "rate_limited", ip, None, full, request)
            return None, _deny(401, "auth_" + exc.reason, ip, None, full, request)
    key = f"sub:{principal.sub}" if principal else f"ip:{ip}"
    ok, retry = LIMITER.allow(key)                       # C5: per-principal token bucket
    if not ok:
        return principal, _deny(429, "rate_limited", ip, principal, full, request, {"Retry-After": f"{max(retry, 0.05):.2f}"})
    if principal is None:
        return None, _deny(401, "auth_missing_credentials", ip, None, full, request, {"WWW-Authenticate": "Bearer"})
    need = _edge_permission(request.method, full, body)
    if need:
        allowed, why = sec.is_allowed(principal, need[0], need[1])
        if not allowed:                                  # C2 at the edge (services re-check)
            return principal, _deny(403, why, ip, principal, full, request)
    return principal, None


def _deny(code, kind, ip, principal, full, request, headers=None):
    M_SEC.labels(kind).inc()
    M_REQ.labels("edge", str(code)).inc()
    _log(level="WARNING", msg="edge_denied", operation=f"{request.method} {full}", status=code, client_ip=ip,
         request_id=request.headers.get("x-request-id"), actor=getattr(principal, "sub", None),
         role=getattr(principal, "role", None), security_event="deny:" + kind)
    names = {401: "unauthenticated", 403: "forbidden", 413: "payload_too_large", 429: "too_many_requests"}
    return JSONResponse({"error": names.get(code, "denied"), "detail": kind}, status_code=code, headers=headers)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy(path: str, request: Request):
    full = "/" + path
    service = next((svc for rx, svc in ROUTES if rx.match(full)), None)
    if service is None:
        return JSONResponse({"error": "no route"}, status_code=404)
    pool = POOLS[service]
    principal = None
    if SECURE:
        body = await request.body()
        principal, denial = await _security_gate(request, full, body)
        if denial is not None:
            return denial
        request.state.principal = principal
    global INFLIGHT
    if FT and INFLIGHT >= GW_MAX_INFLIGHT:
        # graceful degradation at the edge: reject cheaply instead of queueing without bound
        M_SHED.inc()
        M_REQ.labels(service, "503").inc()
        return JSONResponse({"error": "overloaded"}, status_code=503, headers={"Retry-After": "1"})
    INFLIGHT += 1
    M_INFLIGHT.set(INFLIGHT)
    try:
        return await _forward(request, path, full, service, pool)
    finally:
        INFLIGHT -= 1
        M_INFLIGHT.set(INFLIGHT)


async def _forward(request: Request, path: str, full: str, service: str, pool: Pool):
    body = await request.body()
    # never forward client-supplied internal credentials (C3/C7): only services mint those
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "connection", "x-internal-auth", "x-admin-token",
                                    "x-parent-span-id")}
    rid = headers.setdefault("x-request-id", uuid.uuid4().hex[:12])
    span = uuid.uuid4().hex[:8]
    headers["x-parent-span-id"] = span
    url_tail = full + (("?" + request.url.query) if request.url.query else "")
    replay_safe = request.method in ("GET", "HEAD") or "idempotency-key" in {k.lower() for k in headers}
    t0 = time.perf_counter()

    candidates = pool.order()
    attempts = 0
    last_err = "no backend configured"
    last_resp = None
    status_out = 502
    served = None
    try:
        for b in candidates:
            attempts += 1
            try:
                r = await client.request(request.method, b.url + url_tail, content=body, headers=headers)
            except _NEVER_SENT as exc:
                last_err = type(exc).__name__
                if FT:
                    b.fails = FAIL_THRESHOLD
                    _set_health(pool, b, False, "passive:" + last_err)   # passive ejection
                    continue                                              # always safe to retry
                break
            except httpx.TimeoutException as exc:       # sent but no answer in time
                last_err = type(exc).__name__
                status_out = 504
                if FT and replay_safe and attempts < len(candidates):
                    continue
                break
            except _NETWORK as exc:                      # connection dropped mid-flight (replica died)
                last_err = type(exc).__name__
                if FT:
                    _set_health(pool, b, False, "passive:" + last_err)
                    if replay_safe and attempts < len(candidates):
                        continue
                break
            else:
                # masked gray failure: plain 502/503 (no Retry-After => not load shedding) on an idempotent
                # request is retried once on another replica
                if FT and r.status_code in (502, 503) and replay_safe and attempts < len(candidates) \
                        and "retry-after" not in r.headers:
                    last_resp = (b, r)
                    last_err = f"HTTP {r.status_code}"
                    continue
                last_resp = None
                out_headers = {k: v for k, v in r.headers.items()
                               if k.lower() in ("content-type", "x-request-id", "x-instance", "x-retry-count",
                                                "x-degraded", "idempotent-replay", "retry-after")}
                out_headers["x-served-by"] = b.url
                out_headers["x-gateway-attempts"] = str(attempts)
                if attempts > 1:
                    M_FAILOVER.labels(service).inc()
                status_out = r.status_code
                served = b.url
                return Response(r.content, status_code=r.status_code, headers=out_headers)
        if last_resp is not None:                      # every replica answered 5xx: return the last answer
            b, r = last_resp
            status_out = r.status_code
            M_FAILOVER.labels(service).inc()
            return Response(r.content, status_code=r.status_code, headers={
                "content-type": r.headers.get("content-type", "application/json"), "x-served-by": b.url,
                "x-gateway-attempts": str(attempts), "x-request-id": rid})
        status_out = 504 if status_out == 504 else 502
        return JSONResponse({"error": "upstream_unavailable", "service": service, "detail": last_err, "attempts": attempts},
                            status_code=status_out, headers={"x-request-id": rid, "x-gateway-attempts": str(attempts)})
    finally:
        dur = time.perf_counter() - t0
        M_REQ.labels(service, str(status_out)).inc()
        M_LAT.labels(service).observe(dur)
        if FT:   # structured access log with trace context (baseline gateway keeps no access log, as in the midterm)
            p = getattr(request.state, "principal", None)
            _log(level="ERROR" if status_out >= 500 else "INFO", msg="gateway_proxy", operation=f"{request.method} {full}",
                 target_service=service, status=status_out, latency_ms=round(dur * 1000, 2), request_id=rid,
                 trace_id=rid, span_id=span, gateway_attempts=attempts, error=last_err if status_out >= 500 else None,
                 served_by=served or (last_resp[0].url if last_resp else None), actor=getattr(p, "sub", None),
                 role=getattr(p, "role", None))

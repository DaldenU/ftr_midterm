"""Security controls for FT_MODE=final (intrusion tolerance layer).

Controls implemented here and where they are enforced
-----------------------------------------------------
C1  Authentication     HS256 JSON Web Tokens (PyJWT), issued by the gateway at
                       POST /auth/token, verified at the gateway AND again in
                       every service (defence in depth: a request that bypasses
                       the gateway still needs a valid token).
C2  Authorization      role-based access control (RBAC) + object-level checks
                       (a student may only touch his/her own records - OWASP
                       API1:2023 "Broken Object Level Authorization").
C3  Service identity   service-to-service calls carry ``X-Internal-Auth``: an
                       HMAC-SHA256 over actor|role|request_id|timestamp with a
                       60 s replay window, so a forged or replayed internal call
                       is rejected.
C4  Input validation   strict pydantic models (patterns, ranges), body-size cap
                       (413), idempotency payload binding; DB CHECK constraints
                       repeat the domain rules at the persistence boundary.
C5  Rate limiting      per-principal token bucket at the gateway (429 +
                       Retry-After) isolates a flooding client from everybody else.
C6  Audit trail        append-only, SHA-256 hash-chained ``security.audit_events``
                       written INSIDE the business transaction (so an audit row
                       exists iff the write committed); any later modification or
                       deletion breaks the chain and is found by ``verify_chain``.
C7  Admin protection   fault-injection / introspection endpoints require
                       ``X-Admin-Token`` (they are a backdoor otherwise).

All secrets come from the environment (see config.load_settings, which refuses
to start in final mode with missing or short secrets).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import dataclass

import jwt  # PyJWT

JWT_ALG = "HS256"
JWT_ISSUER = "uis-gateway"
TOKEN_TTL_S = 900

# ----------------------------------------------------------------------------- RBAC
# Permissions ending in ":self" are object-scoped: allowed only when the target
# student id equals the caller's subject.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "student":   {"student:read:self", "transcript:read:self", "account:read:self", "payment:create:self",
                  "payment:read:self"},
    "faculty":   {"student:read", "transcript:read", "grade:write"},
    "registrar": {"student:read", "student:write", "transcript:read", "grade:write", "grade:batch"},
    "finance":   {"student:read", "account:read", "payment:create", "payment:read"},
    "admin":     {"*"},
    "service":   {"student:read"},           # internal callers (payment/academic -> student)
}


class AuthError(Exception):
    """401 - caller is not authenticated."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Forbidden(Exception):
    """403 - authenticated, but not allowed."""

    def __init__(self, reason: str, permission: str = "", target: str | None = None):
        super().__init__(reason)
        self.reason, self.permission, self.target = reason, permission, target


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    kind: str = "user"          # user | service
    jti: str = ""


def is_allowed(p: Principal, permission: str, target_student: str | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). ``permission`` is the base name, e.g. 'transcript:read'."""
    perms = ROLE_PERMISSIONS.get(p.role, set())
    if "*" in perms or permission in perms:
        return True, "role"
    if f"{permission}:self" in perms:
        if target_student is not None and target_student == p.sub:
            return True, "owner"
        return False, "bola"          # object-level authorization failure
    return False, "rbac"


def authorize(p: Principal | None, permission: str, target_student: str | None = None) -> None:
    if p is None:
        raise AuthError("missing_credentials")
    ok, why = is_allowed(p, permission, target_student)
    if not ok:
        raise Forbidden(why, permission, target_student)


# ----------------------------------------------------------------------------- JWT
def issue_token(secret: str, sub: str, role: str, ttl_s: int = TOKEN_TTL_S, now: float | None = None) -> str:
    now = int(now if now is not None else time.time())
    claims = {"sub": sub, "role": role, "iat": now, "exp": now + ttl_s, "iss": JWT_ISSUER, "jti": uuid.uuid4().hex[:16]}
    return jwt.encode(claims, secret, algorithm=JWT_ALG)


def decode_token(secret: str, token: str) -> Principal:
    try:
        c = jwt.decode(token, secret, algorithms=[JWT_ALG], issuer=JWT_ISSUER,
                       options={"require": ["exp", "iat", "sub", "iss"]})
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token_expired") from exc
    except jwt.InvalidSignatureError as exc:
        raise AuthError("bad_signature") from exc
    except jwt.InvalidAlgorithmError as exc:        # e.g. alg=none downgrade
        raise AuthError("bad_algorithm") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("invalid_token") from exc
    role = c.get("role")
    if role not in ROLE_PERMISSIONS or role == "service":
        raise AuthError("invalid_role")
    return Principal(sub=str(c["sub"]), role=role, kind="user", jti=str(c.get("jti", "")))


def bearer(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


# ----------------------------------------------------------------------------- internal service auth
def sign_internal(secret: str, actor: str, request_id: str, ts: float | None = None) -> str:
    ts_s = f"{(ts if ts is not None else time.time()):.3f}"
    msg = f"{actor}|service|{request_id}|{ts_s}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"v1;{actor};{ts_s};{sig}"


def verify_internal(secret: str, header_value: str | None, request_id: str, window_s: float = 60.0,
                    now: float | None = None) -> Principal:
    if not header_value:
        raise AuthError("missing_internal_auth")
    try:
        ver, actor, ts_s, sig = header_value.split(";")
        ts = float(ts_s)
    except ValueError as exc:
        raise AuthError("malformed_internal_auth") from exc
    if ver != "v1":
        raise AuthError("malformed_internal_auth")
    now = now if now is not None else time.time()
    if abs(now - ts) > window_s:
        raise AuthError("internal_auth_replay_window")
    expected = hmac.new(secret.encode(), f"{actor}|service|{request_id}|{ts_s}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise AuthError("bad_internal_signature")
    return Principal(sub=f"svc:{actor}", role="service", kind="service")


# ----------------------------------------------------------------------------- passwords (demo users)
def hash_password(password: str, salt: bytes | None = None, iterations: int = 60000) -> str:
    salt = salt or uuid.uuid4().bytes
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        algo, it, salt_hex, dk_hex = encoded.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(it))
    return hmac.compare_digest(dk.hex(), dk_hex)


# ----------------------------------------------------------------------------- rate limiting
class TokenBucketRateLimiter:
    """Classic token bucket per key: ``rate`` tokens/s refill, at most ``burst`` stored."""

    def __init__(self, rate: float, burst: float, max_keys: int = 50000):
        self.rate, self.burst, self.max_keys = rate, burst, max_keys
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Return (allowed, retry_after_s)."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            tokens, last = self._b.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._b[key] = (tokens - 1.0, now)
                return True, 0.0
            self._b[key] = (tokens, now)
            if len(self._b) > self.max_keys:          # crude bound on memory
                self._b.pop(next(iter(self._b)))
            return False, (1.0 - tokens) / self.rate


# ----------------------------------------------------------------------------- audit chain
GENESIS = "0" * 64
AUDIT_FIELDS = ("chain_id", "seq", "epoch_ms", "service", "request_id", "actor", "role", "action", "resource",
                "outcome", "status_code", "detail")


def _canonical(prev_hash: str, fields: dict) -> bytes:
    return (prev_hash + "|" + json.dumps({k: fields.get(k) for k in AUDIT_FIELDS}, sort_keys=True,
                                         separators=(",", ":"), default=str)).encode()


def event_hash(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256(_canonical(prev_hash, fields)).hexdigest()


def audit_append(conn, chain_id: str, *, service: str, request_id: str, actor: str, role: str, action: str,
                 resource: str, outcome: str, status_code: int, detail: str = "") -> str:
    """Append one event to ``chain_id`` inside the caller's transaction.

    The chain head row is locked (SELECT ... FOR UPDATE), so concurrent writers on the
    same chain serialise and the chain can never fork; if the surrounding transaction
    rolls back, the event and the head update disappear together.
    """
    conn.execute("INSERT INTO security.audit_heads(chain_id, seq, head) VALUES (%s, 0, %s) ON CONFLICT DO NOTHING",
                 (chain_id, GENESIS))
    head = conn.execute("SELECT seq, head FROM security.audit_heads WHERE chain_id=%s FOR UPDATE", (chain_id,)).fetchone()
    seq, prev = (head["seq"], head["head"]) if isinstance(head, dict) else (head[0], head[1])
    fields = {"chain_id": chain_id, "seq": seq + 1, "epoch_ms": int(time.time() * 1000), "service": service,
              "request_id": request_id, "actor": actor, "role": role, "action": action, "resource": resource[:200],
              "outcome": outcome, "status_code": status_code, "detail": (detail or "")[:300]}
    h = event_hash(prev, fields)
    conn.execute(
        "INSERT INTO security.audit_events(chain_id, seq, epoch_ms, service, request_id, actor, role, action, resource, "
        "outcome, status_code, detail, prev_hash, event_hash) VALUES (%(chain_id)s, %(seq)s, %(epoch_ms)s, %(service)s, "
        "%(request_id)s, %(actor)s, %(role)s, %(action)s, %(resource)s, %(outcome)s, %(status_code)s, %(detail)s, "
        "%(prev)s, %(h)s)", {**fields, "prev": prev, "h": h})
    conn.execute("UPDATE security.audit_heads SET seq=%s, head=%s WHERE chain_id=%s", (seq + 1, h, chain_id))
    return h


def verify_chain(rows: list[dict]) -> dict:
    """Recompute every chain. ``rows`` = all audit_events rows (any order).

    Returns counts and the first broken link per chain. A link is broken when a row's
    prev_hash does not equal the previous row's event_hash, when the stored event_hash
    does not match the recomputed one (row modified), or when sequence numbers have gaps
    (row deleted).
    """
    chains: dict[str, list[dict]] = {}
    for r in rows:
        chains.setdefault(r["chain_id"], []).append(r)
    broken, first_broken = 0, {}
    for cid, rs in chains.items():
        rs.sort(key=lambda r: r["seq"])
        prev, expect_seq = GENESIS, 1
        for r in rs:
            ok = (r["seq"] == expect_seq and r["prev_hash"] == prev and event_hash(prev, r) == r["event_hash"])
            if not ok:
                broken += 1
                first_broken.setdefault(cid, r["seq"])
            prev, expect_seq = r["event_hash"], r["seq"] + 1
    return {"chains": len(chains), "events": len(rows), "broken_links": broken, "first_broken": first_broken}

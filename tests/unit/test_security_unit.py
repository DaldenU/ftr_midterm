"""Unit tests of the security layer (no network, no database)."""
import base64
import json
import time

import jwt
import pytest

from ftlib import security as sec

SECRET = "s" * 40


def _b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_jwt_roundtrip():
    p = sec.decode_token(SECRET, sec.issue_token(SECRET, "S0001", "student"))
    assert (p.sub, p.role, p.kind) == ("S0001", "student", "user")


@pytest.mark.parametrize("make,reason", [
    (lambda: sec.issue_token("w" * 40, "S0001", "student"), "bad_signature"),
    (lambda: sec.issue_token(SECRET, "S0001", "student", ttl_s=-10, now=time.time() - 100), "token_expired"),
    (lambda: _b64({"alg": "none", "typ": "JWT"}) + "." + _b64({"sub": "S0001", "role": "admin", "iat": 1,
                                                                 "exp": 4102444800, "iss": "uis-gateway"}) + ".", None),
    (lambda: sec.issue_token(SECRET, "svc", "service"), "invalid_role"),
    (lambda: "not.a.jwt", "invalid_token"),
])
def test_jwt_rejections(make, reason):
    with pytest.raises(sec.AuthError) as e:
        sec.decode_token(SECRET, make())
    if reason:
        assert e.value.reason == reason


def test_jwt_tampered_claims_rejected():
    h, p, s = sec.issue_token(SECRET, "S0001", "student").split(".")
    claims = json.loads(base64.urlsafe_b64decode(p + "=="))
    claims["role"] = "admin"
    with pytest.raises(sec.AuthError):
        sec.decode_token(SECRET, h + "." + _b64(claims) + "." + s)


def test_jwt_requires_issuer():
    tok = jwt.encode({"sub": "S0001", "role": "student", "iat": int(time.time()), "exp": int(time.time()) + 60},
                     SECRET, algorithm="HS256")
    with pytest.raises(sec.AuthError):
        sec.decode_token(SECRET, tok)


@pytest.mark.parametrize("role,perm,target,allowed,why", [
    ("student", "student:read", "S0001", True, "owner"),
    ("student", "student:read", "S0002", False, "bola"),
    ("student", "transcript:read", "S0002", False, "bola"),
    ("student", "account:read", "S0001", True, "owner"),
    ("student", "payment:create", "S0005", False, "bola"),
    ("student", "grade:write", None, False, "rbac"),
    ("student", "student:write", None, False, "rbac"),
    ("faculty", "grade:write", None, True, "role"),
    ("faculty", "transcript:read", "S0042", True, "role"),
    ("faculty", "payment:create", "S0001", False, "rbac"),
    ("faculty", "grade:batch", None, False, "rbac"),
    ("registrar", "grade:batch", None, True, "role"),
    ("registrar", "student:write", None, True, "role"),
    ("finance", "account:read", "S0009", True, "role"),
    ("finance", "grade:write", None, False, "rbac"),
    ("service", "student:read", "S0003", True, "role"),
    ("service", "grade:write", None, False, "rbac"),
    ("admin", "anything:at:all", None, True, "role"),
])
def test_rbac_and_bola_matrix(role, perm, target, allowed, why):
    p = sec.Principal("S0001", role)
    assert sec.is_allowed(p, perm, target) == (allowed, why)


def test_authorize_raises():
    with pytest.raises(sec.AuthError):
        sec.authorize(None, "student:read", "S0001")
    with pytest.raises(sec.Forbidden) as e:
        sec.authorize(sec.Principal("S0001", "student"), "student:read", "S0002")
    assert e.value.reason == "bola"


def test_internal_auth_valid_forged_and_replayed():
    h = sec.sign_internal(SECRET, "payment-service", "rid1")
    assert sec.verify_internal(SECRET, h, "rid1").role == "service"
    with pytest.raises(sec.AuthError):
        sec.verify_internal(SECRET, h, "other-request")          # bound to the request id
    with pytest.raises(sec.AuthError):
        sec.verify_internal("x" * 40, h, "rid1")                  # wrong key
    old = sec.sign_internal(SECRET, "payment-service", "rid1", ts=time.time() - 120)
    with pytest.raises(sec.AuthError) as e:
        sec.verify_internal(SECRET, old, "rid1", window_s=60)    # replay outside the window
    assert e.value.reason == "internal_auth_replay_window"
    with pytest.raises(sec.AuthError):
        sec.verify_internal(SECRET, "garbage", "rid1")


def test_password_hashing():
    enc = sec.hash_password("correct horse", iterations=1000)
    assert sec.check_password("correct horse", enc)
    assert not sec.check_password("wrong", enc)
    assert not sec.check_password("x", "md5$1$00$00")


def test_token_bucket_isolates_keys():
    lim = sec.TokenBucketRateLimiter(rate=10, burst=5)
    t = 1000.0
    assert all(lim.allow("attacker", t)[0] for _ in range(5))
    ok, retry = lim.allow("attacker", t)
    assert not ok and retry > 0
    assert lim.allow("victim", t)[0]                   # another principal is unaffected
    assert lim.allow("attacker", t + 0.11)[0]          # refilled after 1/rate seconds


def _chain(n=5, cid="payment-1"):
    rows, prev = [], sec.GENESIS
    for i in range(1, n + 1):
        f = {"chain_id": cid, "seq": i, "epoch_ms": 1000 + i, "service": "payment-service", "request_id": f"r{i}",
             "actor": "S0001", "role": "student", "action": "payment:create", "resource": f"/payments/p{i}",
             "outcome": "ALLOW", "status_code": 201, "detail": f"amount={i}"}
        h = sec.event_hash(prev, f)
        rows.append({**f, "prev_hash": prev, "event_hash": h})
        prev = h
    return rows


def test_audit_chain_verifies():
    v = sec.verify_chain(_chain() + _chain(3, "academic-1"))
    assert v == {"chains": 2, "events": 8, "broken_links": 0, "first_broken": {}}


def test_audit_chain_detects_modification_deletion_and_insertion():
    rows = _chain()
    rows[2]["detail"] = "amount=999999"                         # modified content
    assert sec.verify_chain(rows)["first_broken"] == {"payment-1": 3}
    rows = _chain()
    del rows[1]                                                  # deleted row
    assert sec.verify_chain(rows)["broken_links"] >= 1
    rows = _chain()
    fake = dict(rows[1], seq=2, detail="forged")                 # forged row with recomputed own hash
    fake["event_hash"] = sec.event_hash(fake["prev_hash"], fake)
    rows[1] = fake
    assert sec.verify_chain(rows)["first_broken"] == {"payment-1": 3}   # next link no longer matches

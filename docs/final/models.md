# Final project - dependability objectives, fault model and threat model

(Condensed from the final technical report, sections 2-3. Experiment ids: E1=S1, E2=S2, E3=S3, E4=S5, E5=S6, E6=S8, E7=S9, E8=S7, E9=S4, E0=S0.)

## Service-level objectives
| ID | Objective | Verified by |
|---|---|---|
| SLO-1 | >= 99 % successful requests through one instance or node failure | E1, E9, campaign |
| SLO-2 | Instance RTO <= 5 s; DB-host RTO <= 30 s with RPO = 0 acknowledged writes | E1, E8, E9 |
| SLO-3 | Steady p95 <= 500 ms at 40 req/s; accepted p95 <= 1 s under overload, explicit 503 + Retry-After | E0, E5 |
| SLO-4 | Slow/failing dependency does not stall callers beyond 2 s retry budget / 4 s edge timeout | E3 |
| SLO-5 | 0 violations of invariants I1-I8 | all runs |
| SLO-6 | Each injected fault class alerted within 10 s; 0 false alerts in 14 s warm-up | E6 |
| SLO-7 | >= 95 % of failed/degraded requests traceable to hop + cause from logs | E6 |
| SLO-8 | 100 % misuse rejected (4xx), 0 harmful successes, 0 attack rows, 0 5xx, legit >= 99 %, audit chain intact | E7 |

Invariants: I1 no invoice completed twice; I2 ledger = sum of completed payments; I3 no PENDING after recovery; I4 no duplicate (student, course, term); I5 every acknowledged write exists; I6 batches all-or-nothing; I7 audit hash chain verifies; I8 no row written by an attack request.

## Fault model
| # | Fault | Failure if untolerated | Detection | Tolerance | Exp |
|---|---|---|---|---|---|
| F1 | Application process crash | 502 / outage | passive ejection, /ready, InstanceDown, InstanceRecentlyDown | replica + failover retry, supervisor/Docker restart | E1, E6 |
| F2 | Node loss | total outage (baseline) | as F1 | replicas on node B | E9 |
| F3 | DB crash/restart | data requests fail | db_up, DB breaker, DatabaseUnreachable | pool reconnect, bounded write retry, cached reads | E2, E6 |
| F4 | DB host loss | outage + data loss since backup | failover controller probes | standby promotion, multi-host DSN | E8 |
| F5 | Slow dependency (gray) | latency propagates, timeouts | timeout, breaker, HighLatencyP95 | 1 s timeout, bounded retry, breaker, degradation | E3, E6 |
| F6 | Dependency 5xx | caller 5xx | breaker counts | retry other replica, breaker, degradation | E3 |
| F7 | Interrupted transaction | wrong balance, partial batch | I2, I3, I6 | checkpoint, atomic completion, resume, reconciler | E4 |
| F8 | Duplicate request | double charge / duplicate grade | idempotency key, unique constraints | stored-result replay, 409 | E4 |
| F9 | Overload | latency, timeouts | inflight gauge, LoadSheddingActive | admission control 503 + Retry-After | E5 |
| F10 | Retry storm | metastable outage | rate vs goodput | capped jittered retries, deadline, breaker, shedding | E3, E5 |
| F11 | Stale pooled connection | one failed request | exception classification | discard + reconnect + retry | E2 |
| F12 | Silent data corruption | wrong records | ECC, page checksums | documented only | - |
| F13 | Config error / weak secret | outage or exposure | fail-fast startup | refuse to start | tests |
| F14 | Gateway crash | total outage | external probe | 2 pods + PDB in K8s; single process locally (not demonstrated) | - |

## Threat model (STRIDE; trust boundaries TB-1 client->gateway, TB-2 gateway/peer->replica, TB-3 service->PostgreSQL)
| # | STRIDE | Threat | Controls | E7 test |
|---|---|---|---|---|
| T1 | Spoofing | no/forged/alg:none/expired/edited JWT | C1 | 5 cases x 3 |
| T2 | Elevation | student posts grades / registers students / batches | C2 RBAC | 3 x 3 |
| T3 | Info disclosure | BOLA: other student's record, transcript, account; paying for another | C2 ownership | 4 x 3 |
| T4 | Spoofing (internal) | direct backend call, forged X-Internal-Auth | C1 at service, C3 HMAC, NetworkPolicy | 3 x 3 |
| T5 | Tampering | SQLi, XSS, negative amount, grade 150, idempotency-key tamper, oversized/malformed body | C4, DB CHECKs | 7 x 3 |
| T6 | Credentials | password brute force | PBKDF2, login rate limit | 15 attempts |
| T7 | Elevation/DoS | fault-injection endpoint as backdoor | C7 admin token, disabled in prod config | 1 |
| T8 | DoS | single-principal flood | C5 token bucket, body cap, admission control | 200 req/s, 6 s |
| - | Repudiation | deny an action / edit the log | C6 hash-chained audit in-transaction + append-only trigger | tamper test |

Controls: C1 JWT HS256 (gateway + services), C2 RBAC + object ownership (gateway + services), C3 HMAC service identity (60 s window), C4 strict validation + 16 KiB body cap + payload binding + DB CHECKs, C5 per-principal token bucket (50/s, burst 100), C6 SHA-256 audit chain per instance, C7 admin token on /admin.

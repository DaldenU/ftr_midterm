# Fault-Tolerant & Secure University Information System (final project)

Final project (Fault Tolerance and Dependable Computing, group CSE-2507M). One code base, three behaviours selected by `FT_MODE`:

* `baseline` - naive implementation (1 instance per service, no retry, 30 s timeouts, non-atomic payments, no constraints, no security)
* `ft` - the midterm fault-tolerant configuration (2 replicas on 2 logical nodes, health-checking gateway, timeouts, retry+backoff, circuit breakers, idempotency + DB constraints, payment checkpointing/reconciler, atomic batches, graceful degradation, load shedding, self-healing, PostgreSQL streaming replication + failover)
* `final` - `ft` + security layer (JWT auth, RBAC + object-level authorization, HMAC service identity, strict validation, body cap, per-principal rate limiting, hash-chained audit trail, admin-token protection) + gateway access logs with trace/span ids + Prometheus alert rules

The final technical report is delivered separately (Claude Docs / exported .docx). Results it quotes are in `experiments/results_final/`.

## Final project quick reference

| What | Where |
|---|---|
| Security layer | `shared/ftlib/security.py`, `shared/ftlib/app.py::_security_gate`, `gateway/app.py` (`/auth/token`, `_security_gate`) |
| Final schema (+ audit chain, append-only trigger) | `database/schema_final.sql` |
| Alert rules + rule unit tests | `monitoring/prometheus/alerts.yml`, `tests/promtool/alerts_test.yml` |
| New scenarios S0 (overhead), S8 (alerting/diagnosability), S9 (misuse) | `experiments/harness/scenarios_final.py` |
| Real Prometheus per run + alert recorder | `experiments/harness/prom.py` (needs `PROMETHEUS_BIN`, default `/opt/prom/prometheus`) |
| Container experiment C1 (Docker restart policy, hung container) | `experiments/container/c1_restart.py` |
| Consolidated tables | `experiments/analysis/final_report_tables.py` -> `experiments/results_final/summary.json`, `comparison.md` |
| K8s HPA / PDB / NetworkPolicy, pod hardening | `kubernetes/50-resilience-security-policies.yaml`, `kubernetes/20-services.yaml` |
| Secrets (never committed) | `python scripts/gen_secrets.py` -> `.env`, `secrets/users.json` |

Experiment ids in the report: E1=S1 instance crash, E2=S2 DB unavailable, E3=S3 gray failure, E4=S5 interrupted transactions, E5=S6 load spike, E6=S8 monitoring/alerts, E7=S9 misuse, E8=S7 primary DB loss, E9=S4 node loss, E0=S0 overhead.

### Reproduce the final results (about 80 min on 2 vCPU)

```bash
pip install -r requirements-dev.txt          # + PostgreSQL 16 binaries, Prometheus 2.53 (PROMETHEUS_BIN), Docker for C1
python3 -m pytest -q                          # 118 tests (needs PostgreSQL; promtool for the rule tests)
python3 experiments/run_experiments.py --scenarios S0,S1,S2,S3,S4,S5,S6,S7,S8,S9 --modes baseline,final --reps 3 --results experiments/results_final
python3 experiments/run_experiments.py --scenarios S0 --modes ft --reps 3 --results experiments/results_final
python3 experiments/run_campaign.py --modes baseline,final --results experiments/results_final
python3 experiments/container/c1_restart.py --reps 3
python3 experiments/analysis/final_report_tables.py
python3 scripts/demo_final.py                 # live demonstration of 4 scenarios (final vs baseline)
```

## Architecture

```
Client -> Gateway (LB, health checks, failover, load shedding)
            -> Student Service  x2 (nodes A, B)
            -> Payment Service  x2   (idempotent, checkpointed; calls Student)
            -> Academic Service x2   (grades, transcript; calls Student)
                  -> PostgreSQL primary (+ streaming-replication standby, pg_dump backups)
Prometheus scrapes /metrics; Grafana dashboard; JSON logs on stdout
```

Architecture diagram, fault and threat models: final report sections 3 and 5; condensed in `docs/final/models.md`.

## Prerequisites

* Python 3.11+ (developed on 3.13), `pip install -r requirements-dev.txt`
* For tests/experiments (local harness): PostgreSQL server binaries (`apt install postgresql`; override with `PG_BIN=/usr/lib/postgresql/16/bin`). If run as root the harness uses `runuser -u postgres`.
* For containers: Docker with Compose v2; for Kubernetes: a cluster (e.g. kind/minikube).

## Configuration

All settings are environment variables; copy `.env.example` to `.env`. `DATABASE_URL` is mandatory (no default secret). Main knobs: `FT_MODE`, `HTTP_TIMEOUT_S`, `RETRY_ATTEMPTS`, `RETRY_BASE_DELAY_S`, `DOWNSTREAM_DEADLINE_S`, `BREAKER_FAILURE_THRESHOLD`, `BREAKER_RESET_TIMEOUT_S`, `DB_RETRY_DEADLINE_S`, `DB_POOL_SIZE`, `MAX_INFLIGHT`, `DEGRADE_INFLIGHT`, `GATEWAY_MAX_INFLIGHT`, `PAYMENT_LEASE_S`, `PAYMENT_ROLLBACK_AFTER_S`, `ENABLE_FAULT_INJECTION`.

## Running

### Local Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q                       # run test suite
python3 scripts/demo.py 1                  # kill an instance under load
python3 scripts/demo.py 2                  # DB outage + slow dependency (timeout/retry/breaker/degradation)
python3 scripts/demo.py 3                  # duplicate + interrupted payment (idempotency / checkpoint)
```

### Docker Compose

```bash
cp .env.example .env
docker compose up --build                                  # fault-tolerant stack
docker compose -f docker-compose.baseline.yml up --build   # baseline
docker compose --profile replication up --build            # + PostgreSQL hot standby
```

Gateway `http://localhost:8000`; service replicas 8101/8102 (student), 8201/8202 (payment), 8301/8302 (academic); Prometheus 9090; Grafana 3000 (dashboard "University IS - Fault Tolerance"). Kubernetes: `kubernetes/README.md`.

### Example requests (final mode: get a token first; demo passwords are in secrets/demo_passwords.txt)

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/token -H 'Content-Type: application/json' \
        -d '{"username":"S0001","password":"<from secrets/demo_passwords.txt>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -X POST localhost:8000/payments -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'Idempotency-Key: k-1' \
     -d '{"student_id":"S0001","amount":1000,"invoice_id":"INV-1"}'     # repeat: same result, Idempotent-Replay: true
curl -H "Authorization: Bearer $TOKEN" localhost:8000/students/S0001/transcript    # own record: 200
curl -H "Authorization: Bearer $TOKEN" localhost:8000/students/S0002               # another student's record: 403 (BOLA)
curl localhost:8000/students/S0001                                                  # no token: 401
```

## Failure injection

Integrated harness: `python3 experiments/run_experiments.py --scenarios S1,S5 --modes baseline,ft --reps 1`.
Scenarios: S1 application crash, S2 database crash/restart, S3 gray failure of a dependency, S4 node loss, S5 interrupted/duplicated transactions, S6 high load, S7 loss of the database host.
Manual injection against a running stack: `python3 experiments/failure_injection/inject.py --help`; HTTP fault control `POST /admin/fault` when `ENABLE_FAULT_INJECTION=true`; per-request faults via header `X-Fault: interrupt|crash:<point>`.

## Experiments, metrics and results

```bash
scripts/run_all_experiments.sh      # ~45 min: 42 runs + reliability campaign + tables
make analysis                       # regenerate comparison.md, reliability.md, charts from stored results
python3 report/build_report.py      # regenerate the final report
```

Outputs: `experiments/results/experiments.csv`, `json/`, `summaries/`, `raw/`, `comparison.md`, `reliability.md`, `charts/`.
Formulas (MTTF = U/n, MTTR = D/n, MTBF = T/n, A = MTTF/(MTTF+MTTR), lambda = n/U): `experiments/analysis/reliability.py` (report section 4).

## Midterm headline results (historical; final results are in experiments/results_final/)

| Scenario | Metric | Baseline | FT |
|---|---|---:|---:|
| S1 app crash | failed requests | 83 | 0 |
| S4 node loss | failed requests | 640 | 0 |
| S5 corrupted txn | duplicate payments / consistent runs | 24 / 0 of 3 | 0 / 3 of 3 |
| S6 high load (150 clients) | p95 latency | 3295 ms | 195 ms (2170 requests deliberately shed) |
| S7 DB host loss | recovery / acknowledged writes lost | 21.3 s / 95 | 2.0 s / 0 |
| Campaign (240 s, accelerated) | whole-system availability | 14.6 % | 95.1 % |

Not better: S2 (DB restart) recovery time is bounded by the outage itself (8.4 s vs 8.8 s) and write tail latency rises; S3 recovery duration is longer in FT (25.4 s vs 20.6 s); S5 shows more failed (but consistent) requests. See report section 10.5.

## Observability

`GET /health` (liveness), `GET /ready` (dependencies), `GET /metrics` (Prometheus), `GET /admin/state`, gateway `GET /admin/events`. JSON logs include `request_id`, `instance`, `operation`, `retry_count`, `degraded`. Response headers: `X-Request-ID`, `X-Instance`, `X-Retry-Count`, `X-Degraded`, `Idempotent-Replay`.

## Demonstration

`python3 scripts/demo_final.py` - four scenarios for the final presentation (instance crash, interrupted payments, alert sequence, misuse battery), each baseline then final, followed by the comparison table. Midterm demos: `scripts/demo.py 1|2|3`.

## Troubleshooting

* `PostgreSQL binaries not found` -> install `postgresql` or set `PG_BIN`.
* `Address already in use` / stale `.s.PGSQL` lock after a crash -> `pkill -f "uvicorn app:app"` carefully, remove the work directory (`experiments/harness/stack.py::default_workdir`).
* Timings differ from the report -> the experiments are CPU-sensitive; do not run other heavy work meanwhile.
* Kubernetes manifests were validated as YAML but not applied to a cluster; Docker restart behaviour was measured with `experiments/container/c1_restart.py`.

## Repository map

`shared/ftlib/` FT library - `services/` three services - `gateway/` - `database/` schemas - `experiments/` harness, workload, analysis, results - `tests/` - `monitoring/` - `kubernetes/` - `docs/final/` (models) - `scripts/`.

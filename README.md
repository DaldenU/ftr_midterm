# Fault-Tolerant University Information System

Midterm project (Fault Tolerance and Dependable Computing). One code base, two behaviours selected by `FT_MODE`:

* `baseline` - naive implementation (1 instance per service, no retry, 30 s timeouts, non-atomic payments, no constraints)
* `ft` - fault-tolerant (2 replicas on 2 logical nodes, health-checking gateway, timeouts, retry+backoff, circuit breakers, idempotency + DB constraints, payment checkpointing/reconciler, atomic batches, graceful degradation, load shedding, self-healing, PostgreSQL streaming replication + failover)

## Architecture

```
Client -> Gateway (LB, health checks, failover, load shedding)
            -> Student Service  x2 (nodes A, B)
            -> Payment Service  x2   (idempotent, checkpointed; calls Student)
            -> Academic Service x2   (grades, transcript; calls Student)
                  -> PostgreSQL primary (+ streaming-replication standby, pg_dump backups)
Prometheus scrapes /metrics; Grafana dashboard; JSON logs on stdout
```

Mermaid diagram and mechanism table: `docs/architecture.md`; rendered image: `docs/architecture-diagram.png`. API: `docs/api.md`.

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

### Example requests

```bash
curl -X POST localhost:8000/payments -H 'Content-Type: application/json' -H 'Idempotency-Key: k-1' \
     -d '{"student_id":"S0001","amount":1000,"invoice_id":"INV-1"}'      # repeat: same result, Idempotent-Replay: true
curl localhost:8000/students/S0001 ; curl localhost:8000/students/S0001/transcript ; curl localhost:8000/ready
```
(exact fields: `docs/api.md`)

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
Formulas (MTTF = U/n, MTTR = D/n, MTBF = T/n, A = MTTF/(MTTF+MTTR), lambda = n/U): `docs/reliability.md`, `experiments/analysis/reliability.py`.

## Headline results (measured; mean of 3 runs; details in the report)

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

`docs/demonstration.md` - three demos x seven items (failure, expected baseline behaviour, expected FT behaviour, command, observation, metric/log, conclusion).

## Troubleshooting

* `PostgreSQL binaries not found` -> install `postgresql` or set `PG_BIN`.
* `Address already in use` / stale `.s.PGSQL` lock after a crash -> `pkill -f "uvicorn app:app"` carefully, remove the work directory (`experiments/harness/stack.py::default_workdir`).
* Timings differ from the report -> the experiments are CPU-sensitive; do not run other heavy work meanwhile.
* Docker/K8s/Grafana not tested here - please report failures; configuration lives in `docker-compose*.yml`, `kubernetes/`, `monitoring/`.

## Repository map

`shared/ftlib/` FT library - `services/` three services - `gateway/` - `database/` schemas - `experiments/` harness, workload, analysis, results - `tests/` - `monitoring/` - `kubernetes/` - `docs/` - `report/` - `scripts/`.

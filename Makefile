.PHONY: help install test test-unit test-fast experiments campaign analysis demo1 demo2 demo3 up up-baseline down diagram report

PY ?= python3
help:
	@grep -E '^[a-z0-9-]+:.*##' Makefile | sed 's/:.*##/ -/'
install:            ## install Python dependencies (needs PostgreSQL server binaries for the local harness)
	$(PY) -m pip install -r requirements-dev.txt
test:               ## full test suite (needs PostgreSQL binaries; ~1.5 min)
	$(PY) -m pytest tests -v
test-unit:          ## unit tests only (no database needed)
	$(PY) -m pytest tests/unit -q
experiments:        ## all failure-injection experiments, 3 repetitions (~45 min) + campaign + tables
	REPS=3 scripts/run_all_experiments.sh
campaign:           ## reliability campaign only (MTTF/MTBF/MTTR/availability)
	$(PY) experiments/run_campaign.py && $(PY) experiments/analysis/reliability.py experiments/results
analysis:           ## regenerate comparison.md + charts + reliability.md from stored results
	$(PY) experiments/analysis/reliability.py experiments/results && $(PY) experiments/analysis/report_tables.py experiments/results
demo1:              ## kill one application instance under load
	$(PY) scripts/demo.py 1
demo2:              ## DB outage + slow dependency (timeout/retry/breaker)
	$(PY) scripts/demo.py 2
demo3:              ## duplicate + interrupted payment (idempotency)
	$(PY) scripts/demo.py 3
up:                 ## fault-tolerant stack with Docker Compose
	docker compose up --build
up-baseline:        ## baseline stack with Docker Compose
	docker compose -f docker-compose.baseline.yml up --build
down:
	docker compose down -v; docker compose -f docker-compose.baseline.yml down -v
diagram:            ## render docs/architecture-diagram.png
	$(PY) docs/make_diagram.py
report:             ## assemble report/final_report.md from template + measured tables
	$(PY) report/build_report.py

"""Shared fixtures: a private PostgreSQL cluster and two complete local deployments
(baseline: 1 replica/service, no fault tolerance; ft: 2 replicas/service + all mechanisms).
Tests use different ports and a different work directory from the experiments, so they can
be run independently of experiment results."""
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "shared")]

from experiments.harness.pg import PgCluster, find_pg_bin  # noqa: E402
from experiments.harness.stack import Stack  # noqa: E402

WORK = Path(os.getenv("FTUIS_TEST_WORKDIR", "/var/tmp/ftuis-test"))
TEST_ENV = {"PAYMENT_ROLLBACK_AFTER_S": 2, "PAYMENT_LEASE_S": 1.0, "BREAKER_RESET_TIMEOUT_S": 2, "BREAKER_FAILURE_THRESHOLD": 4}


@pytest.fixture(scope="session")
def pg():
    if find_pg_bin() is None:
        pytest.skip("PostgreSQL binaries not available")
    cluster = PgCluster(WORK, port=56432, standby_port=56433)
    cluster.start()
    yield cluster
    cluster.stop()


def _make_stack(mode, pg, base_port):
    pg.reset_db(f"uni_{mode}", ROOT / f"database/schema_{mode}.sql")
    st = Stack(mode, pg, base_port=base_port, workdir=WORK, env=TEST_ENV, restart_delay_s=0.5)
    st.start()
    return st


@pytest.fixture(scope="session")
def ft_stack(pg):
    st = _make_stack("ft", pg, 19000)
    yield st
    st.stop()


@pytest.fixture(scope="session")
def baseline_stack(pg):
    st = _make_stack("baseline", pg, 19500)
    yield st
    st.stop()


@pytest.fixture(scope="session")
def final_stack(pg):
    st = _make_stack("final", pg, 19800)
    yield st
    st.stop()


@pytest.fixture
def final_client(final_stack):
    with httpx.Client(base_url=final_stack.gateway_url, timeout=10) as c:
        yield c


@pytest.fixture(params=["baseline", "ft"])
def stack(request, baseline_stack, ft_stack):
    return baseline_stack if request.param == "baseline" else ft_stack


@pytest.fixture
def client(stack):
    with httpx.Client(base_url=stack.gateway_url, timeout=10) as c:
        yield c


@pytest.fixture
def ft_client(ft_stack):
    with httpx.Client(base_url=ft_stack.gateway_url, timeout=10) as c:
        yield c


def uid(prefix=""):
    return prefix + uuid.uuid4().hex[:10]


def wait_until(fn, timeout=15.0, interval=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


def db_query(stack, sql, *args):
    import psycopg
    with psycopg.connect(stack.database_url, autocommit=True) as c:
        return c.execute(sql, args).fetchall()

"""Alert rules are syntactically valid and behave as specified (Prometheus' own rule unit-test runner)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROMTOOL = os.getenv("PROMTOOL_BIN", "/opt/prom/promtool")


@pytest.mark.skipif(not (Path(PROMTOOL).exists() or shutil.which("promtool")), reason="promtool not installed")
def test_alert_rules_check_and_unit_tests():
    tool = PROMTOOL if Path(PROMTOOL).exists() else shutil.which("promtool")
    r = subprocess.run([tool, "check", "rules", str(ROOT / "monitoring/prometheus/alerts.yml")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([tool, "test", "rules", str(ROOT / "tests/promtool/alerts_test.yml")], capture_output=True, text=True)
    assert r.returncode == 0 and "SUCCESS" in r.stdout, r.stdout + r.stderr

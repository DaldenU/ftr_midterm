"""Unit tests of the reliability formulas (hand-checkable numbers)."""
import math

import pytest

from experiments.analysis import reliability as R


def test_mttf_mttr_mtbf():
    # 4 failures in 100 s, 20 s total downtime
    assert R.mttf(80, 4) == 20
    assert R.mttr(20, 4) == 5
    assert R.mtbf(100, 4) == 25                      # = MTTF + MTTR
    assert R.mtbf(100, 4) == R.mttf(80, 4) + R.mttr(20, 4)


def test_availability_theory_equals_observed_for_consistent_data():
    assert R.availability_theory(20, 5) == pytest.approx(0.8)
    assert R.availability_observed(80, 100) == pytest.approx(0.8)


def test_failure_rate_and_reliability():
    lam = R.failure_rate(4, 80)
    assert lam == pytest.approx(0.05)
    assert R.reliability(lam, 10) == pytest.approx(math.exp(-0.5))
    assert R.failure_rate(0, 80) == 0


def test_redundancy_formulas():
    assert R.parallel(0.9, 2) == pytest.approx(0.99)
    assert R.parallel(0.9, 3) == pytest.approx(0.999)
    assert R.series([0.9, 0.9, 0.9]) == pytest.approx(0.729)
    assert R.nines(0.999) == pytest.approx(3.0)


def test_rule_of_three_for_zero_failures():
    assert R.rule_of_three(300) == pytest.approx(0.01)
    assert R.mttf(300, 0) is None


def test_analyse_campaign_on_synthetic_data():
    c = {"mode": "baseline", "duration_s": 100.0, "instances": 3, "mttf_component_injected_s": 20.0,
         "kills": [{"repair_s": 10.0}, {"repair_s": 10.0}],
         "services": {"student": {"outages": [[10, 20], [50, 60]]}, "payment": {"outages": []}, "academic": {"outages": [[10, 20]]}},
         "requests": {}}
    out = R.analyse_campaign(c)
    s = out["services"]["student"]
    assert s["failures"] == 2 and s["downtime_s"] == 20 and s["MTTR_s"] == 10 and s["MTTF_s"] == 40 and s["MTBF_s"] == 50
    assert s["availability_observed"] == pytest.approx(0.8)
    assert out["services"]["payment"]["MTTF_s"] is None and "censored" in out["services"]["payment"]["MTTF_note"]
    # union of outages: [10,20] and [50,60] -> 2 system failures, 20 s down
    assert out["system_any_service_down"]["failures"] == 2
    assert out["system_any_service_down"]["downtime_s"] == 20
    # component: 2 kills, 20 s of instance downtime out of 300 instance-seconds -> MTTF_c = 140 s
    assert out["component"]["MTTF_component_s"] == pytest.approx(140.0)

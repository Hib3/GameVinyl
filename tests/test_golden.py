"""Acceptance criterion 2: golden set, 100 cases, 0 diffs, and zero
price-'high' false positives (release blocker)."""
from grt.golden import run_golden


def test_golden_set_100_cases_zero_diff():
    result = run_golden()
    assert result["total"] == 100
    assert result["diffs"] == [], result["diffs"]
    assert result["price_false_positives"] == []

"""
Tests for the calendar-cycle vocabulary (owner concern #4, Way A). Pure
date math, fully offline.

Run either of these from the project folder:
    python tests/test_cycles.py
    python -m pytest tests/test_cycles.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import cycles


def test_expiry_era_rule_thursday_then_tuesday():
    # Pre-migration month: August 2025 -> last THURSDAY.
    assert cycles.monthly_expiry(2025, 8) == date(2025, 8, 28)
    assert cycles.monthly_expiry(2025, 8).weekday() == 3
    # Post-migration month: October 2025 -> last TUESDAY.
    assert cycles.monthly_expiry(2025, 10) == date(2025, 10, 28)
    assert cycles.monthly_expiry(2025, 10).weekday() == 1
    # July 2026 (current era): last Tuesday.
    assert cycles.monthly_expiry(2026, 7) == date(2026, 7, 28)


def test_days_to_expiry_rolls_to_next_month_after_passing():
    exp = cycles.monthly_expiry(2026, 7)                    # 2026-07-28
    assert cycles.days_to_monthly_expiry(date(2026, 7, 27)) == 1
    assert cycles.days_to_monthly_expiry(exp) == 0
    # The day after expiry looks at August's cycle.
    after = cycles.days_to_monthly_expiry(date(2026, 7, 29))
    assert after == (cycles.monthly_expiry(2026, 8) - date(2026, 7, 29)).days
    assert after > 20


def test_cycle_tags_windows():
    # Expiry week: within 4 days of 2026-07-28.
    tags = cycles.cycle_tags(date(2026, 7, 24))
    assert "season:expiry_week" in tags and "season:month_jul" in tags
    assert "season:expiry_day" not in tags
    # Expiry day itself.
    tags = cycles.cycle_tags(date(2026, 7, 28))
    assert "season:expiry_day" in tags and "season:expiry_week" in tags
    # Mid-month: month tag only.
    tags = cycles.cycle_tags(date(2026, 7, 10))
    assert tags == {"season:month_jul"}
    # Quarter-end window (last 5 days of September).
    assert "season:quarter_end" in cycles.cycle_tags(date(2026, 9, 28))
    assert "season:quarter_end" not in cycles.cycle_tags(date(2026, 8, 28))


def test_iso_wrapper_fails_open():
    assert cycles.cycle_tags_for_iso("2026-07-28") == cycles.cycle_tags(
        date(2026, 7, 28))
    assert cycles.cycle_tags_for_iso("garbage") == set()
    assert cycles.cycle_tags_for_iso(None) == set()


def test_event_tags_buckets_and_fail_open():
    cal = {"SOON.NS": "2026-07-13",              # 2 days out
           "LATER.NS": "2026-07-19",             # 8 days out
           "FAR.NS": "2026-09-01"}
    d = date(2026, 7, 11)
    assert cycles.event_tags("SOON.NS", d, cal) == {"event:results_in_3d"}
    assert cycles.event_tags("LATER.NS", d, cal) == {"event:results_in_10d"}
    assert cycles.event_tags("FAR.NS", d, cal) == set()
    assert cycles.event_tags("UNKNOWN.NS", d, cal) == set()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

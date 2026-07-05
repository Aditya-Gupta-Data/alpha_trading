"""
Tests for the Phase 4F learning-loop tuner, using fake journal entries so
they run instantly with no internet and never touch the real journal.

Run either of these from the project folder:
    python tests/test_tuner.py          (simple, no extra installs)
    python -m pytest tests/             (if you have pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tuner import build_weights
from src.config import TUNER_MIN_SAMPLES, TUNER_WEIGHT_BOUNDS


def make_buy_entry(signal="fresh Golden Cross -- trend just turned up",
                    r_multiple=1.0, pattern_tags=None, has_plan=True, resolved=True):
    return {
        "action": "BUY",
        "signal": signal,
        "pattern_tags": pattern_tags or [],
        "plan": {"stop_loss": {"price": 90.0}} if has_plan else None,
        "outcome": ({"r_multiple": r_multiple} if resolved else None),
    }


def test_ignores_entries_without_a_resolved_plan():
    entries = [
        make_buy_entry(has_plan=False),
        make_buy_entry(resolved=False),
        {"action": "SELL", "signal": "downtrend", "plan": {"stop_loss": None}, "outcome": None},
    ]
    data = build_weights(entries)
    assert data["resolved_trade_count"] == 0
    assert data["weights"] == {}


def test_stays_neutral_below_min_sample_size():
    entries = [make_buy_entry(r_multiple=2.0) for _ in range(TUNER_MIN_SAMPLES - 1)]
    data = build_weights(entries)
    assert data["weights"]["fresh_cross"] == 1.0
    assert data["sample_counts"]["fresh_cross"] == TUNER_MIN_SAMPLES - 1


def test_positive_average_r_raises_weight_above_neutral():
    entries = [make_buy_entry(r_multiple=2.0) for _ in range(TUNER_MIN_SAMPLES)]
    data = build_weights(entries)
    assert data["weights"]["fresh_cross"] > 1.0
    assert data["weights"]["fresh_cross"] <= TUNER_WEIGHT_BOUNDS[1]


def test_negative_average_r_lowers_weight_below_neutral():
    entries = [make_buy_entry(r_multiple=-2.0) for _ in range(TUNER_MIN_SAMPLES)]
    data = build_weights(entries)
    assert data["weights"]["fresh_cross"] < 1.0
    assert data["weights"]["fresh_cross"] >= TUNER_WEIGHT_BOUNDS[0]


def test_weight_is_capped_at_bounds():
    entries = [make_buy_entry(r_multiple=50.0) for _ in range(TUNER_MIN_SAMPLES)]
    data = build_weights(entries)
    assert data["weights"]["fresh_cross"] == TUNER_WEIGHT_BOUNDS[1]


def test_archetypes_are_tracked_separately():
    entries = (
        [make_buy_entry(signal="fresh Golden Cross -- trend just turned up", r_multiple=2.0)
         for _ in range(TUNER_MIN_SAMPLES)]
        + [make_buy_entry(signal="uptrend with a dip (RSI 25) -- buying the pullback", r_multiple=-2.0)
           for _ in range(TUNER_MIN_SAMPLES)]
    )
    data = build_weights(entries)
    assert data["weights"]["fresh_cross"] > 1.0
    assert data["weights"]["rsi_oversold"] < 1.0


def test_pattern_tags_are_reported_but_not_weighted():
    entries = [make_buy_entry(r_multiple=1.5, pattern_tags=["Breakout"])
               for _ in range(TUNER_MIN_SAMPLES)]
    data = build_weights(entries)
    assert "Breakout" in data["pattern_tag_report"]
    assert data["pattern_tag_report"]["Breakout"]["count"] == TUNER_MIN_SAMPLES
    assert "Breakout" not in data["weights"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

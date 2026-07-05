"""
Tests for the rule engine, using fake data so they run instantly with no internet.

Run either of these from the project folder:
    python tests/test_rules.py          (simple, no extra installs)
    python -m pytest tests/             (if you have pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.rules import check_rule, describe


def make_quote(price, pct):
    """Build a fake quote so we can test rules without hitting the internet."""
    return {
        "ticker": "TEST.NS",
        "current_price": price,
        "prev_close": price,
        "percent_change": pct,
    }


def test_price_above():
    assert check_rule(make_quote(3100, 0), "price_above", 3000) is True
    assert check_rule(make_quote(2900, 0), "price_above", 3000) is False


def test_price_below():
    assert check_rule(make_quote(2900, 0), "price_below", 3000) is True
    assert check_rule(make_quote(3100, 0), "price_below", 3000) is False


def test_percent_up():
    assert check_rule(make_quote(100, 3.5), "percent_up", 3) is True
    assert check_rule(make_quote(100, 1.0), "percent_up", 3) is False


def test_percent_down():
    assert check_rule(make_quote(100, -4.0), "percent_down", 2) is True
    assert check_rule(make_quote(100, -1.0), "percent_down", 2) is False


def test_describe_runs():
    msg = describe(make_quote(3100, 1.25), "price_above", 3000)
    assert "TEST.NS" in msg


if __name__ == "__main__":
    # Simple runner so you don't even need pytest installed.
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

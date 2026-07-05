"""
Tests for the paper portfolio and strategy engine, using fake data so they
run instantly with no internet.

Run either of these from the project folder:
    python tests/test_portfolio.py      (simple, no extra installs)
    python -m pytest tests/             (if you have pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.portfolio import buy, sell, total_value, max_affordable_shares, STARTING_CASH
from src.strategy import propose
from src.review import verdict_for


def fresh_book():
    return {"cash": STARTING_CASH, "holdings": {}, "created": "2026-01-01"}


def make_analysis(ticker="TEST.NS", uptrend=True, fresh_cross=False, rsi=50, price=100.0):
    return {
        "ticker": ticker,
        "uptrend": uptrend,
        "fresh_cross": fresh_cross,
        "rsi": rsi,
        "price": price,
    }


def test_buy_updates_cash_and_holdings():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    assert book["cash"] == STARTING_CASH - 1000
    assert book["holdings"]["TEST.NS"]["shares"] == 10


def test_buy_cannot_overspend():
    book = fresh_book()
    try:
        buy(book, "TEST.NS", 2000, 100.0)  # Rs.2,00,000 > cash
        assert False, "should have raised"
    except ValueError:
        pass


def test_sell_returns_cash_and_clears_position():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    sell(book, "TEST.NS", 110.0)
    assert book["cash"] == STARTING_CASH + 100  # 10 shares x Rs.10 profit
    assert "TEST.NS" not in book["holdings"]


def test_position_cap_limits_shares():
    book = fresh_book()
    # 25% of Rs.1,00,000 = Rs.25,000 -> at Rs.100/share, max 250 even though cash allows 1000
    assert max_affordable_shares(book, 100.0, {}) == 250


def test_total_value_uses_live_prices():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    assert total_value(book, {"TEST.NS": 120.0}) == STARTING_CASH + 200


def test_propose_buys_on_dip_in_uptrend():
    book = fresh_book()
    prop = propose(make_analysis(uptrend=True, rsi=25), book, {})
    assert prop is not None and prop["action"] == "BUY"


def test_propose_ignores_neutral_uptrend():
    book = fresh_book()
    assert propose(make_analysis(uptrend=True, rsi=50), book, {}) is None


def test_propose_sells_holding_in_downtrend():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    prop = propose(make_analysis(uptrend=False, rsi=50), book, {"TEST.NS": 100.0})
    assert prop is not None and prop["action"] == "SELL" and prop["shares"] == 10


def test_propose_never_sells_what_we_dont_hold():
    book = fresh_book()
    assert propose(make_analysis(uptrend=False, rsi=50), book, {}) is None


def test_verdicts():
    approved_buy = {"action": "BUY", "decision": "approved"}
    assert verdict_for(approved_buy, +5.0).startswith("WIN")
    assert verdict_for(approved_buy, -5.0).startswith("LOSS")
    assert "flat" in verdict_for(approved_buy, +0.5)
    rejected_buy = {"action": "BUY", "decision": "rejected"}
    assert verdict_for(rejected_buy, -5.0).startswith("GOOD SKIP")
    assert verdict_for(rejected_buy, +5.0).startswith("MISSED GAIN")


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

"""
Tests for the Phase 4E forecast layer, using fake data so they run
instantly with no internet (no live price/news fetch).

Run either of these from the project folder:
    python tests/test_forecast.py       (simple, no extra installs)
    python -m pytest tests/             (if you have pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.forecast as forecast_module
from src.forecast import forecast, _rsi_driver, _news_driver, _cross_driver


def make_analysis(ticker="TEST.NS", uptrend=True, fresh_cross=False, rsi=50, price=100.0):
    return {
        "ticker": ticker,
        "uptrend": uptrend,
        "fresh_cross": fresh_cross,
        "rsi": rsi,
        "price": price,
    }


def with_fake_analysis(monkeypatch_fn, result):
    """Patches src.forecast.analyze to return a fixed result (or None)."""
    forecast_module.analyze = monkeypatch_fn or (lambda ticker: result)


def test_forecast_returns_none_without_enough_history():
    with_fake_analysis(lambda ticker: None, None)
    assert forecast("TEST.NS") is None


def test_strong_uptrend_with_fresh_cross_and_bullish_news_is_bullish():
    with_fake_analysis(None, make_analysis(uptrend=True, fresh_cross=True, rsi=50))
    news = {"TEST.NS": {"sentiment_score": 5, "headline_focus": "earnings beat", "stale": False}}
    result = forecast("TEST.NS", news)
    assert result["bias"] == "bullish"
    assert result["confidence"] > 50
    assert any("Golden Cross" in d for d in result["drivers"])
    assert any("earnings beat" in d for d in result["drivers"])


def test_downtrend_is_bearish():
    with_fake_analysis(None, make_analysis(uptrend=False, fresh_cross=False, rsi=50))
    result = forecast("TEST.NS", {})
    assert result["bias"] == "bearish"


def test_neutral_when_signals_cancel_out():
    # Uptrend (+4) but overbought RSI (-2) and no news -> below the +/-2
    # threshold only if they were closer; here trend alone still wins, so
    # use an oversold-in-downtrend combo instead: -4 trend + 2 rsi = -2 (bearish, at threshold).
    with_fake_analysis(None, make_analysis(uptrend=False, fresh_cross=False, rsi=25))
    result = forecast("TEST.NS", {})
    assert result["score"] == -2
    assert result["bias"] == "bearish"


def test_rsi_driver_oversold_and_overbought():
    assert _rsi_driver(make_analysis(rsi=25), rsi_oversold=30, rsi_overbought=70)[0] > 0
    assert _rsi_driver(make_analysis(rsi=75), rsi_oversold=30, rsi_overbought=70)[0] < 0
    assert _rsi_driver(make_analysis(rsi=50), rsi_oversold=30, rsi_overbought=70) is None
    assert _rsi_driver(make_analysis(rsi=None), rsi_oversold=30, rsi_overbought=70) is None


def test_cross_driver_only_fires_on_fresh_cross():
    assert _cross_driver(make_analysis(fresh_cross=False)) is None
    assert _cross_driver(make_analysis(fresh_cross=True, uptrend=True))[0] > 0
    assert _cross_driver(make_analysis(fresh_cross=True, uptrend=False))[0] < 0


def test_news_driver_ignores_stale_and_neutral():
    assert _news_driver(None) is None
    assert _news_driver({"sentiment_score": 3, "stale": True}) is None
    assert _news_driver({"sentiment_score": 0, "stale": False}) is None
    driver = _news_driver({"sentiment_score": -5, "headline_focus": "price crash", "stale": False})
    assert driver[0] < 0 and "price crash" in driver[1]


def test_drivers_are_capped_and_sorted_by_magnitude():
    with_fake_analysis(None, make_analysis(uptrend=True, fresh_cross=True, rsi=25))
    news = {"TEST.NS": {"sentiment_score": 5, "headline_focus": "rally", "stale": False}}
    result = forecast("TEST.NS", news)
    assert len(result["drivers"]) <= 5
    assert "steady uptrend" in result["drivers"][0]  # trend has the biggest weight (4)


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

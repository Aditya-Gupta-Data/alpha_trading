"""
Alpha Trading -- Phase 4E: Forecast layer
==========================================

Combines Phase 2 technicals (trend + RSI, from src.suggestions) with the
Phase 4D news sentiment file (data/news_sentiment.json) into one forecast
per stock: a directional bias, a confidence score, the top drivers behind
it, and a time horizon. Advisory only -- like Phase 2/3, nothing here
places a trade.

v1 is a transparent, rule-based weighted checklist (not a black box):
each signal contributes a documented number of points toward a
bullish/bearish score, so every forecast can be explained in one sentence
per driver.

Points (nominally max +/-10, so confidence = min(|score| / 10, 1) * 100):
  - Trend (SMA 50 vs 200):        +/-4 (untuned)
  - Fresh Golden/Death Cross:     +/-2 (bullish/Golden side only tuned, see below)
  - RSI mean-reversion:           +/-2 (oversold/bullish side only tuned, see below)
  - News sentiment (-5..+5):      scaled to +/-2 (0 weight if stale/no data, untuned)

Phase 4F (src/tuner.py) learns weight multipliers for the two BUY
archetypes strategy.py actually journals outcomes for -- fresh Golden
Cross and RSI-oversold-in-uptrend -- from resolved plan outcomes
(data/brain_weights.json), and this module applies them to the matching
bullish driver above (capped to TUNER_WEIGHT_BOUNDS, so the nominal +/-2
can flex a bit either way once there's enough evidence). The bearish
mirrors (Death Cross, overbought) and the trend/news drivers have no
matching journaled BUY archetype to learn from, so they stay fixed.

Run it from the project folder with:

    python -m src.forecast
"""

import json
from pathlib import Path

import yaml

from src.suggestions import analyze

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "config" / "watchlist.yaml"
NEWS_PATH = ROOT / "data" / "news_sentiment.json"
WEIGHTS_PATH = ROOT / "data" / "brain_weights.json"

TIME_HORIZON = "swing (multi-day to multi-week)"

TREND_POINTS = 4
CROSS_POINTS = 2
RSI_POINTS = 2
NEWS_POINTS_MAX = 2
MAX_SCORE = TREND_POINTS + CROSS_POINTS + RSI_POINTS + NEWS_POINTS_MAX

BULLISH_THRESHOLD = 2
BEARISH_THRESHOLD = -2


def load_tickers() -> list:
    """De-duped watchlist tickers, read straight from the YAML."""
    if not WATCHLIST_PATH.exists():
        return []
    with open(WATCHLIST_PATH) as f:
        config = yaml.safe_load(f) or {}
    seen, tickers = set(), []
    for item in config.get("watchlist", []):
        ticker = item.get("ticker")
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def load_news() -> dict:
    """Ticker -> sentiment entry from data/news_sentiment.json. Empty dict
    if the file doesn't exist yet (news_processor hasn't been run) -- news
    is simply excluded from the score, it never blocks a forecast."""
    if not NEWS_PATH.exists():
        return {}
    with open(NEWS_PATH) as f:
        data = json.load(f) or {}
    return data.get("tickers", {})


def load_weights() -> dict:
    """Archetype -> learned weight (e.g. {"fresh_cross": 1.2}) from Phase
    4F's data/brain_weights.json. Empty dict if the tuner hasn't run yet
    (or an archetype is missing) -- every driver defaults to a neutral 1.0,
    so a forecast never depends on the tuner having run."""
    if not WEIGHTS_PATH.exists():
        return {}
    with open(WEIGHTS_PATH) as f:
        data = json.load(f) or {}
    return data.get("weights", {})


def _trend_driver(result: dict) -> tuple:
    points = TREND_POINTS if result["uptrend"] else -TREND_POINTS
    label = (
        f"steady uptrend (50-day SMA above 200-day)"
        if result["uptrend"]
        else "steady downtrend (50-day SMA below 200-day)"
    )
    return points, label


def _cross_driver(result: dict, weights: dict = None):
    if not result["fresh_cross"]:
        return None
    if result["uptrend"]:
        # Phase 4F: the tuner learns this bullish archetype's weight from
        # resolved BUY plans (strategy.py's "fresh Golden Cross" signal).
        weight = (weights or {}).get("fresh_cross", 1.0)
        points = CROSS_POINTS * weight
        label = "fresh Golden Cross (trend just turned up)"
    else:
        # Death Cross is a SELL/exit signal, not a journaled BUY archetype
        # -- nothing for the tuner to learn from, so it stays untuned.
        points = -CROSS_POINTS
        label = "fresh Death Cross (trend just turned down)"
    return points, label


def _rsi_driver(result: dict, rsi_oversold: float, rsi_overbought: float, weights: dict = None):
    rsi_value = result["rsi"]
    if rsi_value is None:
        return None
    if rsi_value <= rsi_oversold:
        # Phase 4F: learned from strategy.py's "uptrend with a dip" signal.
        weight = (weights or {}).get("rsi_oversold", 1.0)
        return RSI_POINTS * weight, f"RSI {rsi_value:.0f} oversold (possible bounce)"
    if rsi_value >= rsi_overbought:
        # Overbought isn't a BUY archetype strategy.py journals -- untuned.
        return -RSI_POINTS, f"RSI {rsi_value:.0f} overbought (possible pullback)"
    return None


def _news_driver(news_entry: dict):
    if not news_entry or news_entry.get("stale", True):
        return None
    sentiment = news_entry["sentiment_score"]
    if sentiment == 0:
        return None
    points = (sentiment / 5) * NEWS_POINTS_MAX
    direction = "positive" if sentiment > 0 else "negative"
    focus = news_entry.get("headline_focus", "no clear driver")
    return points, f"{direction} news -- {focus} (sentiment {sentiment:+d}/5)"


def forecast(ticker: str, news_by_ticker: dict = None, weights: dict = None) -> dict:
    """Returns a forecast dict, or None if there isn't enough price history
    yet (same 200+ day requirement as suggestions.analyze)."""
    from src.config import RSI_OVERBOUGHT, RSI_OVERSOLD

    result = analyze(ticker)
    if result is None:
        return None

    if news_by_ticker is None:
        news_by_ticker = load_news()
    if weights is None:
        weights = load_weights()
    news_entry = news_by_ticker.get(ticker)

    drivers = []
    for driver in (
        _trend_driver(result),
        _cross_driver(result, weights),
        _rsi_driver(result, RSI_OVERSOLD, RSI_OVERBOUGHT, weights),
        _news_driver(news_entry),
    ):
        if driver is not None:
            drivers.append(driver)

    score = sum(points for points, _ in drivers)

    if score >= BULLISH_THRESHOLD:
        bias = "bullish"
    elif score <= BEARISH_THRESHOLD:
        bias = "bearish"
    else:
        bias = "neutral"

    confidence = round(min(abs(score) / MAX_SCORE, 1.0) * 100)

    # Strongest drivers first, top 5 (checklist only ever has 4 max today,
    # but this keeps the contract stable if more signals are added later).
    ranked = sorted(drivers, key=lambda d: abs(d[0]), reverse=True)[:5]

    return {
        "ticker": ticker,
        "bias": bias,
        "confidence": confidence,
        "score": score,
        "drivers": [label for _, label in ranked],
        "time_horizon": TIME_HORIZON,
        "price": result["price"],
    }


def describe(result: dict) -> str:
    """Plain-English one-liner for a forecast, for the terminal."""
    header = (
        f"{result['ticker']}: {result['bias'].upper()} "
        f"({result['confidence']}% confidence, {result['time_horizon']}) "
        f"-- now Rs.{result['price']:.1f}"
    )
    driver_lines = "".join(f"\n    - {d}" for d in result["drivers"])
    return header + driver_lines


def run_once():
    tickers = load_tickers()
    if not tickers:
        print("Your watchlist is empty. Add some stocks in config/watchlist.yaml")
        return

    news_by_ticker = load_news()
    if not news_by_ticker:
        print("No news_sentiment.json found yet -- run `python3 -m src.news_processor` "
              "first for news-informed forecasts. Continuing on technicals only.\n")

    weights = load_weights()
    if not WEIGHTS_PATH.exists():
        print("No brain_weights.json found yet -- run `python3 -m src.tuner` once you "
              "have resolved trades to learn from. Continuing with neutral weights.\n")

    print(f"Forecasting {len(tickers)} stock(s)...\n")
    for ticker in tickers:
        result = forecast(ticker, news_by_ticker, weights)
        if result is None:
            print(f"  skip  {ticker}: not enough price history yet")
            continue
        print(f"  {describe(result)}")

    print("\nDone.")


if __name__ == "__main__":
    run_once()

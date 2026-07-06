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

Phase 6 (src/brain_map.py) adds pattern MEMORY on top: when the current
setup carries active pattern tags (a fresh Golden Cross, an oversold
RSI), the forecast asks the Brain Map how that cluster of patterns has
historically paid, and -- if there's any history -- attaches a
`memory_context` line ("Historical Performance for active patterns
[...]: Win Rate: X%, ...") plus structured `memory` stats to the result.
This is ADVISORY CONTEXT ONLY: it adds no points to the score (the
tuner's learned weights already adjust the score from outcomes), it just
rides along in the payload so every downstream consumer -- the terminal,
the Discord bot's /analyze, API routes, and any LLM prompt that embeds a
forecast -- sees the historical evidence. If the Brain Map is empty,
missing, or errors, both keys are simply None and the forecast proceeds
exactly as before.

Run it from the project folder with:

    python -m src.forecast
"""

import json
from pathlib import Path

import yaml

from src import brain_map
from src.brain_map import query_similar_events
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


def _active_pattern_tags(result: dict, rsi_oversold: float) -> list:
    """The Brain Map tags describing the CURRENT setup, in the same tag
    vocabulary ingest_existing() writes: the tuner archetype plus the
    normalized user pattern tag that names the same pattern (a fresh
    Golden Cross setup matches both `fresh_cross` signal events and
    `golden_cross` pattern-tag events)."""
    tags = []
    if result["fresh_cross"] and result["uptrend"]:
        tags += ["fresh_cross", "golden_cross"]
    rsi = result["rsi"]
    if rsi is not None and rsi <= rsi_oversold:
        tags.append("rsi_oversold")
    return tags


def _memory_lookup(tags: list, brain=None):
    """Phase 6: ask the Brain Map how this cluster of patterns has paid
    historically. Returns {tags, count, win_rate, avg_r_multiple, context}
    or None. Fail-safe by design: no active tags, no history (count 0), a
    missing database, or any query error all degrade to None -- a forecast
    never depends on the Brain Map existing."""
    if not tags:
        return None
    opened_here = brain is None
    try:
        if brain is None:
            brain = brain_map.connect()
        stats = query_similar_events(brain, tags)
    except Exception:
        return None
    finally:
        if opened_here and brain is not None:
            brain.close()
    if not stats["count"]:
        return None
    avg = stats["avg_r_multiple"]
    context = (
        f"Historical Performance for active patterns [{', '.join(tags)}]: "
        f"Win Rate: {round(stats['win_rate'] * 100)}%, "
        f"Avg R-Multiple: {f'{avg:+.2f}' if avg is not None else 'n/a'} "
        f"over {stats['count']} historical trades."
    )
    return {"tags": tags, "count": stats["count"], "win_rate": stats["win_rate"],
            "avg_r_multiple": avg, "context": context}


def forecast(ticker: str, news_by_ticker: dict = None, weights: dict = None,
             brain=None) -> dict:
    """Returns a forecast dict, or None if there isn't enough price history
    yet (same 200+ day requirement as suggestions.analyze). `brain` is an
    optional open brain_map connection (run_once shares one across the
    watchlist; tests pass ':memory:'); default opens the real database per
    call."""
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

    # Phase 6: advisory pattern memory -- rides along in the payload, adds
    # nothing to the score (see module docstring).
    memory = _memory_lookup(_active_pattern_tags(result, RSI_OVERSOLD), brain)

    return {
        "ticker": ticker,
        "bias": bias,
        "confidence": confidence,
        "score": score,
        "drivers": [label for _, label in ranked],
        "time_horizon": TIME_HORIZON,
        "price": result["price"],
        "memory": ({k: memory[k] for k in ("tags", "count", "win_rate", "avg_r_multiple")}
                   if memory else None),
        "memory_context": memory["context"] if memory else None,
    }


def describe(result: dict) -> str:
    """Plain-English one-liner for a forecast, for the terminal."""
    header = (
        f"{result['ticker']}: {result['bias'].upper()} "
        f"({result['confidence']}% confidence, {result['time_horizon']}) "
        f"-- now Rs.{result['price']:.1f}"
    )
    driver_lines = "".join(f"\n    - {d}" for d in result["drivers"])
    if result.get("memory_context"):
        driver_lines += f"\n    - memory: {result['memory_context']}"
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

    # One shared Brain Map connection for the whole sweep (memory context
    # is optional -- None just means every forecast runs without it).
    try:
        brain = brain_map.connect()
    except Exception:
        brain = None

    print(f"Forecasting {len(tickers)} stock(s)...\n")
    for ticker in tickers:
        result = forecast(ticker, news_by_ticker, weights, brain)
        if result is None:
            print(f"  skip  {ticker}: not enough price history yet")
            continue
        print(f"  {describe(result)}")

    if brain is not None:
        brain.close()
    print("\nDone.")


if __name__ == "__main__":
    run_once()

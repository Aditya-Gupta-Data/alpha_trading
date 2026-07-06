"""
Phase 2: Suggestions.

Combines a trend signal (50-day vs 200-day moving average) with a momentum
signal (14-day RSI) into a plain-English read on each stock. Advisory only --
it never places a trade, it just tells you what it sees so you can decide.

Price history comes from the DhanHQ Data API (via src/dhan_client.py) as of
2026-07-06 — migrated off yfinance.
"""

from src.config import MOVING_AVERAGE_FAST, MOVING_AVERAGE_SLOW, RSI_OVERBOUGHT, RSI_OVERSOLD
from src.dhan_client import get_daily_closes
from src.indicators import sma, rsi

TREND_WINDOW_SHORT = MOVING_AVERAGE_FAST
TREND_WINDOW_LONG = MOVING_AVERAGE_SLOW
RSI_PERIOD = 14

_READS = {
    ("uptrend", "overbought"): "steady uptrend but stretched -- maybe wait for a pullback before adding.",
    ("uptrend", "neutral"): "steady uptrend, no red flags.",
    ("uptrend", "oversold"): "uptrend with a dip -- possible opportunity, worth a look.",
    ("downtrend", "overbought"): "downtrend with a bounce -- could be short-lived, stay cautious.",
    ("downtrend", "neutral"): "steady downtrend, no bottoming signs yet.",
    ("downtrend", "oversold"): "downtrend and beaten down -- could bounce, but confirm before buying.",
    ("uptrend", "unknown momentum"): "in an uptrend (momentum unclear).",
    ("downtrend", "unknown momentum"): "in a downtrend (momentum unclear).",
}


def _closing_prices(ticker: str):
    # ~1 year of daily closes (oldest first). We need 200+ for the slow SMA,
    # so ask for a generous window; Dhan returns only real trading days (no
    # empty/NaN rows to scrub, unlike Yahoo's pre-open placeholder rows).
    closes = get_daily_closes(ticker, days=400)
    return closes or None


def analyze(ticker: str):
    """Returns a dict with the current trend + momentum read, or None if
    there isn't enough price history yet (needs 200+ days)."""
    prices = _closing_prices(ticker)
    if prices is None or len(prices) < TREND_WINDOW_LONG + 1:
        return None

    uptrend_today = sma(prices, TREND_WINDOW_SHORT) > sma(prices, TREND_WINDOW_LONG)
    uptrend_yesterday = sma(prices[:-1], TREND_WINDOW_SHORT) > sma(prices[:-1], TREND_WINDOW_LONG)

    return {
        "ticker": ticker,
        "uptrend": uptrend_today,
        "fresh_cross": uptrend_today != uptrend_yesterday,
        "rsi": rsi(prices[-(RSI_PERIOD * 3):], RSI_PERIOD),
        "price": prices[-1],
    }


def describe(result: dict) -> str:
    """Turns an analyze() result into a plain-English suggestion line."""
    rsi_value = result["rsi"]
    trend = "uptrend" if result["uptrend"] else "downtrend"

    if rsi_value is None:
        zone = "unknown momentum"
    elif rsi_value >= RSI_OVERBOUGHT:
        zone = "overbought"
    elif rsi_value <= RSI_OVERSOLD:
        zone = "oversold"
    else:
        zone = "neutral"

    read = _READS.get((trend, zone), "no clear read.")

    prefix = ""
    if result["fresh_cross"]:
        prefix = f"[FRESH {'GOLDEN' if result['uptrend'] else 'DEATH'} CROSS] "

    rsi_text = f"RSI {rsi_value:.0f}" if rsi_value is not None else "RSI n/a"
    return (
        f"{prefix}{result['ticker']}: {trend}, {rsi_text} ({zone}) -- {read} "
        f"(now Rs.{result['price']:.1f})"
    )


def bucket(result: dict) -> str:
    """Groups a result into a plain-English category for the email digest:
    'opportunity', 'caution', or 'steady' (nothing to do)."""
    rsi_value = result["rsi"]
    overbought = rsi_value is not None and rsi_value >= RSI_OVERBOUGHT
    oversold = rsi_value is not None and rsi_value <= RSI_OVERSOLD

    if result["fresh_cross"] and result["uptrend"]:
        return "opportunity"
    if result["uptrend"] and oversold:
        return "opportunity"
    if result["fresh_cross"] and not result["uptrend"]:
        return "caution"
    if not result["uptrend"] and oversold:
        return "caution"
    if result["uptrend"] and overbought:
        return "caution"
    return "steady"


def plain_english_line(result: dict) -> str:
    """A single-sentence, jargon-free summary for one stock, for the email digest."""
    ticker, price = result["ticker"], result["price"]
    if result["fresh_cross"]:
        direction = "turned upward" if result["uptrend"] else "turned downward"
        return f"{ticker} (Rs.{price:.1f}) — its trend just {direction}. Worth a look."
    if result["uptrend"] and result["rsi"] is not None and result["rsi"] <= RSI_OVERSOLD:
        return f"{ticker} (Rs.{price:.1f}) — heading up overall, but had a recent dip. Possible buying window."
    if result["uptrend"] and result["rsi"] is not None and result["rsi"] >= RSI_OVERBOUGHT:
        return f"{ticker} (Rs.{price:.1f}) — heading up but risen fast. Might be due for a pause."
    if not result["uptrend"] and result["rsi"] is not None and result["rsi"] <= RSI_OVERSOLD:
        return f"{ticker} (Rs.{price:.1f}) — been falling, but may be oversold. Could bounce, confirm before buying."
    if result["uptrend"]:
        return f"{ticker} (Rs.{price:.1f}) — steady upward trend, nothing unusual."
    return f"{ticker} (Rs.{price:.1f}) — steady downward trend, no action needed."

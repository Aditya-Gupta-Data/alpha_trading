"""
Technical indicators used by the suggestion engine (Phase 2).

Both functions take a list of closing prices, oldest first.
"""


def sma(prices, window):
    """Simple moving average of the last `window` prices. None if not enough data."""
    if len(prices) < window:
        return None
    return sum(prices[-window:]) / window


def rsi(prices, period=14):
    """
    Wilder's RSI over `period` days. None if not enough data.
    0-100 scale: >70 = overbought, <30 = oversold, else neutral.
    """
    if len(prices) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

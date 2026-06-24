"""
Fetches recent price data for stocks using yfinance.

yfinance is free and needs no account or API key. It pulls from Yahoo Finance,
which can lag by ~15 minutes and occasionally hiccups — fine for alerts. When we
add live trading later, we'll swap in a real-time broker feed (Zerodha/Upstox).
"""

import yfinance as yf


def get_quote(ticker: str):
    """
    Return a small snapshot for one ticker, e.g.:
        {
            "ticker": "RELIANCE.NS",
            "current_price": 2987.5,
            "prev_close": 2950.0,
            "percent_change": 1.27,
        }
    Returns None if the data couldn't be fetched (bad ticker, network issue, etc.),
    so the rest of the program can just skip it and keep going.
    """
    try:
        data = yf.Ticker(ticker).history(period="2d")
    except Exception as e:
        print(f"  !  Could not fetch {ticker}: {e}")
        return None

    if data is None or data.empty:
        print(f"  !  No data for {ticker} — is the ticker spelled right? (e.g. TCS.NS)")
        return None

    closes = data["Close"].tolist()
    current_price = float(closes[-1])
    prev_close = float(closes[-2]) if len(closes) >= 2 else current_price

    percent_change = 0.0 if prev_close == 0 else (current_price - prev_close) / prev_close * 100

    return {
        "ticker": ticker,
        "current_price": round(current_price, 2),
        "prev_close": round(prev_close, 2),
        "percent_change": round(percent_change, 2),
    }

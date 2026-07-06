"""
Fetches recent price data for stocks via the DhanHQ Data API.

Migrated off yfinance (2026-07-06): prices now come from Dhan's real-time
market-data API through src/dhan_client.py. The public contract is unchanged
— get_quote(ticker) still returns {ticker, current_price, prev_close,
percent_change} or None — so every caller (alerts, watchlist, api) keeps
working without edits.

Note: Dhan prices instruments by security id, so only tickers present in
dhan_client.SECURITY_ID_MAP can be quoted; unknown tickers return None (the
callers already treat None as "skip this one").
"""

from src.dhan_client import get_quote  # re-exported; contract unchanged


if __name__ == "__main__":
    for t in ("RELIANCE.NS", "ONGC.NS", "TCS.NS"):
        print(t, get_quote(t))

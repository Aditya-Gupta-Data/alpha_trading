"""
src/dhan_client.py — DhanHQ market-data engine (DATA ONLY)
==========================================================

The single price/quote/OHLC source for the whole engine, replacing yfinance.
Uses the official dhanhq v2 SDK (DhanContext + dhanhq) keyed by
DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN from .env.

STRICT SAFETY RULE: this module calls ONLY market-data endpoints
(historical_daily_data, quote_data, option_chain). It NEVER calls order /
trade / fund-transfer methods, so the project's paper-only guarantee holds —
Dhan is used here strictly for data, exactly as VISION_PLAN Phase 8 scoped.

Security IDs below were pulled from Dhan's official scrip master
(api-scrip-master-detailed.csv) and verified, not guessed — a wrong id would
silently price the wrong stock. (Note: Dhan's own docs example maps ONGC to
2885, which is actually RELIANCE; the correct ONGC id is 2475.)

Public wrappers:
  get_daily_ohlc(ticker, days=5)   -> [{date, open, high, low, close, volume}]
  get_ohlc_since(ticker, start)    -> same, from an ISO start date (plan tracker)
  get_live_price(ticker)           -> float last traded price (or None)
  get_quote(ticker)                -> data_fetcher-compatible dict (or None)
  get_daily_closes(ticker, days)   -> [close, ...] oldest first (suggestions)
  get_option_chain(index_ticker, expiry_date)
  get_expiry_list(index_ticker)
"""

import time
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
_IST = timezone(timedelta(hours=5, minutes=30))

# Dhan's market-data endpoints are rate-limited (quotes ~1/sec). Callers loop
# over the watchlist, so a single retry after a short pause absorbs a
# transient "too many requests" without failing the whole refresh.
_RATE_PAUSE = 1.1


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_env()

# ticker -> Dhan instrument. `seg`/`inst` are the exact strings the SDK wants
# for quote_data securities keys and historical_daily_data arguments.
SECURITY_ID_MAP = {
    "RELIANCE.NS":   {"id": "2885",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "TCS.NS":        {"id": "11536", "seg": "NSE_EQ", "inst": "EQUITY"},
    "HDFCBANK.NS":   {"id": "1333",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "ICICIBANK.NS":  {"id": "4963",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "INFY.NS":       {"id": "1594",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "ONGC.NS":       {"id": "2475",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "HINDUNILVR.NS": {"id": "1394",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "ITC.NS":        {"id": "1660",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "MARUTI.NS":     {"id": "10999", "seg": "NSE_EQ", "inst": "EQUITY"},
    "TMPV.NS":       {"id": "3456",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "NIFTY 50":      {"id": "13",    "seg": "IDX_I",  "inst": "INDEX"},
    "NIFTY BANK":    {"id": "25",    "seg": "IDX_I",  "inst": "INDEX"},
    # id 21 verified against api-scrip-master-detailed.csv on 2026-07-06
    # (NSE, segment I, SYMBOL_NAME "INDIA VIX").
    "INDIA VIX":     {"id": "21",    "seg": "IDX_I",  "inst": "INDEX"},
}

# Friendly / legacy aliases -> a key in SECURITY_ID_MAP. Lets the rest of the
# engine keep passing the tickers it already uses (bare symbols from the demo
# trades, yfinance-style ^ index symbols, common index names).
_ALIASES = {
    "^NSEI": "NIFTY 50", "NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50",
    "^NSEBANK": "NIFTY BANK", "BANKNIFTY": "NIFTY BANK",
    "^INDIAVIX": "INDIA VIX", "INDIAVIX": "INDIA VIX", "VIX": "INDIA VIX",
}

_client = None


def _get_client():
    """Lazily build the dhanhq client. Returns None if creds are missing so
    callers can degrade instead of crashing at import."""
    global _client
    if _client is not None:
        return _client
    cid, token = os.environ.get("DHAN_CLIENT_ID"), os.environ.get("DHAN_ACCESS_TOKEN")
    if not cid or not token:
        return None
    from dhanhq import DhanContext, dhanhq
    _client = dhanhq(DhanContext(cid, token))
    return _client


def _resolve(ticker: str) -> dict | None:
    """Map any accepted ticker spelling to a SECURITY_ID_MAP instrument."""
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t in _ALIASES:
        t = _ALIASES[t]
    if t in SECURITY_ID_MAP:
        return SECURITY_ID_MAP[t]
    # bare NSE symbol like "TCS" -> "TCS.NS"
    if "." not in t and not t.startswith("^") and f"{t}.NS" in SECURITY_ID_MAP:
        return SECURITY_ID_MAP[f"{t}.NS"]
    return None


# ------------------------------------------------------------------ OHLC

def _fetch_daily(instr: dict, from_date: str, to_date: str) -> list:
    """Raw historical_daily_data -> list of bar dicts oldest first, or []."""
    client = _get_client()
    if client is None:
        return []
    resp = None
    for attempt in range(2):
        try:
            resp = client.historical_daily_data(
                instr["id"], instr["seg"], instr["inst"], from_date, to_date
            )
        except Exception as e:
            print(f"  Dhan historical fetch error: {e}")
            resp = None
        if isinstance(resp, dict) and resp.get("status") == "success":
            break
        if attempt == 0:
            time.sleep(_RATE_PAUSE)  # transient rate limit — retry once
    if not isinstance(resp, dict) or resp.get("status") != "success":
        print(f"  Dhan historical returned: {str(resp)[:160]}")
        return []
    d = resp.get("data") or {}
    ts = d.get("timestamp") or []
    bars = []
    for i in range(len(ts)):
        bars.append({
            "date": datetime.fromtimestamp(ts[i], tz=_IST).date().isoformat(),
            "open": float(d["open"][i]),
            "high": float(d["high"][i]),
            "low": float(d["low"][i]),
            "close": float(d["close"][i]),
            "volume": float(d["volume"][i]) if d.get("volume") else 0.0,
        })
    return bars


def get_daily_ohlc(ticker: str, days: int = 5) -> list:
    """Last `days` trading days of daily OHLC, oldest first. Fetches a padded
    calendar window (markets are closed on weekends/holidays) then trims."""
    instr = _resolve(ticker)
    if instr is None:
        return []
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=days * 2 + 10)).isoformat()
    bars = _fetch_daily(instr, from_date, to_date)
    return bars[-days:] if days and len(bars) > days else bars


def get_ohlc_since(ticker: str, start_iso: str) -> list:
    """All daily OHLC bars from `start_iso` (inclusive) to today, oldest
    first. Used by the plan tracker to resolve stop/target hits on real
    daily highs/lows (not a naive last price)."""
    instr = _resolve(ticker)
    if instr is None:
        return []
    # A trade opened today (or in the future) has no completed daily bar since
    # entry yet — and Dhan rejects a same-day/empty range with DH-905. Return
    # [] so the tracker cleanly waits for the next session (matches the old
    # yfinance "empty history" behaviour, just without the noisy error).
    if start_iso >= date.today().isoformat():
        return []
    return _fetch_daily(instr, start_iso, date.today().isoformat())


def get_daily_closes(ticker: str, days: int = 400) -> list:
    """Closing prices oldest first — the indicator engine (SMA/RSI) input."""
    return [b["close"] for b in get_daily_ohlc(ticker, days=days)]


# ----------------------------------------------------------------- quotes

def _quote_sec(ticker: str) -> dict | None:
    """The per-instrument quote dict from quote_data, or None."""
    instr = _resolve(ticker)
    client = _get_client()
    if instr is None or client is None:
        return None
    resp = None
    for attempt in range(2):
        try:
            resp = client.quote_data({instr["seg"]: [int(instr["id"])]})
        except Exception as e:
            print(f"  Dhan quote error for {ticker}: {e}")
            resp = None
        if isinstance(resp, dict) and resp.get("status") == "success":
            break
        if attempt == 0:
            time.sleep(_RATE_PAUSE)  # transient rate limit — retry once
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    try:
        return resp["data"]["data"][instr["seg"]][str(instr["id"])]
    except (KeyError, TypeError):
        return None


def get_live_price(ticker: str) -> float | None:
    sec = _quote_sec(ticker)
    if sec is None or sec.get("last_price") is None:
        return None
    return float(sec["last_price"])


def get_quote(ticker: str) -> dict | None:
    """Drop-in replacement for the old data_fetcher.get_quote — same shape:
    {ticker, current_price, prev_close, percent_change} or None."""
    sec = _quote_sec(ticker)
    if sec is None or sec.get("last_price") is None:
        return None
    last = float(sec["last_price"])
    prev = float((sec.get("ohlc") or {}).get("close") or last)
    pct = 0.0 if prev == 0 else (last - prev) / prev * 100
    return {
        "ticker": ticker,
        "current_price": round(last, 2),
        "prev_close": round(prev, 2),
        "percent_change": round(pct, 2),
    }


def get_india_vix() -> float | None:
    """Latest India VIX level, or None when unavailable (no creds, market
    data hiccup). Callers must treat None as "regime unknown" and fail
    safe — the Phase 5 strategy layer refuses to propose range-bound
    spreads without a VIX reading rather than assuming calm."""
    return get_live_price("INDIA VIX")


# ----------------------------------------------------------- option chain

def get_expiry_list(index_ticker: str) -> list:
    """List of ISO expiry date strings for an index underlying.

    Dhan's SDK wraps the payload in `{"status", "data": ...}` — but the
    inner `data` value has been observed DOUBLY nested
    (`{"data": {"data": [...], "status": ...}}`), not the single list the
    outer shape implies. Found live 2026-07-09: the single-unwrap version
    silently handed pick_expiry a dict instead of a list, which iterated
    its KEYS as if they were dates and matched nothing — every proposal
    cycle failed with "no usable expiry" regardless of real market
    conditions. Unwrap defensively so either shape (and a plain list, in
    case Dhan reverts) works, and anything else degrades to []."""
    instr = _resolve(index_ticker)
    client = _get_client()
    if instr is None or client is None:
        return []
    try:
        resp = client.expiry_list(int(instr["id"]), instr["seg"])
    except Exception as e:
        print(f"  Dhan expiry_list error: {e}")
        return []
    data = (resp or {}).get("data", []) if isinstance(resp, dict) else []
    if isinstance(data, dict):
        data = data.get("data", [])
    return data if isinstance(data, list) else []


def get_option_chain(index_ticker: str, expiry_date: str) -> dict | None:
    """Option chain for an index underlying at a given expiry (YYYY-MM-DD).

    Returns the flat {"last_price", "oc": {...}} dict options_proposer
    expects. Same doubly-nested SDK response as get_expiry_list
    (`{"data": {"data": {"last_price", "oc"}}}`) — found live 2026-07-09
    right after fixing that one; unwrap defensively here too."""
    instr = _resolve(index_ticker)
    client = _get_client()
    if instr is None or client is None:
        return None
    try:
        resp = client.option_chain(int(instr["id"]), instr["seg"], expiry_date)
    except Exception as e:
        print(f"  Dhan option_chain error: {e}")
        return None
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    data = resp.get("data")
    if isinstance(data, dict) and "oc" not in data and "data" in data:
        data = data.get("data")
    return data if isinstance(data, dict) else None


if __name__ == "__main__":
    # Manual smoke test: python3 -m src.dhan_client
    print("ONGC.NS quote:", get_quote("ONGC.NS"))
    print("TCS.NS last 3 daily bars:", get_daily_ohlc("TCS.NS", days=3))

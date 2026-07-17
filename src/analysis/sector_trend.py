"""
src/analysis/sector_trend.py — Top-Down sector trend + relative strength
========================================================================

SCAFFOLD (owner directive 2026-07-16): a fundamentally strong stock still
fails if its whole industry is in a downtrend. This module answers two
questions off the NSE sectoral indices and their constituents (mapped in
config/sector_universe.json):

  is_sector_bullish(sector)          — is the sector INDEX above its 200-SMA
                                       AND 50-SMA? (the industry "tide")
  get_relative_strength(ticker, sec) — is the stock leading or lagging its
                                       parent sector index on recent momentum?

Read-only, NOT wired into the engine / Regime Compass yet (a data + analysis
scaffold). Every data seam is injectable (`index_bars`, `stock_bars`) so it is
testable offline; the defaults read the local sector-index bars cache
(data/sector_index_bars.json, yfinance-sourced) and — for stocks — a supplied
bars provider (the live Dhan path is token-gated, so callers inject during the
sandbox phase). NULL-honest: missing/short data returns an explicit
`{"error": ...}`, never a guessed verdict.
"""
import json
from pathlib import Path

from src.indicators import sma

ROOT = Path(__file__).resolve().parent.parent.parent
SECTOR_UNIVERSE_PATH = ROOT / "config" / "sector_universe.json"
SECTOR_INDEX_BARS_PATH = ROOT / "data" / "sector_index_bars.json"

SMA_FAST, SMA_SLOW = 50, 200
RS_LOOKBACK = 63          # ~3 trading months of momentum


def load_universe(path=None) -> dict:
    path = Path(path) if path else SECTOR_UNIVERSE_PATH
    try:
        return json.loads(path.read_text()).get("sectors", {})
    except (OSError, ValueError):
        return {}


def _closes(bars) -> list:
    """bars = [(date, low, high, close), ...] -> [close, ...]."""
    return [float(b[3]) for b in bars]


def _index_bars_for(sector: str, universe: dict, index_bars_path=None):
    """The sector INDEX bars from the local cache via the universe's
    yahoo_index mapping. [] when absent."""
    meta = universe.get(sector)
    if not meta:
        return []
    sym = meta.get("yahoo_index")
    path = Path(index_bars_path) if index_bars_path else SECTOR_INDEX_BARS_PATH
    try:
        store = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return (store.get(sym) or {}).get("bars", [])


def _pct_return(closes, lookback):
    if len(closes) < lookback + 1 or closes[-1 - lookback] == 0:
        return None
    return round((closes[-1] / closes[-1 - lookback] - 1) * 100, 2)


def is_sector_bullish(sector_name: str, index_bars=None,
                      universe=None, index_bars_path=None,
                      universe_path=None) -> dict:
    """Verdict on the sector INDEX: bullish iff its latest close is above
    BOTH its 50-SMA and 200-SMA. `index_bars` injectable for tests."""
    universe = universe if universe is not None else load_universe(universe_path)
    if index_bars is None:
        index_bars = _index_bars_for(sector_name, universe, index_bars_path)
    closes = _closes(index_bars)
    if len(closes) < SMA_SLOW + 1:
        return {"sector": sector_name, "bullish": None,
                "error": f"insufficient index history ({len(closes)} bars, "
                         f"need {SMA_SLOW + 1})"}
    close = closes[-1]
    s50, s200 = sma(closes, SMA_FAST), sma(closes, SMA_SLOW)
    above50, above200 = close > s50, close > s200
    return {
        "sector": sector_name,
        "index": (universe.get(sector_name) or {}).get("yahoo_index"),
        "as_of": index_bars[-1][0],
        "close": round(close, 2),
        "sma50": round(s50, 2), "sma200": round(s200, 2),
        "above_sma50": above50, "above_sma200": above200,
        "bullish": bool(above50 and above200),
    }


def get_relative_strength(ticker: str, sector_name: str, lookback: int = RS_LOOKBACK,
                          stock_bars=None, index_bars=None,
                          universe=None, index_bars_path=None,
                          universe_path=None) -> dict:
    """Stock momentum vs its parent-sector-index momentum over `lookback`
    trading days. `leader` = stock outperformed its sector (RS spread > 0).
    `stock_bars` MUST be supplied while the live price path is token-gated."""
    universe = universe if universe is not None else load_universe(universe_path)
    if index_bars is None:
        index_bars = _index_bars_for(sector_name, universe, index_bars_path)
    if not stock_bars:
        return {"ticker": ticker, "sector": sector_name, "leader": None,
                "error": "no stock_bars supplied (live price path token-gated)"}
    if not index_bars:
        return {"ticker": ticker, "sector": sector_name, "leader": None,
                "error": f"no index bars for sector {sector_name}"}

    stock_ret = _pct_return(_closes(stock_bars), lookback)
    sector_ret = _pct_return(_closes(index_bars), lookback)
    if stock_ret is None or sector_ret is None:
        return {"ticker": ticker, "sector": sector_name, "leader": None,
                "error": f"insufficient history for a {lookback}-day return"}
    spread = round(stock_ret - sector_ret, 2)
    return {
        "ticker": ticker, "sector": sector_name,
        "index": (universe.get(sector_name) or {}).get("yahoo_index"),
        "lookback_days": lookback,
        "stock_return_pct": stock_ret, "sector_return_pct": sector_ret,
        "rs_spread_pct": spread,
        "leader": bool(spread > 0),
    }


if __name__ == "__main__":
    uni = load_universe()
    print(f"sector_trend scaffold — {len(uni)} sectors: {list(uni)}")
    for sec in uni:
        print(" ", json.dumps(is_sector_bullish(sec)))

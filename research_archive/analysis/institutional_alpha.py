"""
src/analysis/institutional_alpha.py — Smart-Money Accumulation VWAP-pullback
============================================================================

A STANDALONE entry edge (no SMA-dip): copy the big players and buy at their
defense line. Built on the 12.5-yr bulk/block deals ledger.

  ACCUMULATION : over a trailing window (30-60d) the stock shows heavy NET
                 institutional BUYING (block/bulk) — net-buy dominates gross.
  DEFENSE LINE : the volume-weighted average price (VWAP) of those institutional
                 BUY deals = where the big money is long from.
  TRIGGER      : price, having traded ABOVE the VWAP, PULLS BACK and tests it
                 (intraday dips to the VWAP but the daily close holds) -> buy
                 exactly at the institutions' average, where they defend.
  INVALIDATION : a daily CLOSE significantly below the VWAP = institutions are
                 underwater / dumping -> thesis dead, stop out.

Strict point-in-time: all deals used are dated STRICTLY BEFORE the decision day
(deals disclose post-close). Pure signal primitives here; the backtest that
resolves them into R-multiples lives in the sandbox. Read-only, NOT wired in.
"""
from datetime import date, timedelta

ACC_WINDOW = 45          # accumulation lookback (days)
MIN_BUY_DEALS = 2        # need a real footprint, not one print
MIN_NET_RATIO = 0.20     # net-buy must dominate gross (heavy accumulation)
STOP_PCT = 0.04          # close this far below the VWAP invalidates the thesis


def accumulation(deals_for_ticker: list, as_of: str, window: int = ACC_WINDOW) -> dict:
    """Institutional accumulation state + BUY-VWAP over [as_of-window, as_of).
    `accumulating` True only on heavy, multi-deal net buying. NULL-honest."""
    lo = (date.fromisoformat(as_of) - timedelta(days=window)).isoformat()
    w = [d for d in deals_for_ticker if lo <= d["as_of"] < as_of]
    buys = [d for d in w if d.get("side") == "buy"]
    buy_val = sum(d["value_rs"] for d in buys)
    sell_val = sum(d["value_rs"] for d in w if d.get("side") == "sell")
    gross = buy_val + sell_val
    net = buy_val - sell_val
    qty = sum(d["qty"] for d in buys)
    vwap = (sum(d["qty"] * d["price"] for d in buys) / qty) if qty > 0 else None
    accumulating = (len(buys) >= MIN_BUY_DEALS and net > 0 and gross > 0
                    and net / gross >= MIN_NET_RATIO and vwap is not None)
    return {"accumulating": accumulating, "vwap": round(vwap, 2) if vwap else None,
            "net_value_rs": round(net, 2), "n_buy_deals": len(buys)}


def pullback_trigger(prev_close: float, low: float, close: float,
                     vwap: float, stop_pct: float = STOP_PCT) -> bool:
    """True on the day price pulls back FROM ABOVE and TESTS the VWAP but the
    close HOLDS above the invalidation band. Buy here = the defense line."""
    if vwap is None:
        return False
    stop = vwap * (1 - stop_pct)
    return prev_close > vwap and low <= vwap and close >= stop

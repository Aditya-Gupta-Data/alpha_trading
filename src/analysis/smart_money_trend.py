"""
src/analysis/smart_money_trend.py — smart-money (bulk/block) trend signals
==========================================================================

SCAFFOLD (owner directive 2026-07-16): read the disclosed NSE bulk/block deal
ledger (already captured by src/ingestion/deals_tracker.py into
data/deals_history.jsonl) and turn it into institutional accumulation /
distribution signals for a ticker, point-in-time.

Two signals (owner spec):
  net_institutional_volume(ticker, as_of, window=90)
      — value-weighted net (buy₹ − sell₹) of large deals in a rolling window.
        Positive = accumulation, negative = distribution.
  block_deal_vwap(ticker, as_of, months=6)
      — VWAP of the MAJOR BLOCK BUYS (the ≥₹10cr negotiated window) over the
        recent window = a "smart-money floor". Price above it confirms bull.

Retail-noise handling: rather than a fragile client-name classifier we
(a) VALUE-weight the net (big institutional prints dominate) and (b) restrict
the VWAP floor to BLOCK deals (structurally ≥₹10cr, institutional by
construction). `smart_money_ok` is the Gate-5 verdict: accumulation OR price
above the block-VWAP floor; None (honest abstain) when the ledger has NO deals
for that ticker in the window.

STRICT POINT-IN-TIME: every window uses deals with `as_of` STRICTLY BEFORE the
decision day (deals disclose post-close, so day-D's own deal isn't "known"
when deciding at D's close). Read-only; NOT wired into the engine yet.
"""
from collections import defaultdict
from datetime import date, timedelta

from src.ingestion.deals_tracker import read_deal_history


def load_deals_by_ticker(path=None) -> dict:
    """The raw ledger -> {ticker: [deal, ...] sorted by as_of}. Reuses
    deals_tracker.read_deal_history (single source; no re-fetch)."""
    by = defaultdict(list)
    for r in read_deal_history(path):
        t, a = r.get("ticker"), r.get("as_of")
        if t and a and r.get("value_rs") is not None:
            by[t].append(r)
    for t in by:
        by[t].sort(key=lambda r: r["as_of"])
    return dict(by)


def _window(deals: list, as_of: str, days: int) -> list:
    """Deals in [as_of-days, as_of) — STRICTLY before as_of (no look-ahead)."""
    lo = (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()
    return [d for d in deals if lo <= d["as_of"] < as_of]


def net_institutional_volume(deals: list, as_of: str, window: int = 90) -> dict:
    w = _window(deals, as_of, window)
    buy = sum(d["value_rs"] for d in w if d.get("side") == "buy")
    sell = sum(d["value_rs"] for d in w if d.get("side") == "sell")
    return {"as_of": as_of, "window_days": window, "n_deals": len(w),
            "buy_value_rs": round(buy, 2), "sell_value_rs": round(sell, 2),
            "net_value_rs": round(buy - sell, 2),
            "accumulation": (buy - sell) > 0 if w else None}


def block_deal_vwap(deals: list, as_of: str, months: int = 6,
                    side: str = "buy", block_only: bool = True) -> dict:
    """VWAP of major block BUYS in the recent window — the smart-money floor."""
    w = _window(deals, as_of, months * 30)
    sel = [d for d in w if d.get("side") == side
           and (not block_only or d.get("deal_type") == "block")]
    qty = sum(d["qty"] for d in sel)
    notional = sum(d["qty"] * d["price"] for d in sel)
    vwap = notional / qty if qty > 0 else None
    return {"as_of": as_of, "window_days": months * 30, "side": side,
            "block_only": block_only, "n_deals": len(sel),
            "vwap": round(vwap, 2) if vwap else None}


def smart_money_ok(deals: list, as_of: str, current_price: float,
                   net_window: int = 90, vwap_months: int = 6) -> dict:
    """Gate-5 verdict: BUY-confirmed iff net institutional volume is positive
    OR current price is above the recent block-deal VWAP floor. Returns
    smart_money_ok=None (honest abstain) when there's NO deal data in-window —
    the caller decides whether 'no confirmation' blocks (strict) or passes."""
    niv = net_institutional_volume(deals, as_of, net_window)
    vw = block_deal_vwap(deals, as_of, vwap_months)
    accumulation = niv["accumulation"] is True
    above_vwap = vw["vwap"] is not None and current_price is not None \
        and current_price > vw["vwap"]
    has_data = niv["n_deals"] > 0 or vw["n_deals"] > 0
    return {"as_of": as_of, "current_price": current_price,
            "accumulation": accumulation, "net_value_rs": niv["net_value_rs"],
            "above_block_vwap": above_vwap, "block_vwap": vw["vwap"],
            "n_recent_deals": niv["n_deals"], "n_block_buys": vw["n_deals"],
            "smart_money_ok": (accumulation or above_vwap) if has_data else None}


if __name__ == "__main__":
    by = load_deals_by_ticker()
    print(f"smart_money_trend — ledger covers {len(by)} tickers")
    for t in ("KOTAKBANK.NS", "CIPLA.NS", "AXISBANK.NS"):
        d = by.get(t, [])
        if not d:
            print(f"  {t}: no deals"); continue
        as_of = d[-1]["as_of"]
        import json
        print(f"  {t} @ {as_of}: "
              f"{json.dumps(smart_money_ok(d, as_of, d[-1]['price']))}")

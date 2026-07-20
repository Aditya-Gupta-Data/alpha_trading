"""
15-minute intraday price snapshotter — READ-ONLY capture, never trades.

Phase-0 lake tap: every 15 minutes during market hours it fetches the live
price of each watchlist ticker and appends one JSON line per ticker to
`data/lake/intraday_15m.jsonl`. We are NOT trading on this yet (decision:
start collecting the substrate ASAP); it is pure capture for a future
intraday-feature layer.

Design rules (match the ingestion-department idioms):
  * READ-ONLY on all trade state — imports only the data-only dhan_client
    and the shared IST clock; no journal/portfolio/brain_map writes.
  * Market-hours self-gated (IST 09:15–15:30 via market_loop.is_market_open)
    so a coarse cron window can't capture junk off-hours.
  * Fail-open PER ticker — one dead quote never aborts the sweep; a failed
    ticker is counted, not raised.
  * Fully injectable (`price_fn`, `clock`, `tickers`, `out_path`) so it is
    testable offline with zero network — the whole department's convention.

Needs a VALID Dhan token to capture real prices. The Mac's token is
frequently expired (one active token per account, decision #48), so the
LIVE home for this cron is the VM. CLI: `python3 -m src.ingestion.intraday_tracker`.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "data" / "lake" / "intraday_15m.jsonl"

# How many failed ticker names a summary line may carry. Enough to diagnose
# the usual "the same 2 are always dead", short enough that a token outage
# (every ticker fails) doesn't write a wall of text every 15 minutes.
MAX_NAMED_FAILURES = 12


def watchlist_tickers(path: Path = None) -> list:
    """Deduped ticker list from config/watchlist.yaml (order-preserving)."""
    import yaml
    path = path or (ROOT / "config" / "watchlist.yaml")
    doc = yaml.safe_load(Path(path).read_text()) or {}
    seen, out = set(), []
    for row in doc.get("watchlist", []):
        t = (row or {}).get("ticker")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def capture(price_fn=None, clock=None, tickers=None,
            out_path=None, force: bool = False) -> dict:
    """One 15-minute snapshot. Returns a summary dict (never raises for a
    dead ticker). `force=True` bypasses the market-hours gate (tests)."""
    from src.market_loop import is_market_open, ist_now
    now = (clock or ist_now)()
    if not force and not is_market_open(now):
        return {"skipped": "market_closed", "ts": now.isoformat(),
                "captured": 0, "failed": 0}

    if price_fn is None:
        from src.dhan_client import get_live_price as price_fn
    if tickers is None:
        tickers = watchlist_tickers()
    out_path = Path(out_path or OUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ts = now.isoformat()
    rows, failed_tickers = [], []
    for t in tickers:
        try:
            px = price_fn(t)
        except Exception:
            px = None
        if px is None:
            failed_tickers.append(t)
            continue
        rows.append({"ts": ts, "ticker": t,
                     "price": round(float(px), 2), "src": "dhan_live_15m"})

    # Append-only lake write (one line per ticker); atomic enough for a
    # 15-min cadence — each line is a self-contained JSON record.
    with open(out_path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # NAME THE DEAD (2026-07-20). This used to report only `failed: 2`, so
    # the VM logged the same two silent tickers every 15 minutes for days
    # and no one could tell WHICH two without a live token to bisect with.
    # An anonymous failure count is a number you cannot act on; the names
    # turn it into a one-line diagnosis. Capped so a total-outage slot
    # cannot write an 84-name line every quarter hour.
    return {"ts": ts, "captured": len(rows), "failed": len(failed_tickers),
            "failed_tickers": failed_tickers[:MAX_NAMED_FAILURES],
            "tickers": len(tickers), "out": str(out_path)}


def capture_depth(quote_fn=None, clock=None, tickers=None,
                  out_path=None, force: bool = False) -> dict:
    """Phase-1 Order-Book Reality Check (FORWARD-ONLY scaffold): snapshot the
    Top-5 Bid/Ask depth per watchlist ticker so we can later study live
    spread / spoofing / smart-money footprints. Historical L2 is
    cost-prohibitive (acknowledged) — this only captures going forward.

    `quote_fn(ticker) -> quote dict` (default: dhan_client.get_quote's richer
    sibling) must expose the market-depth block. Dhan's quote_data returns a
    `depth`/`buy`/`sell` array per instrument; we store the top-5 levels +
    the derived best-bid/ask spread. Fail-open per ticker; needs a LIVE token
    (VM). Appends to data/lake/orderbook_15m.jsonl. NOT wired into trading."""
    from src.market_loop import is_market_open, ist_now
    now = (clock or ist_now)()
    if not force and not is_market_open(now):
        return {"skipped": "market_closed", "ts": now.isoformat()}
    if quote_fn is None:
        from src.dhan_client import get_quote as quote_fn      # depth added on VM path
    if tickers is None:
        tickers = watchlist_tickers()
    out_path = Path(out_path or (ROOT / "data" / "lake" / "orderbook_15m.jsonl"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ts = now.isoformat()
    rows, failed_tickers = [], []
    for t in tickers:
        try:
            q = quote_fn(t) or {}
            depth = q.get("depth") or {"buy": q.get("buy"), "sell": q.get("sell")}
            bids = (depth.get("buy") or [])[:5]
            asks = (depth.get("sell") or [])[:5]
        except Exception:
            bids = asks = None
        if not bids and not asks:
            failed_tickers.append(t)      # named, same reason as capture()
            continue
        best_bid = bids[0].get("price") if bids else None
        best_ask = asks[0].get("price") if asks else None
        spread = (best_ask - best_bid) if (best_bid and best_ask) else None
        rows.append({"ts": ts, "ticker": t, "best_bid": best_bid,
                     "best_ask": best_ask, "spread": spread,
                     "bids5": bids, "asks5": asks, "src": "dhan_depth_15m"})
    with open(out_path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return {"ts": ts, "captured": len(rows), "failed": len(failed_tickers),
            "failed_tickers": failed_tickers[:MAX_NAMED_FAILURES],
            "out": str(out_path)}


if __name__ == "__main__":
    print(json.dumps(capture()))

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


# ------------------------------------------------ date partitioning (08-05)
#
# `intraday_15m.jsonl` was ONE flat append-only file: 25,782 rows / 2.8 MB
# after three weeks, growing ~130 KB a day forever, on a VM with ~2 GB of
# free disk. It is also the only dataset in `data/lake/` that was NOT
# date-partitioned, so `lake.scan`/`read_day` could not read it and no
# consumer could ever ask for "just that day".
#
# Rotation reuses the lake's own layout — `<dataset>/date=YYYY-MM-DD/<name>`
# — so these files become readable through `lake.read_day("intraday_15m",
# day)` like every other dataset, with no new reader and no new format.
#
# `out_path` keeps its meaning as the BASE for every caller and test; the
# dated partition is derived from it, and the returned `out` is the real
# file written.

def partition_path(base_path, day: str):
    """`.../intraday_15m.jsonl` + a day -> `.../intraday_15m/date=<day>/part.jsonl`"""
    base = Path(base_path)
    dataset = base.parent / base.name[:-len(".jsonl")] if \
        base.name.endswith(".jsonl") else base.parent / base.name
    return dataset / f"date={day}" / "part.jsonl"


def append_rows(base_path, rows: list, day: str):
    """Append `rows` to the day's partition. Returns the path written."""
    target = partition_path(base_path, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return target


def migrate_flat_file(base_path=None, keep_original: bool = True) -> dict:
    """ONE-SHOT, LOSSLESS: fan the legacy flat file out into day partitions.

    Every row is routed by its OWN `ts` date, so the history keeps its true
    shape rather than all landing on the migration day. A row with no usable
    `ts` goes to `date=unknown` — never dropped and never guessed into a
    date it might not belong to.

    The original is RENAMED to `.migrated`, never deleted (RULE 4: look
    before you overwrite, and leave the evidence). Idempotent: with the flat
    file already gone this is a no-op.
    """
    base = Path(base_path or OUT_PATH)
    if not base.exists():
        return {"migrated": 0, "days": 0, "status": "no_flat_file"}
    by_day, bad = {}, 0
    for line in base.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            day = str(row.get("ts") or "")[:10]
            if len(day) != 10 or day[4] != "-":
                day, bad = "unknown", bad + 1
        except ValueError:
            row, day, bad = {"_unparsed": line}, "unknown", bad + 1
        by_day.setdefault(day, []).append(row)
    for day, rows in by_day.items():
        append_rows(base, rows, day)
    total = sum(len(v) for v in by_day.values())
    if keep_original:
        base.rename(base.with_suffix(base.suffix + ".migrated"))
    return {"migrated": total, "days": len(by_day), "undated": bad,
            "status": "ok"}

# How many failed ticker names a summary line may carry. Enough to diagnose
# the usual "the same 2 are always dead", short enough that a token outage
# (every ticker fails) doesn't write a wall of text every 15 minutes.
MAX_NAMED_FAILURES = 12

# One spaced second chance for a failed sweep slot. Long enough to fall out
# of the same rate-limit window the first pass tripped, short enough that a
# sweep still finishes far inside its 15-minute slot.
RETRY_SLEEP_SECONDS = 2.0


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


def desk_tickers(path=None) -> list:
    """The equity desk's OPEN funded positions — the names whose exit
    logic depends on a fresh price (owner directive 2026-08-04).

    They are NOT in `config/watchlist.yaml` (that is the options
    universe), so before this they were priced only on demand at card
    render time: a single transient fetch failure at 15:45 blanked the
    whole LAST/P&L column with no stored price to fall back on. Capturing
    them every 15 minutes gives the exit logic a durable series.

    VM-local read (decision #83), fail-open to an empty list — the
    watchlist sweep must never be lost to a desk-side error.

    MUZZLED UNDER PYTEST (same doctrine as `equity_desk.market_data_muzzled`
    and the brain_map seams). Without it this default silently reads the
    REAL open book from inside the suite: every existing `capture()` test
    that passes only `tickers=` suddenly grew 5 phantom desk failures.
    Tests inject `desk=` explicitly; nothing in the suite reaches live
    desk state through this door."""
    import os
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    try:
        from src import knowledge_graph_logger as kg
        out = []
        for ticker, entry in sorted(kg.open_positions(path=path).items()):
            if (entry.get("funding") or {}).get("funded"):
                out.append(ticker)
        return out
    except Exception as exc:
        print(f"  (desk tickers unavailable: {type(exc).__name__}: {exc})")
        return []


def capture(price_fn=None, clock=None, tickers=None,
            out_path=None, force: bool = False, sleep_fn=None,
            desk=None, desk_price_fn=None) -> dict:
    """One 15-minute snapshot. Returns a summary dict (never raises for a
    dead ticker). `force=True` bypasses the market-hours gate (tests);
    `sleep_fn` injects the retry pause (tests pass a no-op).

    Two universes, priced through DIFFERENT doors because they are keyed
    differently: the watchlist (`RELIANCE.NS`, priced by symbol through
    `dhan_client.get_live_price`) and the equity desk's open book
    (`FINEORG`, priced by scrip id through `equity_desk.live_quote`).
    Desk rows carry `src="dhan_live_15m_desk"` so a consumer can tell the
    two universes apart. `desk`/`desk_price_fn` are test seams."""
    from src.market_loop import is_market_open, ist_now
    now = (clock or ist_now)()
    if not force and not is_market_open(now):
        return {"skipped": "market_closed", "ts": now.isoformat(),
                "captured": 0, "failed": 0}

    if price_fn is None:
        from src.dhan_client import get_live_price as price_fn
    if tickers is None:
        tickers = watchlist_tickers()
    if desk is None:
        desk = desk_tickers()
    if desk_price_fn is None:
        from src.equity_desk import live_quote as desk_price_fn
    # A desk name already in the watchlist is captured once, by the
    # watchlist door — never priced twice on one sweep.
    desk = [t for t in desk if t not in set(tickers)]
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

    # SECOND CHANCE FOR THE DEAD (2026-07-22, bug-ledger triage). The
    # failure bursts cluster by SLOT, not by scrip — 15-26 big names dead
    # at 11:00 and all fine at 11:15 — which is another job sharing the
    # Dhan rate limit, not dead tickers. One spaced retry pass inside the
    # same sweep recovers those; a ticker that fails BOTH passes stays a
    # named failure, so the fail-open contract is unchanged.
    recovered = 0
    if failed_tickers:
        import time
        (sleep_fn or time.sleep)(RETRY_SLEEP_SECONDS)
        still_failed = []
        for t in failed_tickers:
            try:
                px = price_fn(t)
            except Exception:
                px = None
            if px is None:
                still_failed.append(t)
                continue
            rows.append({"ts": ts, "ticker": t,
                         "price": round(float(px), 2),
                         "src": "dhan_live_15m"})
            recovered += 1
        failed_tickers = still_failed

    # THE DESK PASS (2026-08-04). Same fail-open contract, same one
    # spaced retry, priced through the scrip-id door. Runs after the
    # watchlist so a desk-side fault can never cost the main sweep.
    desk_failed = []
    for t in desk:
        try:
            px = desk_price_fn(t)
        except Exception:
            px = None
        if px is None:
            desk_failed.append(t)
            continue
        rows.append({"ts": ts, "ticker": t, "price": round(float(px), 2),
                     "src": "dhan_live_15m_desk"})
    if desk_failed:
        import time
        (sleep_fn or time.sleep)(RETRY_SLEEP_SECONDS)
        still = []
        for t in desk_failed:
            try:
                px = desk_price_fn(t)
            except Exception:
                px = None
            if px is None:
                still.append(t)
                continue
            rows.append({"ts": ts, "ticker": t,
                         "price": round(float(px), 2),
                         "src": "dhan_live_15m_desk"})
            recovered += 1
        desk_failed = still

    # Append-only lake write (one line per ticker); atomic enough for a
    # 15-min cadence — each line is a self-contained JSON record. Since
    # 2026-08-05 it lands in the DAY's partition (see partition_path) so the
    # file cannot grow without bound and lake.read_day can read it.
    out_path = append_rows(out_path, rows, ts[:10])

    # NAME THE DEAD (2026-07-20). This used to report only `failed: 2`, so
    # the VM logged the same two silent tickers every 15 minutes for days
    # and no one could tell WHICH two without a live token to bisect with.
    # An anonymous failure count is a number you cannot act on; the names
    # turn it into a one-line diagnosis. Capped so a total-outage slot
    # cannot write an 84-name line every quarter hour.
    return {"ts": ts, "captured": len(rows),
            "failed": len(failed_tickers) + len(desk_failed),
            "failed_tickers": (failed_tickers + desk_failed)[:MAX_NAMED_FAILURES],
            "recovered": recovered,
            "desk_tickers": len(desk), "desk_failed": len(desk_failed),
            "tickers": len(tickers) + len(desk), "out": str(out_path)}


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
    out_path = append_rows(out_path, rows, ts[:10])
    return {"ts": ts, "captured": len(rows), "failed": len(failed_tickers),
            "failed_tickers": failed_tickers[:MAX_NAMED_FAILURES],
            "out": str(out_path)}


# --------------------------------------------------- the darling day-tap

DARLINGS_OUT_PATH = ROOT / "data" / "lake" / "darlings_daily.jsonl"


def darling_universe(ids_path=None) -> list:
    """Every darling symbol in the Mac-built scrip-id artifact — the FULL
    watch universe, not just the funded book."""
    from src.equity_desk import DARLING_IDS_PATH
    p = Path(ids_path) if ids_path else DARLING_IDS_PATH
    try:
        return sorted((json.loads(p.read_text()).get("ids") or {}).keys())
    except Exception as exc:
        print(f"  (darling universe unavailable: {type(exc).__name__}: {exc})")
        return []


def capture_darlings(price_fn=None, clock=None, tickers=None, out_path=None,
                     force: bool = False, sleep_fn=None) -> dict:
    """ONE daily close-of-session price for EVERY darling — including the
    ones we do not hold (owner directive 2026-08-04).

    WHY THIS EXISTS: the 15-minute sweep covers the watchlist plus the
    desk's OPEN book, so a darling we are merely waiting on was never
    priced by anything on the VM. Entry logic needs to see a name reach
    its buying zone, and until now that depended entirely on the Mac's
    19:15 bhavcopy chain — which only fires on days the Mac happens to be
    awake (verified 2026-08-04: 4 of 11 recent weekdays missed, no log
    line at all on the missed days). The VM is always up, so the entry
    signal now has a home that does not depend on a laptop's sleep
    schedule. Complements bhavcopy, does not replace it: bhavcopy carries
    the whole exchange with OHLC, this carries ~105 names with one close.

    Prices through the same scrip-id door as the desk, so an unmapped
    symbol is a NAMED failure, never a guessed price. One spaced retry
    pass, same as the 15-minute sweep. Append-only."""
    from src.market_loop import ist_now
    now = (clock or ist_now)()
    if not force and now.weekday() >= 5:
        return {"skipped": "weekend", "ts": now.isoformat(),
                "captured": 0, "failed": 0}
    if price_fn is None:
        from src.equity_desk import live_quote as price_fn
    if tickers is None:
        tickers = darling_universe()
    out_path = Path(out_path or DARLINGS_OUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ts = now.isoformat()
    day = now.date().isoformat()
    rows, failed = [], []
    for t in tickers:
        try:
            px = price_fn(t)
        except Exception:
            px = None
        if px is None:
            failed.append(t)
            continue
        rows.append({"ts": ts, "day": day, "ticker": t,
                     "price": round(float(px), 2), "src": "dhan_darling_daily"})
    recovered = 0
    if failed:
        import time
        (sleep_fn or time.sleep)(RETRY_SLEEP_SECONDS)
        still = []
        for t in failed:
            try:
                px = price_fn(t)
            except Exception:
                px = None
            if px is None:
                still.append(t)
                continue
            rows.append({"ts": ts, "day": day, "ticker": t,
                         "price": round(float(px), 2),
                         "src": "dhan_darling_daily"})
            recovered += 1
        failed = still

    out_path = append_rows(out_path, rows, day)
    return {"ts": ts, "day": day, "captured": len(rows), "failed": len(failed),
            "failed_tickers": failed[:MAX_NAMED_FAILURES],
            "recovered": recovered, "tickers": len(tickers),
            "out": str(out_path)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--darlings", action="store_true",
                    help="run the DAILY all-darlings tap instead of the "
                         "15-minute watchlist+desk sweep")
    args = ap.parse_args()
    print(json.dumps(capture_darlings() if args.darlings else capture()))

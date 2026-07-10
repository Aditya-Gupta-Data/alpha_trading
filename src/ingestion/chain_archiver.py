"""
src/ingestion/chain_archiver.py — EOD option-chain capture-forward archive
==========================================================================

Phase 0 of docs/HOLY_GRAIL_PLAN.md — the one dataset in the whole design
space with a hard irreversibility clock. Decision #36 documents that
historical option chains are NOT retrievable, which is why the simulator,
tracker, and planner all live in a synthetic intrinsic+time-value pricing
world. Every trading day this job doesn't run is a day of real IV surface,
real OI distribution, and real bid/ask spreads lost forever. After months
of capture: a real-chain simulator mode for recent windows, slippage tiers
calibrated against observed spreads, and OI/max-pain/IV-skew features —
exactly the entry-filter-orthogonal features decision #50 said the skeptic
needs.

Discipline:
  * Runs ONCE daily post-close (cron ~15:40 IST, after master_scheduler
    self-terminates at 15:30) so it never contends with the live loop for
    the single Dhan token (#48/#56) and the chain reflects the close.
  * Throttled between expiry requests (Dhan's chain endpoint is
    rate-limited ~1 req/3s).
  * Fail-open: no token, a dead endpoint, one bad expiry — capture what
    answered, log what didn't, never raise. Weekend/holiday runs write
    nothing (markets closed = nothing new to capture).
  * Writes ONLY its lake partitions:
        data/lake/chains/<slug>/date=YYYY-MM-DD/part.jsonl.gz
    one row per expiry: {underlying, expiry, spot, vix, captured_at, oc}.
  * Heartbeat-monitored: chain_archiver.log is in ops_monitor's
    EXPECTED_JOBS, so silent failure surfaces on the nightly health card.

Manual check:  python3 -m src.ingestion.chain_archiver
"""

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import lake

ROOT = Path(__file__).resolve().parent.parent.parent

# The two liquid index chains the engine actually trades (Phase 5 scope).
UNDERLYINGS = {"NIFTY 50": "nifty", "NIFTY BANK": "banknifty"}

# Capture the nearest N expiries per underlying: the active weekly/monthly
# contracts where all trading (and all future feature value) concentrates.
MAX_EXPIRIES = 4
THROTTLE_SECONDS = 3.0

IST = timezone(timedelta(hours=5, minutes=30))


def _is_weekday(day: date) -> bool:
    return day.weekday() < 5


def capture_underlying(underlying: str, slug: str, today: date,
                       expiry_fn=None, chain_fn=None, spot_fn=None,
                       vix_fn=None, sleep_fn=time.sleep,
                       max_expiries: int = MAX_EXPIRIES) -> list:
    """Snapshot one underlying's nearest expiries into archive rows.
    Injectable fetchers for offline tests. Returns the rows captured
    ([] when nothing answered). Never raises."""
    if expiry_fn is None or chain_fn is None or spot_fn is None or vix_fn is None:
        from src.dhan_client import (get_expiry_list, get_india_vix,
                                     get_live_price, get_option_chain)
        expiry_fn = expiry_fn or get_expiry_list
        chain_fn = chain_fn or get_option_chain
        spot_fn = spot_fn or get_live_price
        vix_fn = vix_fn or get_india_vix

    try:
        expiries = [e for e in (expiry_fn(underlying) or [])
                    if isinstance(e, str)]
    except Exception as exc:
        print(f"  (chain archiver: expiry list failed for {underlying} "
              f"[{exc}])")
        return []
    # Nearest first; drop already-past expiries defensively.
    expiries = sorted(e for e in expiries if e >= today.isoformat())
    expiries = expiries[:max_expiries]
    if not expiries:
        print(f"  (chain archiver: no expiries answered for {underlying})")
        return []

    try:
        spot = spot_fn(underlying)
    except Exception:
        spot = None
    try:
        vix = vix_fn()
    except Exception:
        vix = None

    rows, captured_at = [], datetime.now(IST).isoformat(timespec="seconds")
    for i, expiry in enumerate(expiries):
        if i:
            sleep_fn(THROTTLE_SECONDS)
        try:
            chain = chain_fn(underlying, expiry)
        except Exception as exc:
            print(f"  (chain archiver: {underlying} {expiry} failed [{exc}])")
            continue
        if not isinstance(chain, dict) or not chain.get("oc"):
            print(f"  (chain archiver: {underlying} {expiry} — empty chain)")
            continue
        rows.append({
            "underlying": underlying,
            "slug": slug,
            "expiry": expiry,
            "spot": spot if spot is not None else chain.get("last_price"),
            "vix": vix,
            "captured_at": captured_at,
            "oc": chain["oc"],
        })
    return rows


def run(today: date = None, lake_root=None, force: bool = False,
        **fetchers) -> dict:
    """The daily entry point: capture every underlying's chains into the
    lake. Skips weekends unless force. Returns a summary dict (also
    printed — this module's log IS its heartbeat). Never raises."""
    today = today or date.today()
    summary = {"date": today.isoformat(), "captured": {}, "skipped": None}
    if not _is_weekday(today) and not force:
        summary["skipped"] = "weekend"
        print(f"(chain archiver: {today} is a weekend — nothing to capture)")
        return summary
    for underlying, slug in UNDERLYINGS.items():
        rows = capture_underlying(underlying, slug, today, **fetchers)
        if rows:
            path = lake.write_partition(f"chains/{slug}", today.isoformat(),
                                        rows, root=lake_root)
            summary["captured"][underlying] = len(rows) if path else 0
            print(f"(chain archiver: {underlying} — {len(rows)} expiry "
                  f"snapshot(s) -> lake)")
        else:
            summary["captured"][underlying] = 0
            print(f"(chain archiver: {underlying} — nothing captured)")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))

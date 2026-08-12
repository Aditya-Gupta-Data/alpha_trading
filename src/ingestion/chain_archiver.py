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

A HEARTBEAT IS NOT A CAPTURE (fixed 2026-08-13). The lake-depth audit found
2026-08-05 written as:

    {"date":"2026-08-05","captured":{"NIFTY 50":0,"NIFTY BANK":0},
     "skipped":null}

The job RAN, so its heartbeat was green and ops_monitor said nothing; every
other day that week captured 4/4 expiries. A whole trading day's IV surface
and OI distribution went missing in a line that reads like success — on the
ONE dataset in this repo that can never be re-bought (decision #36). `0` and
`null` are not an outage report.

So an empty capture is now a NAMED, LOUD skip:
  * per underlying   CA-EMPTY  — nothing came back for that name
  * whole-day        CA-BLACKOUT — every underlying empty on a trading day,
                     which is a token/endpoint failure, not nine coincidences
Both print the word UNAVAILABLE, which is what `ops_monitor.PROBLEM_PATTERNS`
matches, so the nightly card carries them without ops_monitor needing to know
this module exists. `summary["skipped"]` stops being `null` on a bad day and
carries the code; `summary["empty"]` lists the names. Still never raises —
the clock must not die over a bad capture (rule 8, ARCHITECTURE.md).

Manual check:  python3 -m src.ingestion.chain_archiver
"""

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import lake

ROOT = Path(__file__).resolve().parent.parent.parent

# EXPANDED 2026-08-07 from the two Phase-5 index chains to the WHOLE live
# universe. The desk went 2 -> 9 underlyings on 08-05, but only NIFTY and
# BANKNIFTY chains were being captured, so `ghost_tracker` could not price
# a refused FINNIFTY or equity-option trade at all — and decision #36's
# clock applies to every one of them equally: a chain not captured today
# is not retrievable tomorrow.
#
# Slugs are the lake directory names and are PERMANENT: `chains/nifty` and
# `chains/banknifty` already hold history, so those two keep their
# original slugs rather than being renamed to match a new convention.
UNDERLYINGS = {
    "NIFTY 50": "nifty",
    "NIFTY BANK": "banknifty",
    "NIFTY FIN SERVICE": "finnifty",
    "NIFTY MID SELECT": "midcpnifty",
    "RELIANCE.NS": "reliance",
    "HDFCBANK.NS": "hdfcbank",
    "ICICIBANK.NS": "icicibank",
    "INFY.NS": "infy",
    "TCS.NS": "tcs",
}

# Capture the nearest N expiries per underlying: the active weekly/monthly
# contracts where all trading (and all future feature value) concentrates.
#
# 3 for everything except NIFTY. FINNIFTY, MIDCPNIFTY and all five equity
# names are MONTHLY-ONLY (measured against the scrip master on 08-05), so
# "the nearest 4" reached four months out into contracts nobody trades.
# NIFTY still carries weeklies — the near-dated surface — so it keeps 4.
#
# RAISED 2 -> 3 on 2026-08-11, and the ghost book is what raised it: on
# 08-10 the desk refused equity spreads written on the 2026-10-27 expiry,
# which is the THIRD monthly. At depth 2 the archive stopped at 09-29, so
# seven of nineteen ghosts came back NO_CHAIN_ARCHIVE — the refusals were
# real trades the engine had built and we could not say what they would
# have done. The proposer reaches the third monthly because `horizon_for`
# can ask for 90+ days; the archive has to reach as far as the proposer
# does or the ghost book has a hole exactly where the long-horizon trades
# are. Cost is ~9 extra chain calls (~30s) and ~50 KB/day.
MAX_EXPIRIES = 3
MAX_EXPIRIES_BY_UNDERLYING = {"NIFTY 50": 4}
THROTTLE_SECONDS = 3.0

# RATE-LIMIT HEADROOM (2026-08-07). Going 2 -> 9 underlyings turns ~10
# chain calls into ~28, on an account with ONE rate budget. Two protections
# already exist and both still apply: `dhan_client._throttle()` spaces
# EVERY Dhan call on the host >= 1.1s apart across processes (the DH-905
# fix), and this job runs at 15:40 IST — after the scheduler self-
# terminates at 15:30 — so it never competes with the live loop.
# This adds the third: a pause between underlyings, so a nine-underlying
# sweep is a slow drip rather than nine back-to-back bursts. Worst case
# ~28 chain calls x 3s + 8 x 5s = ~2 minutes, entirely post-close.
UNDERLYING_PAUSE_SECONDS = 5.0

IST = timezone(timedelta(hours=5, minutes=30))


def _is_weekday(day: date) -> bool:
    return day.weekday() < 5


def expiries_wanted(underlying: str) -> int:
    """How many expiries are worth capturing for this underlying — 4 for
    the only index still carrying weeklies, 2 for the monthly-only rest."""
    return MAX_EXPIRIES_BY_UNDERLYING.get(underlying, MAX_EXPIRIES)


def capture_underlying(underlying: str, slug: str, today: date,
                       expiry_fn=None, chain_fn=None, spot_fn=None,
                       vix_fn=None, sleep_fn=time.sleep,
                       max_expiries: int = None) -> list:
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
    expiries = expiries[:(max_expiries if max_expiries is not None
                          else expiries_wanted(underlying))]
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
    summary = {"date": today.isoformat(), "captured": {}, "skipped": None,
               "empty": []}
    if not _is_weekday(today) and not force:
        summary["skipped"] = "weekend"
        print(f"(chain archiver: {today} is a weekend — nothing to capture)")
        return summary
    sleep_fn = fetchers.get("sleep_fn") or time.sleep
    for i, (underlying, slug) in enumerate(UNDERLYINGS.items()):
        if i:
            # Nine underlyings back-to-back is a burst; this makes it a
            # drip. Post-close, so the wall-clock cost buys nothing back
            # from anyone.
            sleep_fn(UNDERLYING_PAUSE_SECONDS)
        rows = capture_underlying(underlying, slug, today, **fetchers)
        path = None
        if rows:
            path = lake.write_partition(f"chains/{slug}", today.isoformat(),
                                        rows, root=lake_root)
        if rows and path:
            summary["captured"][underlying] = len(rows)
            print(f"(chain archiver: {underlying} — {len(rows)} expiry "
                  f"snapshot(s) -> lake)")
        else:
            # A zero here is a PERMANENT hole, not a quiet nothing. Name it
            # with a word ops_monitor's PROBLEM_PATTERNS actually matches;
            # "nothing captured" matched none of them and that is how
            # 2026-08-05 passed as a healthy night.
            summary["captured"][underlying] = 0
            summary["empty"].append(underlying)
            reason = ("lake write failed" if rows else
                      "no expiry answered")
            print(f"  (chain archiver: CA-EMPTY {underlying} {today} — "
                  f"{reason}, chain UNAVAILABLE and NOT recoverable later)")

    if summary["empty"]:
        summary["skipped"] = (
            "CA-BLACKOUT" if len(summary["empty"]) == len(UNDERLYINGS)
            else "CA-EMPTY")
        if summary["skipped"] == "CA-BLACKOUT":
            # Nine independent underlyings do not fail together by chance.
            # This shape is a dead token or a dead endpoint, and it is the
            # exact shape 2026-08-05 had.
            print(f"  (chain archiver: CA-BLACKOUT {today} — ALL "
                  f"{len(UNDERLYINGS)} underlyings empty; the whole trading "
                  f"day's chains are UNAVAILABLE. Suspect the token or the "
                  f"endpoint, not the market.)")
        else:
            print(f"  (chain archiver: CA-EMPTY {today} — "
                  f"{len(summary['empty'])}/{len(UNDERLYINGS)} underlyings "
                  f"UNAVAILABLE: {', '.join(summary['empty'])})")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))

"""
Alpha Trading — the market loop: automated ingestion + headless proposals
=========================================================================

A continuous async daemon that watches the option-enabled indices during
NSE market hours and, when the engine finds a favorable setup, fires the
options proposer in HEADLESS mode: the rich 🚨 alert lands in Discord and
the proposal is journaled as PENDING_APPROVAL — the human decision still
happens later in the terminal (decision #11's human-in-the-loop rule is
about execution, and nothing here executes anything).

Design rules:
  * Market hours only — Mon-Fri 09:15-15:30 IST; outside that window the
    loop just sleeps. All timing is IST regardless of host timezone.
  * `fetch_market_state()` is THE abstraction seam: it returns plain
    build_proposal keyword overrides, so the Phase 7 simulator can inject
    historical state through the exact same interface (decision #30 is
    untouched: the state is fetched by pure-Python indicators/quotes; no
    LLM anywhere in this loop).
  * Cool-down: after a successful proposal an index goes quiet for
    COOLDOWN_SECONDS (default 2h) so Discord isn't spammed while the
    market lingers on the same setup. Blocked/no-signal cycles do NOT arm
    the cool-down — the next favorable read may come any time.
  * Fail-safe: one bad cycle (Dhan hiccup, dead chain, anything) is
    printed and skipped; the loop never dies.

Run as a daemon from the project folder:

    python3 -m src.market_loop
"""

import asyncio
from datetime import datetime, time as dtime, timedelta, timezone

from src import options_proposer as proposer
from src.dhan_client import get_india_vix
from src.suggestions import analyze

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

POLL_INTERVAL_SECONDS = 900     # look at the market every 15 minutes
COOLDOWN_SECONDS = 7200         # at most one proposal per index per 2 hours
UNDERLYINGS = ("NIFTY 50", "NIFTY BANK")


def ist_now() -> datetime:
    return datetime.now(IST)


def is_market_open(now: datetime = None) -> bool:
    """Mon-Fri, 09:15-15:30 IST inclusive. (NSE holidays are not modeled —
    a holiday cycle just finds no fresh data and proposes nothing.)"""
    now = now or ist_now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def fetch_market_state(underlying: str) -> dict | None:
    """The abstract ingestion step: everything the proposer needs to know
    about the market right now, as build_proposal keyword overrides.

    THE injection seam for Phase 7: the simulator replaces this function
    (pass fetch_fn= to run_market_loop) with one that serves historical
    analysis/VIX/chain state — nothing else in the pipeline changes.

    Live implementation: trend analysis (SMA/RSI over Dhan daily closes)
    + India VIX; expiry and option chain are left to build_proposal's own
    (already injectable) fetchers. Returns None when the underlying can't
    be analyzed (not enough history / data outage) — skip the cycle."""
    analysis = analyze(underlying)
    if analysis is None:
        return None
    return {"analysis": analysis, "vix": get_india_vix()}


class CooldownRegistry:
    """Per-key rate limiter: ready() until arm()ed, then quiet for
    `seconds`. Pure and clock-injectable for tests."""

    def __init__(self, seconds: float = COOLDOWN_SECONDS):
        self.seconds = seconds
        self._last: dict = {}

    def ready(self, key: str, now: datetime = None) -> bool:
        now = now or ist_now()
        last = self._last.get(key)
        return last is None or (now - last).total_seconds() >= self.seconds

    def arm(self, key: str, now: datetime = None) -> None:
        self._last[key] = now or ist_now()


async def run_market_loop(underlyings=UNDERLYINGS,
                          interval: float = POLL_INTERVAL_SECONDS,
                          cooldown: CooldownRegistry = None,
                          fetch_fn=fetch_market_state,
                          propose_fn=None,
                          now_fn=ist_now) -> None:
    """The daemon. Every `interval` seconds during market hours: fetch
    each index's state, and when the engine likes the setup, trigger the
    headless proposer (Discord alert + PENDING_APPROVAL journal entry).
    Everything is injectable: the simulator swaps fetch_fn/now_fn, tests
    swap all of it."""
    cooldown = cooldown or CooldownRegistry()
    propose_fn = propose_fn or proposer.run_headless
    print(f"[Market Loop] armed — {', '.join(underlyings)} every "
          f"{interval:g}s during 09:15-15:30 IST, cool-down "
          f"{cooldown.seconds / 3600:g}h per index.", flush=True)

    while True:
        now = now_fn()
        if is_market_open(now):
            for underlying in underlyings:
                if not cooldown.ready(underlying, now):
                    continue  # still cooling down — stay quiet
                try:
                    state = await asyncio.to_thread(fetch_fn, underlying)
                    if state is None:
                        print(f"[Market Loop] {underlying}: no market state "
                              "this cycle.", flush=True)
                        continue
                    result = await asyncio.to_thread(propose_fn, underlying, state)
                    if result.get("proposed"):
                        cooldown.arm(underlying, now)
                        print(f"[Market Loop] {underlying}: proposal fired — "
                              f"cooling down {cooldown.seconds / 3600:g}h.",
                              flush=True)
                    else:
                        print(f"[Market Loop] {underlying}: no proposal "
                              f"({result.get('reason')}).", flush=True)
                except Exception as e:
                    print(f"[Market Loop] {underlying}: cycle failed ({e}) — "
                          "loop continues.", flush=True)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(run_market_loop())
    except KeyboardInterrupt:
        print("\n[Market Loop] stopped.")

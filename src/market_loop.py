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
    state: dict = {"analysis": analysis, "vix": get_india_vix()}
    try:
        from src.vol_bridge import compute_regime_overrides
        vol_overrides = compute_regime_overrides()
        if vol_overrides:
            state["vol_overrides"] = vol_overrides
    except Exception:
        pass  # bridge unavailable — run with default params
    # Advisory regime radars (Task 1 smart-money/sector veto + Task 2 war
    # playbook). Fail-open: any failure leaves `advisory` unset -> proposer
    # unchanged. Loaded here (once per cycle) so build_proposal never touches
    # the deals ledger itself.
    try:
        from src.analysis import regime_filters
        from src.analysis import smart_money_trend
        deals = smart_money_trend.load_deals_by_ticker()
        state["advisory"] = regime_filters.advise(
            underlying, vix=state["vix"], deals_by_ticker=deals)
    except Exception:
        pass  # radar data unavailable — proposer runs without the advisory
    return state


class CooldownRegistry:
    """Per-key rate limiter: ready() until arm()ed, then quiet for
    `seconds`. Pure and clock-injectable for tests.

    Persistence (ledger Issue 8, 2026-07-09): the armed state used to be
    memory-only, so any mid-session restart (crash, deploy, token refresh)
    forgot proposals fired minutes earlier and immediately re-proposed —
    live positions DOUBLED. seed_from_journal() rebuilds the state from
    the journal itself (entries now carry `created_at`), so the journal is
    the persistence and no new state file exists to corrupt."""

    def __init__(self, seconds: float = COOLDOWN_SECONDS):
        self.seconds = seconds
        self._last: dict = {}

    def ready(self, key: str, now: datetime = None) -> bool:
        now = now or ist_now()
        last = self._last.get(key)
        return last is None or (now - last).total_seconds() >= self.seconds

    def arm(self, key: str, now: datetime = None) -> None:
        self._last[key] = now or ist_now()

    def seed_from_journal(self, underlyings, now: datetime = None,
                          entries: list = None) -> list:
        """Re-arm every underlying whose newest journaled proposal is
        younger than the cooldown window. Any journal line for the
        underlying counts — pending, approved OR rejected all mean a
        proposal FIRED, which is what the cooldown rate-limits.

        Returns the keys that were re-armed. Fail-safe: an unreadable
        journal or unparseable timestamps seed nothing (the loop then
        behaves exactly as before this fix — never worse). Entries
        predating the `created_at` field are skipped by construction."""
        now = now or ist_now()
        if entries is None:
            try:
                from src import journal
                entries = journal.read_all()
            except Exception as e:
                print(f"[Market Loop] cooldown seed skipped — journal "
                      f"unreadable ({e}).", flush=True)
                return []
        wanted = set(underlyings)
        for e in entries:
            ticker = e.get("ticker")
            if ticker not in wanted or not e.get("created_at"):
                continue
            try:
                created = datetime.fromisoformat(e["created_at"])
            except (ValueError, TypeError):
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=IST)
            if (now - created).total_seconds() < self.seconds:
                last = self._last.get(ticker)
                if last is None or created > last:
                    self._last[ticker] = created
        return [u for u in underlyings if not self.ready(u, now)]


async def run_market_loop(underlyings=UNDERLYINGS,
                          interval: float = POLL_INTERVAL_SECONDS,
                          cooldown: CooldownRegistry = None,
                          fetch_fn=fetch_market_state,
                          propose_fn=None,
                          now_fn=ist_now,
                          shadow_fn=None) -> None:
    """The daemon. Every `interval` seconds during market hours: fetch
    each index's state, and when the engine likes the setup, trigger the
    headless proposer (Discord alert + PENDING_APPROVAL journal entry).
    Everything is injectable: the simulator swaps fetch_fn/now_fn, tests
    swap all of it.

    `shadow_fn` (2026-07-17, Shadow Equity Engine): an optional zero-arg
    telemetry cycle run once per open-market poll AFTER the options pass.
    Default None = OFF — only the composition roots (master_scheduler,
    __main__) wire the real equity_shadow_proposer.run_cycle in, so direct
    callers (tests, the simulator) never touch live telemetry. Strictly
    fail-open: a shadow failure is printed and the loop continues."""
    if cooldown is None:
        # Rebuild persisted cooldown state so a mid-session restart cannot
        # double-enter (Issue 8). Injected registries (tests, simulator)
        # are the caller's responsibility and are not re-seeded.
        cooldown = CooldownRegistry()
        try:
            armed = cooldown.seed_from_journal(underlyings, now=now_fn())
            if armed:
                print(f"[Market Loop] cooldown restored from the journal "
                      f"for: {', '.join(armed)}.", flush=True)
        except Exception as e:
            print(f"[Market Loop] cooldown seed failed open ({e}).",
                  flush=True)
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
            if shadow_fn is not None:
                try:
                    res = await asyncio.to_thread(shadow_fn)
                    n_in = len(res.get("entries") or [])
                    n_out = len(res.get("exits") or [])
                    if n_in or n_out:
                        print(f"[Shadow Equity] PAPER_TELEMETRY logged: "
                              f"{n_in} entry(ies), {n_out} exit(s).",
                              flush=True)
                except Exception as e:
                    print(f"[Shadow Equity] cycle failed ({e}) — "
                          "loop continues.", flush=True)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        from src.equity_shadow_proposer import run_cycle as _shadow_cycle
        asyncio.run(run_market_loop(shadow_fn=_shadow_cycle))
    except KeyboardInterrupt:
        print("\n[Market Loop] stopped.")

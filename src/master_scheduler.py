"""
Alpha Trading — Phase 7A: the master execution scheduler
=========================================================

THE one-command entry point for a fully automated live paper-trading
session ("main" in spirit — the name `src/main.py` was already taken by
the Phase 1 alert job that runs on the VM cron at 15:35 IST, so this
lives here):

    python3 -m src.master_scheduler

`run_trading_session()` runs strictly inside NSE market hours (Mon-Fri
09:15-15:30 IST, via the project's own IST clock in market_loop):

  * launched early (the 09:10 cron), it sleeps until the open;
  * launched after the close (a misfire), it exits immediately;
  * at 15:30 it shuts itself down — one cron entry per day, no daemon
    babysitting.

During the window it supervises the two live loops as asyncio tasks:

  ENTRY  market_loop.run_market_loop with the Phase 6H live adapter
         (fetch_fn=live_bridge.fetch_live_market_state): live ticks →
         trend/VIX read → options_proposer.run_headless, which consults
         the vol_bridge regime, REQUESTS MARGIN from the Phase 6G
         capital layer (silent discard on exhaustion/ruin-halt), records
         the paper trade as a PENDING_APPROVAL journal entry, and fires
         the Discord alert. Decisions #11/#53: by default a human's
         Approve/Reject buttons execute; with PAPER_AUTO_APPROVE=1 in the
         environment (the VM's deployed setting since 2026-07-10),
         headless proposals auto-approve through the SAME decide_pending
         path a human tap takes — paper-only either way, and the
         engagement tripwire (src/human_pulse.py) alerts when the owner
         hasn't been seen for days.

  EXIT   live_bridge.run_live_loop: every minute, live ticks are folded
         into candles and every ACTIVE open spread is marked against the
         live spot. Profit-take conditions are SQUARED OFF intraday on
         real chain quotes (decision #69, config-gated) through the
         tracker's one settlement path; pre-expiry conditions stay ONE
         advisory Discord alert each (the tracker settles those at the
         daily close; #41's read-only rule holds everywhere else).

Session bookends go to Discord (fail-safe, muzzled in tests by the
Phase 6J guard): the OPEN card carries the Phase 6G account snapshot
plus the Phase 6I planner's advisory playbook per underlying (the
proposer's live market_view mapped onto the planner's matrix — its
"bullish"/"bearish" IS its strongest read, so it maps to the planner's
strong tier); the CLOSE card carries the end-of-session account state.

GRACEFUL SHUTDOWN (SIGINT/SIGTERM): handlers set an asyncio.Event; the
supervisor cancels both loops and awaits them before returning. State
CANNOT corrupt by design — every DhanHQ httpx client in this codebase
is per-call scoped (async with), every SQLite touch opens, commits
atomically, and closes inside the call that made it; there are no
long-lived handles for a cancellation to strand mid-write.

Everything (clock, loops, notifier, account reader) is injectable —
tests drive whole sessions offline in milliseconds.
"""

import asyncio
import signal
from datetime import datetime, timedelta

from src import live_bridge
from src import market_loop
from src.market_loop import MARKET_CLOSE, MARKET_OPEN, UNDERLYINGS, ist_now

SESSION_POLL_SECONDS = 30    # how often the supervisor rechecks the clock


def _shadow_cycle_fn():
    """The equity-side per-cycle work, resolved lazily and fail-open — a
    broken/absent equity module must never stop the options engine. Two
    legs since decision #83: the block-VWAP telemetry cycle (2026-07-17)
    AND the darling desk's LIVE cycle (exits at live marks, settlements
    into the one firm account, live in-zone entries). This is the
    composition root that turns both ON in prod; run_market_loop's own
    default stays None (tests/simulator unaffected)."""
    legs = []
    try:
        from src.equity_shadow_proposer import run_cycle
        legs.append(("block_shadow", run_cycle))
    except Exception:
        pass
    try:
        from src.config import EQUITY_DESK_ENABLED
        from src.equity_desk import run_darling_live_cycle
        if EQUITY_DESK_ENABLED:
            legs.append(("darling_desk", run_darling_live_cycle))
    except Exception:
        pass
    if not legs:
        return None

    def _combined():
        out = {}
        for name, fn in legs:
            try:
                out[name] = fn()
            except Exception as exc:         # one leg never kills the other
                print(f"  (equity {name} cycle failed: {exc})")
                out[name] = None
        return out
    return _combined


def seconds_until_open(now: datetime) -> float:
    """Seconds from `now` to today's 09:15 IST; 0 when already open or
    past close (the caller decides what past-close means)."""
    open_dt = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                          second=0, microsecond=0)
    return max(0.0, (open_dt - now).total_seconds())


def session_over(now: datetime) -> bool:
    """True once today's session cannot run/continue: past 15:30 IST or
    a weekend day."""
    if now.weekday() >= 5:
        return True
    return now.time() > MARKET_CLOSE


def _unrealized_pnl() -> tuple:
    """(unrealized_rs, marked_count, unmarked_count) for the open book,
    marked on the latest available price via the SHARED snapshot ladder
    (portfolio_report.get_live_marks — engine snapshot first, so this
    costs no Dhan call when the loop just ran). Returns (None, 0, 0) when
    no mark is available — fail-open, the CLOSED card then simply omits
    the unrealized lines rather than guessing a book value."""
    try:
        from src.portfolio_report import get_live_marks
        marks, _source = get_live_marks()
        priced = [m["live_pnl_rs"] for m in marks
                  if m.get("live_pnl_rs") is not None]
        if not priced:
            return None, 0, 0
        return round(sum(priced), 2), len(priced), len(marks) - len(priced)
    except Exception as e:
        print(f"  (unrealized P&L unavailable: {e})")
        return None, 0, 0


def _account_lines() -> list:
    """The Phase 6G account, formatted for a bookend card. Fail-safe:
    an unreadable capital layer reports itself instead of raising.

    Shows realized equity AND (new) the live Unrealized P&L on open
    positions + Net Equity (realized + unrealized) — the true real-time
    book value without waiting for a trade to close."""
    try:
        from src import brain_map, portfolio_manager as pm
        conn = brain_map.connect()
        try:
            s = pm.account_summary(conn)
        finally:
            conn.close()
        lines = [
            f"Equity Rs.{s['equity']:,.2f} "
            f"(realized P&L Rs.{s['realized_pnl']:,.2f})",
        ]
        # New: unrealized P&L + net equity, from the live marks (fail-open —
        # omitted, never guessed, when no snapshot mark exists).
        unreal, marked, unmarked = _unrealized_pnl()
        if unreal is not None:
            # Reconcile coverage against the SAME "active trade(s)" count
            # shown below (open margin locks), so the reader can see the
            # unrealized figure may cover fewer positions than are open —
            # e.g. "priced on 2 of 5" rather than a bare "2 marked" that
            # silently ignores the 3 positions with no live quote this run.
            active = s["open_locks"]
            cover = (f"priced on {marked} of {active} "
                     f"open trade{'s' if active != 1 else ''}")
            lines.append(f"Unrealized P&L Rs.{unreal:+,.2f} ({cover})")
            lines.append(f"**Net Equity Rs.{s['equity'] + unreal:,.2f}** "
                         "(realized + unrealized — true book value)")
        lines.append(
            f"Free cash Rs.{s['available_cash']:,.2f} | "
            f"locked Rs.{s['locked_margin']:,.2f} | "
            f"{s['open_locks']} active trade(s)")
        return lines + (["⛔ RISK-OF-RUIN HALT ACTIVE — all entries blocked"]
                        if s["trading_halted"] else [])
    except Exception as e:
        return [f"(account snapshot unavailable: {e})"]


def _playbook_lines(underlyings, fetch_fn=None) -> list:
    """The Phase 6I planner's advisory read per underlying, from the same
    live state the entry loop trades on. The proposer's market_view is
    its strongest read, so bullish/bearish map to the planner's strong
    tier. Purely informational — the pipeline's own gates still rule."""
    from src.options_proposer import market_view
    from src.trade_planner import map_technical_to_strategy
    fetch_fn = fetch_fn or live_bridge.fetch_live_market_state
    trend_map = {"bullish": "strong_bullish", "bearish": "bearish",
                 "neutral": "neutral"}
    lines = []
    for u in underlyings:
        try:
            state = fetch_fn(u)
            if state is None:
                lines.append(f"{u}: no live read this minute")
                continue
            analysis, vix = state["analysis"], state.get("vix")
            plan = map_technical_to_strategy({
                "spot": analysis["price"], "underlying": u,
                "trend": trend_map.get(market_view(analysis), "neutral"),
                "vix": vix})
            vix_text = f"{vix:.1f}" if vix is not None else "n/a"
            if plan["tradeable"]:
                lines.append(
                    f"{u}: {plan['strategy'].replace('_', ' ')} "
                    f"(view {plan['view']}, VIX {vix_text}, "
                    f"max profit Rs.{plan['max_profit']:,.0f} / "
                    f"loss Rs.{plan['max_loss']:,.0f} per lot)")
            else:
                lines.append(f"{u}: no trade — {plan['rationale']}")
        except Exception as e:
            lines.append(f"{u}: playbook unavailable ({e})")
    return lines


async def _notify(notify_fn, title: str, lines: list) -> None:
    """Session bookends to Discord — fail-safe and never blocking."""
    text = f"**{title}**\n" + "\n".join(lines)
    try:
        if notify_fn is not None:
            result = notify_fn(text)
            if asyncio.iscoroutine(result):
                await result
        else:
            from src.notifier import send_discord_message
            await send_discord_message(text)
    except Exception as e:
        print(f"[Scheduler] (bookend notify failed: {e})", flush=True)


async def run_trading_session(underlyings=UNDERLYINGS, *, now_fn=ist_now,
                              entry_loop=None, exit_loop=None,
                              notify_fn=None, playbook_fn=_playbook_lines,
                              account_fn=_account_lines,
                              stop_event: asyncio.Event = None,
                              poll_seconds: float = SESSION_POLL_SECONDS) -> dict:
    """One full automated session (see module docstring). Returns a
    summary dict: {"status": "completed"|"market_closed"|"stopped",
    "started", "ended"}. Injectable loops/clock/notifier for tests."""
    stop_event = stop_event or asyncio.Event()
    now = now_fn()
    if session_over(now):
        print(f"[Scheduler] {now:%Y-%m-%d %H:%M} IST — market day over; "
              "nothing to run.", flush=True)
        return {"status": "market_closed", "started": None, "ended": None}

    wait = seconds_until_open(now)
    if wait > 0:
        print(f"[Scheduler] waiting {wait / 60:.1f} min for the 09:15 IST "
              "open…", flush=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
            return {"status": "stopped", "started": None, "ended": None}
        except asyncio.TimeoutError:
            pass  # the open arrived

    started = now_fn()
    await _notify(notify_fn, "🟢 Trading session OPEN (paper)",
                  account_fn() + [""] + playbook_fn(underlyings))

    entry_loop = entry_loop or (lambda: market_loop.run_market_loop(
        underlyings=underlyings,
        fetch_fn=live_bridge.fetch_live_market_state,
        shadow_fn=_shadow_cycle_fn()))
    exit_loop = exit_loop or (lambda: live_bridge.run_live_loop(
        underlyings=underlyings))
    tasks = [asyncio.create_task(entry_loop(), name="entry-loop"),
             asyncio.create_task(exit_loop(), name="exit-loop")]
    print(f"[Scheduler] session open — entry + exit loops armed for "
          f"{', '.join(underlyings)}.", flush=True)

    status = "completed"
    try:
        while not session_over(now_fn()):
            if stop_event.is_set():
                status = "stopped"
                break
            if any(t.done() for t in tasks):
                for t in tasks:
                    if t.done() and not t.cancelled() and t.exception():
                        print(f"[Scheduler] {t.get_name()} died: "
                              f"{t.exception()} — shutting down.", flush=True)
                status = "stopped"
                break
            try:
                await asyncio.wait_for(stop_event.wait(),
                                       timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    ended = now_fn()
    await _notify(notify_fn,
                  "🔴 Trading session CLOSED" if status == "completed"
                  else "🟠 Trading session STOPPED (signal)",
                  account_fn())
    print(f"[Scheduler] session {status} at {ended:%H:%M} IST.", flush=True)
    return {"status": status, "started": started.isoformat(),
            "ended": ended.isoformat()}


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """SIGINT/SIGTERM -> set the stop event; the supervisor then cancels
    the loops and lets every in-flight call finish its own atomic
    open-commit-close cycle (nothing long-lived exists to corrupt)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass  # non-unix / nested loop — Ctrl+C still raises


async def _main() -> dict:
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    return await run_trading_session(stop_event=stop_event)


if __name__ == "__main__":
    from src import deploy_log
    # kind="job": this process self-terminates at 15:30, so its sha is a
    # record of what THIS morning's session ran — not what is live tonight.
    deploy_log.record_startup("master_scheduler", kind="job")
    try:
        summary = asyncio.run(_main())
        print(f"[Scheduler] done: {summary}")
    except KeyboardInterrupt:
        print("\n[Scheduler] stopped.")
    except Exception as exc:
        # Set-and-forget doctrine (decision #84): an UNHANDLED crash of
        # the trading session is the ONE event that pages the owner in
        # real time — everything else waits for the EOD/CEO cards.
        print(f"[Scheduler] FATAL: {exc}")
        try:
            import traceback
            from src.notifier import fire_broadcast
            fire_broadcast({
                "event": "system_crash", "ticker": "🚨 SYSTEM",
                "date": ist_now().date().isoformat(),
                "description": ("🚨 MASTER SCHEDULER CRASHED — the trading "
                                "session died on an unhandled exception and "
                                "will not resume until tomorrow's 09:10 "
                                f"cron.\n`{type(exc).__name__}: {exc}`\n"
                                "```\n"
                                + "".join(traceback.format_exc()
                                          .splitlines(True)[-6:])
                                + "\n```")})
        except Exception:
            pass                       # the pager itself must never mask
        raise                          # the crash from the log/exit code

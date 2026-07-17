"""
next_gen_engine/portfolio_risk_manager.py — the DAILY loss circuit breaker
===========================================================================

Blueprint Phase 1 (owner, 2026-07-17). The canonical account layer
(src/portfolio_manager.py) already holds the LIFETIME risk-of-ruin halt
(10% trailing drawdown from peak equity). What it cannot see is a fast
bleed WITHIN a day that hasn't dented peak equity yet — five correlated
stop-outs before lunch. This module adds that second, tighter fuse:

    realized loss TODAY >= MAX_DAILY_LOSS_PCT of session-open equity
        -> halt NEW entries for the rest of the IST day.

Design contract (matching the house rules):
  * PURE + injectable: the breaker never reads the journal or DB itself —
    callers hand it `session_open_equity` and today's resolved trade P&Ls.
    That keeps it testable offline and lets the simulator replay it.
  * Halts ENTRIES ONLY. Exits/tracking must never be blocked by risk
    plumbing (a halt that traps open positions is worse than the loss).
  * Resets by construction at the IST day boundary — "today" is an input,
    so there is no state file to corrupt and nothing to un-stick.
  * INTEGRATION TARGET: a check inside portfolio_manager.gate_headless_entry
    beside trading_halted(); NOT a replacement for it.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

MAX_DAILY_LOSS_PCT = 3.0   # halt entries after -3% realized in one day


def ist_today() -> str:
    return datetime.now(IST).date().isoformat()


def realized_pnl_today(resolved_entries: list, today: str = None) -> float:
    """Sum of net P&L across trades RESOLVED today (IST). Rows without a
    resolution date or P&L are skipped — unknown is not a loss. Accepts
    journal-shaped dicts: uses `resolved_at`/`closed_at` (ISO, date prefix
    match) and `pnl_net`/`pnl` for the amount."""
    today = today or ist_today()
    total = 0.0
    for e in resolved_entries or []:
        stamp = e.get("resolved_at") or e.get("closed_at") or ""
        if not str(stamp).startswith(today):
            continue
        pnl = e.get("pnl_net", e.get("pnl"))
        if isinstance(pnl, (int, float)):
            total += float(pnl)
    return round(total, 2)


def check_daily_breaker(session_open_equity: float,
                        pnl_today: float,
                        max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT) -> dict:
    """The verdict. `halted=True` means NO NEW ENTRIES today.

    Fail-safe posture: an unusable equity figure (None/0/negative) returns
    halted=False with an explicit `error` — the daily breaker refusing to
    guess must not freeze the engine, because the canonical lifetime
    drawdown halt is still armed underneath it."""
    if not session_open_equity or session_open_equity <= 0:
        return {"halted": False, "daily_loss_pct": None,
                "error": "no usable session-open equity — breaker abstains "
                         "(lifetime drawdown halt still active)"}
    loss_pct = max(0.0, -pnl_today) / session_open_equity * 100
    # compare UNROUNDED (a 2.9999% loss must not round-trip into a trip),
    # report rounded
    halted = loss_pct >= max_daily_loss_pct
    loss_pct = round(loss_pct, 3)
    return {
        "halted": halted,
        "daily_loss_pct": loss_pct,
        "limit_pct": max_daily_loss_pct,
        "pnl_today": round(pnl_today, 2),
        "session_open_equity": round(session_open_equity, 2),
        "reason": (f"daily circuit breaker TRIPPED: realized "
                   f"{-pnl_today:,.0f} = {loss_pct}% of session-open equity "
                   f"(limit {max_daily_loss_pct}%) — entries halted until "
                   f"tomorrow" if halted else "within daily loss budget"),
    }


def gate_entry(session_open_equity: float, resolved_entries: list,
               today: str = None,
               max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT) -> dict:
    """One-call convenience for the future gate_headless_entry hook:
    computes today's realized P&L from journal-shaped rows and returns the
    breaker verdict."""
    pnl = realized_pnl_today(resolved_entries, today=today)
    return check_daily_breaker(session_open_equity, pnl,
                               max_daily_loss_pct=max_daily_loss_pct)

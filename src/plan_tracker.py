"""
Alpha Trading — Phase 4C: the plan tracker
==========================================

Watches every journaled 4B plan (approved OR rejected) and resolves it
against what prices actually did, day by day:

  stop hit    -> the plan's stop-loss price traded; approved positions are
                 closed on paper at the stop, exactly as the approved plan
                 said they would be (a bracket order, in effect)
  target hit  -> the take-profit price traded; approved positions are closed
                 at the target
  time stop   -> neither hit within PLAN_MAX_DAYS; the plan is closed out
                 at the latest close so nothing dangles forever

Rejected plans resolve by the same rules but hypothetically (no portfolio
touch) — that's how we learn whether a skip was smart.

Outcomes land in the same journal `outcome` field review.py uses, with real
plan metrics: exit price/date, %, R-multiple, days in trade, rupee P&L.
Entries WITHOUT a stop-carrying plan (pre-4B entries, sell/exit decisions)
are not touched here — src/review.py keeps scoring those as before.

Runs automatically at the start of every trade session, or manually:

    python3 -m src.plan_tracker

Same-day ambiguity is resolved pessimistically: if a day's range covers both
the stop and the target, we assume the stop hit first. The entry day itself
is never scanned (the trade happened near that day's close; its intraday low
usually predates the entry).
"""

from datetime import date

from src import journal
from src import portfolio as pf
from src.config import PLAN_MAX_DAYS
from src.dhan_client import get_ohlc_since
from src.notifier import send_digest
from src.review import MOVE_THRESHOLD


def _daily_bars(ticker: str, start_iso: str):
    """[(date_iso, low, high, close), ...] since start_iso.

    Uses Dhan's real daily OHLC so stop/target hits resolve on the true
    intraday low/high of each session (migrated off yfinance 2026-07-06) —
    NOT a naive last-price check. Dhan returns clean trading-day bars only."""
    return [
        (bar["date"], bar["low"], bar["high"], bar["close"])
        for bar in get_ohlc_since(ticker, start_iso)
    ]


def _trackable(entry: dict) -> bool:
    plan = entry.get("plan")
    return entry["outcome"] is None and bool(plan and plan.get("stop_loss"))


def _resolve(entry: dict, bars: list):
    """(resolution, exit_price, exit_date) once the plan has resolved,
    else None while it is still live."""
    stop = entry["plan"]["stop_loss"]["price"]
    target = entry["plan"]["target"]["price"]
    for day, low, high, _close in bars:
        if day <= entry["date"]:
            continue  # never scan the entry day itself
        if low <= stop:
            return "stop_hit", stop, day
        if high >= target:
            return "target_hit", target, day
    age = (date.today() - date.fromisoformat(entry["date"])).days
    if bars and age >= PLAN_MAX_DAYS:
        day, _low, _high, close = bars[-1]
        return "time_stop", round(float(close), 2), day
    return None


def _verdict(entry: dict, resolution: str, pct: float) -> str:
    approved = entry["decision"] == "approved"
    if resolution == "target_hit":
        return ("WIN — target hit" if approved
                else "MISSED GAIN — it hit the target without you")
    if resolution == "stop_hit":
        return ("LOSS — stop hit" if approved
                else "GOOD SKIP — it would have hit the stop")
    # time stop: score the drift, using review.py's same flat threshold
    if pct >= MOVE_THRESHOLD:
        return ("WIN — time stop, closed ahead" if approved
                else "MISSED GAIN — it drifted up without you")
    if pct <= -MOVE_THRESHOLD:
        return ("LOSS — time stop, closed behind" if approved
                else "GOOD SKIP — it drifted down")
    return "flat (time stop, went nowhere)"


def _close_paper_position(entry: dict, exit_price: float) -> bool:
    """Close the tracked holding at the plan's exit price. Returns False if
    the position was already closed some other way (e.g. a Death Cross sell
    the user approved in a session) — the outcome still gets recorded."""
    book = pf.load()
    if entry["ticker"] not in book["holdings"]:
        return False
    pf.sell(book, entry["ticker"], exit_price)
    pf.save(book)
    return True


def _outcome_line(entry: dict) -> str:
    o = entry["outcome"]
    label = {"stop_hit": "STOPPED OUT", "target_hit": "TARGET HIT",
             "time_stop": "TIME STOP"}[o["resolution"]]
    if entry["decision"] == "approved":
        head = (f"{label}: {entry['ticker']} bought {entry['date']} at "
                f"Rs.{entry['price']:,.2f} exited at Rs.{o['price']:,.2f} "
                f"on {o['exit_date']} — Rs.{o['pnl_rs']:+,.2f} "
                f"({o['pct']:+.1f}%, {o['r_multiple']:+.1f}R), "
                f"{o['days_in_trade']} day(s) in trade.")
        if not o["position_closed"]:
            head += " (Position was already closed earlier — outcome recorded for scoring only.)"
    else:
        head = (f"{label} (you skipped this one): {entry['ticker']} plan from "
                f"{entry['date']} would have exited at Rs.{o['price']:,.2f} "
                f"on {o['exit_date']} ({o['pct']:+.1f}%, {o['r_multiple']:+.1f}R).")
    return (f"{head}\n   Verdict: {o['verdict']}\n"
            f"   Engine's reason at the time: {entry['signal']}\n"
            f"   Your reason at the time: {entry['why']}")


def run_tracker(email: bool = True) -> int:
    """Sweep all open plans; returns how many resolved this run."""
    entries = journal.read_all()
    open_plans = [e for e in entries if _trackable(e)]
    if not open_plans:
        print("Plan tracker: no open plans to check.")
        return 0

    resolved_lines, resolved = [], 0
    for entry in open_plans:
        bars = _daily_bars(entry["ticker"], entry["date"])
        if not bars:
            print(f"Plan tracker: no price data for {entry['ticker']} — will retry next run.")
            continue
        hit = _resolve(entry, bars)
        stop_price = entry["plan"]["stop_loss"]["price"]
        if hit is None:
            _day, low, high, close = bars[-1]
            print(f"Plan tracker: {entry['ticker']} still live "
                  f"(now Rs.{close:,.2f}; stop Rs.{stop_price:,.2f}, "
                  f"target Rs.{entry['plan']['target']['price']:,.2f}).")
            continue

        resolution, exit_price, exit_day = hit
        pct = (exit_price - entry["price"]) / entry["price"] * 100
        risk_per_share = entry["price"] - stop_price
        approved = entry["decision"] == "approved"
        closed = _close_paper_position(entry, exit_price) if approved else False
        entry["outcome"] = {
            "checked": date.today().isoformat(),
            "resolution": resolution,
            "price": exit_price,
            "exit_date": exit_day,
            "pct": round(pct, 2),
            "r_multiple": round((exit_price - entry["price"]) / risk_per_share, 2)
                          if risk_per_share > 0 else None,
            "days_in_trade": (date.fromisoformat(exit_day)
                              - date.fromisoformat(entry["date"])).days,
            "pnl_rs": round(entry["shares"] * (exit_price - entry["price"]), 2),
            "hypothetical": not approved,
            "position_closed": closed,
            "verdict": _verdict(entry, resolution, pct),
        }
        resolved += 1
        resolved_lines.append(_outcome_line(entry))
        print(f"Plan tracker: resolved {entry['ticker']} — {entry['outcome']['verdict']}")

    if resolved:
        journal.rewrite_all(entries)
        if email:
            send_digest("Paper Trading: plans resolved", resolved_lines)
    return resolved


if __name__ == "__main__":
    run_tracker()

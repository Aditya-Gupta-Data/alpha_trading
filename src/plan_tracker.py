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

Phase 6 core loop: the moment a plan resolves, the tracker also (a) asks
the post-mortem analyst (src/analyst.py, Gemini) to compare the original
plan with what actually happened, and (b) writes the outcome + its
signal/pattern events + that post-mortem into the Brain Map
(data/brain_map.db), keyed by the entry's journal short_id. Both steps
are strictly fail-safe: no Gemini key, no network, or any Brain Map error
just prints a note — the journal outcome above is already saved and is
never blocked by the memory write.

Runs automatically at the start of every trade session, or manually:

    python3 -m src.plan_tracker

Same-day ambiguity is resolved pessimistically: if a day's range covers both
the stop and the target, we assume the stop hit first. The entry day itself
is never scanned (the trade happened near that day's close; its intraday low
usually predates the entry).
"""

from datetime import date

from src import analyst
from src import brain_map
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


# Patchable seam for tests (point it at a temp DB) — production always
# opens the real data/brain_map.db.
_brain_connect = brain_map.connect


def _post_mortem_payloads(entry: dict) -> tuple:
    """(initial_plan, actual_execution) for the analyst — captured at the
    exact moment of resolution. initial_plan is the forecasting thesis we
    journaled at entry (signal, user reasoning, pattern tags, the full 4B
    plan JSON); actual_execution is what the market really did (the
    trigger that fired plus the realized metrics)."""
    o = entry["outcome"]
    initial_plan = {
        "date": entry["date"],
        "ticker": entry["ticker"],
        "action": entry["action"],
        "thesis_signal": entry.get("signal"),
        "user_reasoning": entry.get("why"),
        "pattern_tags": entry.get("pattern_tags") or [],
        "plan": entry.get("plan"),
        "entry_price": entry["price"],
        "shares": entry["shares"],
    }
    actual_execution = {
        "trigger": o["resolution"],  # stop_hit / target_hit / time_stop
        "entry_price": entry["price"],
        "exit_price": o["price"],
        "exit_date": o["exit_date"],
        "pct_change": o["pct"],
        "r_multiple": o["r_multiple"],
        "days_in_trade": o["days_in_trade"],
        "pnl_rs": o["pnl_rs"],
        "verdict": o["verdict"],
        "hypothetical": o.get("hypothetical", False),
    }
    return initial_plan, actual_execution


def record_post_mortem(entry: dict, brain) -> None:
    """Phase 6 core loop, per resolved entry: generate the analyst's
    post-mortem (None is fine — the trade is recorded either way) and
    write outcome + events + post-mortem to the Brain Map, keyed by the
    entry's short_id via brain_map.journal_ref_for."""
    initial_plan, actual_execution = _post_mortem_payloads(entry)
    post_mortem = analyst.generate_post_mortem(initial_plan, actual_execution)
    brain_map.record_resolved_entry(brain, entry, post_mortem=post_mortem)


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

    # One Brain Map connection for the whole sweep. Optional by design:
    # if it can't open, plans still resolve and journal normally.
    try:
        brain = _brain_connect()
    except Exception as e:
        print(f"Plan tracker: Brain Map unavailable ({e}) — resolving without memory writes.")
        brain = None

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

        # Phase 6 core loop: post-mortem + Brain Map write. Fail-safe —
        # the journal outcome above is already set and never blocked.
        if brain is not None:
            try:
                record_post_mortem(entry, brain)
                print(f"Plan tracker: {entry['ticker']} outcome recorded in the Brain Map.")
            except Exception as e:
                print(f"Plan tracker: Brain Map write failed for {entry['ticker']} "
                      f"({e}) — outcome still journaled.")

    if brain is not None:
        brain.close()
    if resolved:
        journal.rewrite_all(entries)
        if email:
            send_digest("Paper Trading: plans resolved", resolved_lines)
    return resolved


if __name__ == "__main__":
    run_tracker()

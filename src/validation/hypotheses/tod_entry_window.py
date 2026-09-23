"""Hypothesis A — TIME-OF-DAY ENTRY WINDOW (decision #109).

"Entering trades strictly between 10:00 and 14:30 IST yields a higher
per-trade Sharpe than all-day entries." Cohorts from the options journal's
own resolved, approved spreads: `in_window` (entry stamp inside the window)
vs `all_day` (every resolved entry — the hypothesis compares against the
unfiltered book, so the window cohort is a subset). Metric: per-trade
Sharpe of realized R (`outcome.r_multiple`). Not a live filter.
"""
from __future__ import annotations

from datetime import date, datetime

from ._metrics import summarize

NAME = "tod_entry_window"
DESCRIPTION = ("ToD: options entries 10:00-14:30 IST have a higher per-trade Sharpe "
               "than all-day entries (options journal, resolved approved spreads)")
DEFINITION = {"hypothesis": NAME, "window_start": "10:00", "window_end": "14:30",
              "tz": "Asia/Kolkata", "metric": "per_trade_sharpe",
              "cohorts": ["in_window", "all_day"], "source": "journal.spread_outcomes"}
SEAMS = ("entries",)
WINDOW = ("10:00", "14:30")


def entry_time(entry: dict) -> str | None:
    """'HH:MM' of the journal entry's creation stamp, or None."""
    raw = entry.get("created_at") or entry.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return None


def in_window(hhmm: str | None, window=WINDOW) -> bool:
    return bool(hhmm) and window[0] <= hhmm <= window[1]


def cohorts(entries: list) -> dict:
    """{'in_window': [R...], 'all_day': [R...], 'unstamped': n}."""
    inw, allday, unstamped = [], [], 0
    for e in entries or []:
        o = e.get("outcome") or {}
        if e.get("decision") != "approved" or not e.get("spread") or o.get("r_multiple") is None:
            continue
        r = float(o["r_multiple"])
        allday.append(r)
        t = entry_time(e)
        if t is None:
            unstamped += 1
        elif in_window(t):
            inw.append(r)
    return {"in_window": inw, "all_day": allday, "unstamped": unstamped}


def evidence(entries: list = None, today: date = None, min_n: int = 20) -> dict:
    if entries is None:
        from src import journal
        entries = journal.read_all()
    c = cohorts(entries)
    a, b = summarize(c["in_window"]), summarize(c["all_day"])
    if a["n"] < min_n or b["n"] < min_n:
        verdict = "insufficient_n"
    elif a["sharpe"] is None or b["sharpe"] is None:
        verdict = "unmeasurable"
    else:
        verdict = "supports" if a["sharpe"] > b["sharpe"] else "contradicts"
    return {"verdict": verdict, "min_n": min_n, "in_window": a, "all_day": b,
            "unstamped": c["unstamped"],
            "headline": (f"in-window n={a['n']} Sharpe {a['sharpe']} vs all-day n={b['n']} "
                         f"Sharpe {b['sharpe']}")}

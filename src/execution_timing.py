"""
src/execution_timing.py — the T+1-open execution-timing contract (§5.5)
=======================================================================

EOD honesty (HOLY_GRAIL_PLAN §2/§5.5): any signal sourced after the close
of day T — deals (~19:00 IST), FII/DII flows, the news/macro snapshots,
the earnings calendar — did not exist during T's session. A backtest that
lets such a signal "enter" at T's close is trading on information from
the future of its own fill. The contract:

    An EOD-sourced signal dated T executes at T+1's OPEN, at the gapped
    price — and the trade's receipt records how stale the signal was at
    execution (`signal_age_hours`).

This module is the single home of that arithmetic: which calendar moment
each EOD artifact becomes available, what "the next trading day" means
(the bars themselves define trading days — holidays need no calendar),
and the honest refusal when open prices aren't available (a missing open
is LABELLED and skipped, never interpolated — #55's no-deception rule).

Consumed by the simulator (`run_simulation(eod_signal_days=...)`) today;
built for the Phase-5 walk-forward miners tomorrow.
"""

from datetime import date, datetime, timedelta

# When each EOD artifact is actually available (IST, conservative — the
# moment the VM job has WRITTEN it, not when NSE begins publishing).
# Matches the cron block in scripts/setup_cron.sh.
EOD_PUBLICATION_IST = {
    "deals": "19:30",       # NSE publishes ~19:00; our pull lands 19:30
    "earnings": "19:20",
    "flows": "19:35",
    "news": "19:45",        # daily_archiver snapshot
    "macro": "19:45",
    "affinity": "20:00",    # sleep-phase Task F folds after 20:00
}

# NSE cash open. Execution at "T+1 open" means this moment on the next
# trading day.
MARKET_OPEN_IST = "09:15"


def publication_moment(layer: str, signal_day: str) -> datetime | None:
    """The naive-IST datetime at which `layer`'s artifact for `signal_day`
    became available, or None for an unknown layer / unparseable day."""
    hhmm = EOD_PUBLICATION_IST.get(layer)
    if not hhmm:
        return None
    try:
        d = date.fromisoformat(str(signal_day))
        h, m = (int(x) for x in hhmm.split(":"))
        return datetime(d.year, d.month, d.day, h, m)
    except (ValueError, TypeError):
        return None


def signal_age_hours(layer: str, signal_day: str,
                     exec_day: str, exec_hhmm: str = MARKET_OPEN_IST):
    """Hours between `layer`'s publication for `signal_day` and execution
    at `exec_day` `exec_hhmm` — the receipt's staleness figure. A Friday
    deals print executed Monday 09:15 honestly reads ~61.75h, not 13.75h.
    Returns None (never a guess) when either moment can't be derived or
    execution would precede publication."""
    published = publication_moment(layer, signal_day)
    if published is None:
        return None
    try:
        d = date.fromisoformat(str(exec_day))
        h, m = (int(x) for x in exec_hhmm.split(":"))
        executed = datetime(d.year, d.month, d.day, h, m)
    except (ValueError, TypeError):
        return None
    if executed < published:
        return None
    return round((executed - published).total_seconds() / 3600.0, 2)


def next_trading_bar(bars: list, after_day: str):
    """(index, bar) of the first bar dated STRICTLY after `after_day`, or
    None when the range ends first. The bar list itself is the trading
    calendar — weekends and holidays are simply days with no bar, so a
    Friday signal's next bar is Monday's without any holiday table."""
    for i, bar in enumerate(bars or []):
        if bar and str(bar[0]) > str(after_day):
            return i, bar
    return None


def t1_open_entry(bars: list, signal_day: str,
                  opens_by_date: dict) -> dict | None:
    """Where and at what price an EOD signal from `signal_day` executes:
    {"exec_day", "open", "bar_index", "basis": "t1_open"}. Bars stay the
    project's (date, low, high, close) tuples; opens ride their own
    {date: open} map. Returns None — with the reason printed, never an
    interpolated price — when the range ends before a next bar exists or
    no true open is known for that day (#55: label, never interpolate)."""
    nxt = next_trading_bar(bars, signal_day)
    if nxt is None:
        return None
    i, bar = nxt
    exec_day = str(bar[0])
    open_px = (opens_by_date or {}).get(exec_day)
    if open_px is None:
        print(f"  (execution timing: no open price for {exec_day} — "
              "T+1 entry refused, never interpolated)")
        return None
    return {"exec_day": exec_day, "open": float(open_px), "bar_index": i,
            "basis": "t1_open"}

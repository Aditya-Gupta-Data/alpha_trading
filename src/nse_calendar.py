"""
src/nse_calendar.py — the NSE trading-holiday calendar (ledger Issue 41)
========================================================================
THE one place that knows which weekdays the exchange is closed. Before
2026-10-04 nothing did: `market_loop.is_market_open` was "Mon-Fri,
09:15-15:30", and on Fri 2026-10-02 (Mahatma Gandhi Jayanti) the engine ran
a full session on frozen data and filled a phantom trade in three accounts.

Pure data + date arithmetic: no network, no file, no state. Everything that
asks "is the market open / is today a session" reaches this module through
`market_loop.is_market_open` (the entry loop, the live bridge, the live
pricer, the 15-minute tracker, the report cards) or
`master_scheduler.session_over`, or calls `is_trading_day` directly (the
chain archiver, the darlings tap, the ops heartbeats, the human-pulse count).
`plan_tracker` asks it which session an expiry settles on and whether today
is the last session before an expiry (`expiry_session`,
`in_forced_exit_window` — audit F22/F23).

THE LISTS ARE HAND-MAINTAINED, ON PURPOSE. A holiday date guessed from a
pattern is the same bug as a guessed security id (ledger Issues 14/15):
lunar-calendar festivals move every year and the exchange adds ad-hoc
closures (elections). Each year is copied from NSE's own annual circular,
which NSE publishes each December for the following year.

  2026  VERIFIED on 2026-10-04 against NSE circular NSE/CMTR/71775 as
        reproduced by two independent secondary sources that agree
        date-for-date (smallcase.com/learn/nse-holidays-2026, which names
        the circular, and the listings a web search returned the same
        day). The circular PDF on nsearchives.nseindia.com could not
        be opened from here (the host timed out), so this is NOT a
        first-hand read of the circular. 2026-01-15 (Maharashtra municipal
        elections) was added by a later NSE notice.
  2027  PROVISIONAL. NSE has not published its 2027 circular yet (due
        December 2026). These are third-party projections
        (calendarlabs.com, financecalendar.com, read 2026-10-04). The two
        sources DISAGREE on Muharram (15 vs 16 June) and only one lists
        Mahavir Jayanti; every date either source lists is treated as
        CLOSED, because sleeping through a real session costs one day of
        paper data while trading a closed one books phantom trades.
        `calendar_warning` nags from 15 December until the year is
        replaced with the circular's list.

A year with no list at all is NOT guessed: only weekends are closed, and
`calendar_warning` says so in words the nightly ops card carries.

Muhurat trading (the Diwali evening session) and other special sessions are
not modelled: the engine trades Mon-Fri 09:15-15:30 only.
"""
from datetime import date, datetime, timedelta

# date -> occasion. Weekday closures only; a holiday that falls on a weekend
# is already closed by the weekday rule and is left out.
_VERIFIED = {
    2026: {
        date(2026, 1, 15): "Municipal Corporation Election in Maharashtra",
        date(2026, 1, 26): "Republic Day",
        date(2026, 3, 3): "Holi",
        date(2026, 3, 26): "Shri Ram Navami",
        date(2026, 3, 31): "Shri Mahavir Jayanti",
        date(2026, 4, 3): "Good Friday",
        date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
        date(2026, 5, 1): "Maharashtra Day",
        date(2026, 5, 28): "Bakri Id",
        date(2026, 6, 26): "Muharram",
        date(2026, 9, 14): "Ganesh Chaturthi",
        date(2026, 10, 2): "Mahatma Gandhi Jayanti",
        date(2026, 10, 20): "Dussehra",
        date(2026, 11, 10): "Diwali-Balipratipada",
        date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
        date(2026, 12, 25): "Christmas",
    },
}

# NOT from an NSE circular (see the module docstring). Treated as closed.
_PROVISIONAL = {
    2027: {
        date(2027, 1, 26): "Republic Day",
        date(2027, 3, 10): "Id-Ul-Fitr (Ramzan Id)",
        date(2027, 3, 22): "Holi",
        date(2027, 3, 26): "Good Friday",
        date(2027, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
        date(2027, 4, 15): "Shri Ram Navami",
        date(2027, 4, 19): "Shri Mahavir Jayanti",
        date(2027, 5, 17): "Bakri Id",
        date(2027, 6, 15): "Muharram",
        date(2027, 6, 16): "Muharram (the sources disagree: 15 or 16 June)",
        date(2027, 10, 29): "Diwali Laxmi Pujan",
    },
}

NAG_FROM = (12, 15)      # month, day: NSE's next-year circular is out by then


def _day(d) -> date:
    return d.date() if isinstance(d, datetime) else d


def holiday_name(d) -> str | None:
    """The occasion when `d` is a listed weekday holiday (a provisional one
    is labelled so), else None."""
    d = _day(d)
    name = _VERIFIED.get(d.year, {}).get(d)
    if name is not None:
        return name
    name = _PROVISIONAL.get(d.year, {}).get(d)
    return f"{name} (provisional list)" if name is not None else None


def is_trading_holiday(d) -> bool:
    """True on a listed weekday holiday. Weekends are NOT holidays here —
    they are closed by the weekday rule in `is_trading_day`."""
    return holiday_name(d) is not None


def is_trading_day(d) -> bool:
    """Mon-Fri and not a listed NSE holiday."""
    d = _day(d)
    return d.weekday() < 5 and not is_trading_holiday(d)


def previous_trading_day(d) -> date:
    """The last trading day strictly before `d`."""
    d = _day(d) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d) -> date:
    """The first trading day strictly after `d` (plan_tracker's forced
    pre-expiry window asks it whether today is the last session before an
    expiry — audit F23)."""
    d = _day(d) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def year_status(year: int) -> str:
    """'verified' | 'provisional' | 'missing'."""
    if year in _VERIFIED:
        return "verified"
    return "provisional" if year in _PROVISIONAL else "missing"


def calendar_warning(today=None) -> str | None:
    """One plain line when the calendar cannot be trusted for today's year,
    or — from 15 December — for next year; None when both are verified.
    Worded for the ops sweep ("UNAVAILABLE"/"PROVISIONAL" are its own
    problem vocabulary)."""
    today = _day(today or datetime.now())
    years = [today.year]
    if (today.month, today.day) >= NAG_FROM:
        years.append(today.year + 1)
    for year in years:
        status = year_status(year)
        if status == "missing":
            return (f"NSE holiday list for {year} is UNAVAILABLE in src/nse_calendar.py — only "
                    f"weekends are treated as closed, so the engine WILL trade on {year}'s "
                    "exchange holidays. Add the list from NSE's annual circular.")
        if status == "provisional":
            return (f"NSE holiday list for {year} is PROVISIONAL (third-party dates, not NSE's "
                    "circular) — replace it in src/nse_calendar.py with the circular's list.")
    return None

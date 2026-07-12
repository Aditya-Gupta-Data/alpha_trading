"""
src/cycles.py — the calendar/event cycle vocabulary (pure date math)
====================================================================

Owner concern #4 (2026-07-11): confidence should be able to notice when a
CYCLE is also aligned — but per the composition law (#63) and the Way-A
decision, a cycle can never hand-add points. This module only DEFINES the
cycles as pure, deterministic functions of a date; two consumers earn
meaning from them:

  * the miners: `cooccurrence_miner.context_tags` emits these as
    `ctx:season:*` daily_context tags, so the discovery brain measures
    whether any cycle actually co-occurs with / precedes wins;
  * the forecast: a cycle driver whose points come ONLY from
    brain_weights.json `cycle_points` — written by the tuner from
    resolved outcomes, floor-gated and capped, defaulting to ZERO.
    An unlearned cycle contributes nothing (silent until earned).

Everything here is computable as-of any historical date with no stored
state (pure calendar math), so the tuner can honestly recompute "was this
cycle active at entry?" for every past trade — no look-ahead possible.

EXPIRY ERA RULE: NSE index derivatives expired on THURSDAYS until the
2025-09-01 exchange migration moved them to TUESDAYS (SEBI's two-day
split; BSE holds Thursday). `monthly_expiry` respects the era of the date
it is asked about, so backfilled tags stay true to their own time.
Holiday-shifted expiries (an expiry falling on a trading holiday moves a
day earlier) are NOT modeled — a ±1-day edge case a support floor absorbs.

Deliberately NOT modeled (overfit bait on thin data): lunar/festival
dates, weekday-of-month interactions, half-yearly cycles. The miners can
discover any real one from the month/expiry tags without us guessing.
"""

import calendar
from datetime import date, timedelta

# NSE index expiry weekday: Thursday (weekday 3) before the 2025-09-01
# migration, Tuesday (weekday 1) from it onward.
EXPIRY_ERA_SWITCH = date(2025, 9, 1)
_THURSDAY, _TUESDAY = 3, 1

# How many calendar days before the monthly expiry count as "expiry week".
EXPIRY_WEEK_SPAN = 4

_MONTH_ABBR = ("", "jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec")


def expiry_weekday_for(d: date) -> int:
    """The monthly-expiry weekday in force on date `d` (era rule above)."""
    return _TUESDAY if d >= EXPIRY_ERA_SWITCH else _THURSDAY


def monthly_expiry(year: int, month: int) -> date:
    """The month's NSE index monthly expiry: its LAST expiry-weekday.
    The era is judged from the month itself (its first day)."""
    weekday = expiry_weekday_for(date(year, month, 1))
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def days_to_monthly_expiry(d: date) -> int:
    """Calendar days from `d` to the CURRENT cycle's monthly expiry —
    this month's if it hasn't passed, else next month's."""
    exp = monthly_expiry(d.year, d.month)
    if d > exp:
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        exp = monthly_expiry(y, m)
    return (exp - d).days


def is_expiry_day(d: date) -> bool:
    return d == monthly_expiry(d.year, d.month)


def is_expiry_week(d: date) -> bool:
    """Within EXPIRY_WEEK_SPAN calendar days of (and including) the
    monthly expiry — the gamma/theta endgame window."""
    exp = monthly_expiry(d.year, d.month)
    return 0 <= (exp - d).days <= EXPIRY_WEEK_SPAN


def is_quarter_end_week(d: date) -> bool:
    """The last 5 calendar days of a fiscal quarter month (Mar/Jun/Sep/
    Dec) — window-dressing / rebalancing season."""
    if d.month not in (3, 6, 9, 12):
        return False
    return d.day > calendar.monthrange(d.year, d.month)[1] - 5


def cycle_tags(d: date) -> set:
    """The seasonal-cycle tags active on `d`. Pure; namespaced `season:`
    so consumers prefix honestly (miners emit them as ctx:season:*)."""
    if d is None:
        return set()
    tags = {f"season:month_{_MONTH_ABBR[d.month]}"}
    if is_expiry_week(d):
        tags.add("season:expiry_week")
    if is_expiry_day(d):
        tags.add("season:expiry_day")
    if is_quarter_end_week(d):
        tags.add("season:quarter_end")
    return tags


def cycle_tags_for_iso(day: str) -> set:
    """cycle_tags for an ISO 'YYYY-MM-DD' string; empty set on garbage
    (fail-open — a bad date must never sink a caller)."""
    try:
        return cycle_tags(date.fromisoformat(str(day)[:10]))
    except (TypeError, ValueError):
        return set()


def event_tags(ticker: str, d: date, calendar_data: dict = None) -> set:
    """Per-ticker EVENT-cycle tags (results proximity) from the earnings
    calendar. LIVE-FORWARD ONLY: the calendar is whole-overwritten daily,
    so historical as-of recomputation is impossible — these tags are for
    live capture surfaces (evidence snapshots already store
    days_to_results per entry; miners consume event cycles from THERE,
    not from backfill). Never raises."""
    try:
        from src.ingestion.earnings_calendar import days_to_results
        dtr = days_to_results(ticker, d, calendar_data)
    except Exception:
        return set()
    if dtr is None:
        return set()
    if 0 <= dtr <= 3:
        return {"event:results_in_3d"}
    if 4 <= dtr <= 10:
        return {"event:results_in_10d"}
    return set()


if __name__ == "__main__":
    today = date.today()
    print(f"{today}: expiry {monthly_expiry(today.year, today.month)} "
          f"({days_to_monthly_expiry(today)}d away)")
    print(f"tags: {sorted(cycle_tags(today))}")

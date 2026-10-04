"""
src/nse_calendar.py — the NSE trading-holiday calendar (ledger Issue 41).

On Fri 2026-10-02 (Mahatma Gandhi Jayanti) the engine ran a full session on
frozen data and filled a phantom trade. These tests pin the calendar and
every place that must now sleep on an exchange holiday. Hermetic: injected
clocks, no network.
"""
import asyncio
import os
from datetime import date, datetime, timedelta, timezone

from src import live_bridge, market_loop, master_scheduler as ms, nse_calendar as cal, ops_monitor
from src.execution import live_pricer
from src.ingestion import chain_archiver, intraday_tracker

IST = timezone(timedelta(hours=5, minutes=30))
GANDHI_JAYANTI = datetime(2026, 10, 2, 11, 0, tzinfo=IST)          # a Friday
NEXT_MONDAY = datetime(2026, 10, 5, 11, 0, tzinfo=IST)


def test_the_2026_list_is_exactly_the_circulars_weekday_closures():
    assert sorted(cal._VERIFIED[2026]) == [
        date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
        date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28), date(2026, 6, 26),
        date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24),
        date(2026, 12, 25)]
    for table in (cal._VERIFIED, cal._PROVISIONAL):
        for year, days in table.items():
            assert all(d.year == year and d.weekday() < 5 for d in days)   # weekday closures only


def test_trading_day_is_a_weekday_that_is_not_a_listed_holiday():
    assert cal.holiday_name(date(2026, 10, 2)) == "Mahatma Gandhi Jayanti"
    assert not cal.is_trading_day(date(2026, 10, 2)) and not cal.is_trading_day(GANDHI_JAYANTI)
    assert cal.is_trading_day(date(2026, 10, 5)) and cal.holiday_name(date(2026, 10, 5)) is None
    assert not cal.is_trading_day(date(2026, 10, 3)) and not cal.is_trading_holiday(date(2026, 10, 3))  # Saturday
    assert cal.previous_trading_day(date(2026, 10, 5)) == date(2026, 10, 1)        # over the holiday + weekend
    assert cal.holiday_name(date(2027, 3, 22)) == "Holi (provisional list)"
    assert not cal.is_trading_day(date(2027, 6, 15)) and not cal.is_trading_day(date(2027, 6, 16))


def test_every_market_open_check_is_closed_on_the_holiday():
    assert not market_loop.is_market_open(GANDHI_JAYANTI)
    assert market_loop.is_market_open(NEXT_MONDAY)
    assert live_bridge.is_market_open is market_loop.is_market_open        # the bridge shares the one door
    assert not live_pricer._market_open(GANDHI_JAYANTI) and live_pricer._market_open(NEXT_MONDAY)
    assert ms.session_over(GANDHI_JAYANTI.replace(hour=9, minute=10))
    assert not ms.session_over(NEXT_MONDAY.replace(hour=9, minute=10))


def test_the_scheduler_names_the_holiday_and_arms_nothing(capsys):
    armed, cards = [], []

    async def _loop():
        armed.append(1)

    out = asyncio.run(ms.run_trading_session(
        now_fn=lambda: GANDHI_JAYANTI.replace(hour=9, minute=10), entry_loop=_loop, exit_loop=_loop,
        notify_fn=lambda text: cards.append(text), playbook_fn=lambda u: [], account_fn=lambda: []))
    assert out == {"status": "market_closed", "reason": "NSE holiday: Mahatma Gandhi Jayanti",
                   "started": None, "ended": None}
    assert armed == [] and cards == []                                     # no loops, no Discord card
    assert "2026-10-02 is an NSE holiday (Mahatma Gandhi Jayanti)" in capsys.readouterr().out


def test_the_entry_loop_fetches_and_proposes_nothing_on_the_holiday():
    # the 10-02 failure itself: the loop polled Dhan and proposed on frozen chains
    calls = []
    clock = {"now": GANDHI_JAYANTI}

    def fetch(u):
        calls.append(("fetch", u))
        return None

    def now_fn():
        clock["now"] += timedelta(minutes=30)
        if clock["now"].hour >= 16:
            raise asyncio.CancelledError
        return clock["now"]

    async def go():
        try:
            await market_loop.run_market_loop(underlyings=("NIFTY 50",), interval=0, fetch_fn=fetch,
                                              propose_fn=lambda *a, **k: calls.append(("propose",)),
                                              now_fn=now_fn, cooldown=market_loop.CooldownRegistry())
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert calls == []


def test_the_capture_jobs_skip_the_holiday_by_name():
    hit = []
    s = chain_archiver.run(today=date(2026, 10, 2), chain_fn=lambda *a: hit.append(a), expiry_fn=lambda u: hit.append(u))
    assert s["skipped"] == "NSE holiday: Mahatma Gandhi Jayanti" and s["captured"] == {} and hit == []
    assert chain_archiver.run(today=date(2026, 10, 3), chain_fn=lambda *a: hit.append(a))["skipped"] == "weekend"
    d = intraday_tracker.capture_darlings(clock=lambda: GANDHI_JAYANTI.replace(hour=15, minute=50),
                                            price_fn=lambda t: hit.append(t))
    assert d["skipped"] == "nse_holiday: Mahatma Gandhi Jayanti" and d["captured"] == 0 and hit == []


def test_weekday_only_heartbeats_are_excused_on_the_holiday(tmp_path):
    (tmp_path / "daily.log").write_text("x")
    old = (datetime(2026, 10, 1, 12, 0)).timestamp()
    os.utime(tmp_path / "daily.log", (old, old))
    jobs = {"weekday.log": True, "daily.log": False}
    missing = ops_monitor.check_heartbeats(tmp_path, now=datetime(2026, 10, 2, 20, 30), expected=jobs)
    assert missing == ["daily.log — did not run today"]                    # the weekday-only job is excused
    monday = ops_monitor.check_heartbeats(tmp_path, now=datetime(2026, 10, 5, 20, 30), expected=jobs)
    assert "weekday.log — did not run today" in monday


def test_the_ops_card_nags_about_a_provisional_or_missing_year():
    assert cal.calendar_warning(date(2026, 10, 4)) is None and ops_monitor.calendar_alarm(datetime(2026, 10, 4)) is None
    assert cal.calendar_warning(date(2026, 12, 14)) is None                # NSE's circular is due mid-December
    amber = ops_monitor.calendar_alarm(datetime(2026, 12, 15, 20, 30))
    assert amber["red"] is False and "2027 is PROVISIONAL" in amber["text"] and amber["kind"] == "nse_calendar"
    assert "2027 is PROVISIONAL" in cal.calendar_warning(date(2027, 3, 1))
    red = ops_monitor.calendar_alarm(datetime(2028, 1, 3, 20, 30))
    assert red["red"] is True and "2028 is UNAVAILABLE" in red["text"]
    assert cal.year_status(2026) == "verified" and cal.year_status(2027) == "provisional" and cal.year_status(2028) == "missing"
    assert cal.is_trading_day(date(2028, 1, 26))                           # a missing year is never guessed

"""
Phase 7A master scheduler tests — whole sessions driven offline with a
fake IST clock and stub loops; no network, no real journal/DB, and the
Phase 6J muzzle keeps Discord silent even if a bookend slips through.

Run from the project folder:
    python tests/test_master_scheduler.py      (simple, no extra installs)
    python -m pytest tests/                    (if you have pytest)
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import master_scheduler as ms
from src.market_loop import IST

MONDAY_PRE_OPEN = datetime(2026, 7, 6, 9, 0, tzinfo=IST)
MONDAY_MIDDAY = datetime(2026, 7, 6, 11, 0, tzinfo=IST)
MONDAY_EVENING = datetime(2026, 7, 6, 16, 0, tzinfo=IST)
SATURDAY = datetime(2026, 7, 4, 11, 0, tzinfo=IST)


class FakeClock:
    """An IST clock the tests wind forward by hand."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


async def _idle_loop():
    while True:
        await asyncio.sleep(0.01)


def run(coro):
    return asyncio.run(coro)


# --- the session window ----------------------------------------------------

def test_seconds_until_open_and_session_over():
    assert ms.seconds_until_open(MONDAY_PRE_OPEN) == 15 * 60
    assert ms.seconds_until_open(MONDAY_MIDDAY) == 0.0
    assert not ms.session_over(MONDAY_MIDDAY)
    assert ms.session_over(MONDAY_EVENING)
    assert ms.session_over(SATURDAY)          # weekend, any hour
    # the boundary minute itself still counts as open
    close_dt = datetime(2026, 7, 6, 15, 30, tzinfo=IST)
    assert not ms.session_over(close_dt)
    assert ms.session_over(close_dt + timedelta(minutes=1))


def test_past_close_launch_exits_immediately_without_loops():
    started = []

    async def tracked():
        started.append(True)
        await _idle_loop()

    summary = run(ms.run_trading_session(
        ("NIFTY BANK",), now_fn=lambda: MONDAY_EVENING,
        entry_loop=tracked, exit_loop=tracked,
        notify_fn=lambda text: None, playbook_fn=lambda u, **k: [],
        account_fn=lambda: []))
    assert summary["status"] == "market_closed"
    assert started == []                      # nothing was ever armed


def test_session_runs_loops_and_closes_itself_at_1530():
    clock = FakeClock(MONDAY_MIDDAY)
    alive = {"entry": False, "exit": False}
    notes = []

    async def entry():
        alive["entry"] = True
        try:
            await _idle_loop()
        finally:
            alive["entry"] = False

    async def exit_():
        alive["exit"] = True
        try:
            await _idle_loop()
        finally:
            alive["exit"] = False

    async def scenario():
        task = asyncio.create_task(ms.run_trading_session(
            ("NIFTY BANK",), now_fn=clock, entry_loop=entry,
            exit_loop=exit_, notify_fn=notes.append,
            playbook_fn=lambda u, **k: ["playbook line"],
            account_fn=lambda: ["account line"], poll_seconds=0.02))
        await asyncio.sleep(0.05)
        assert alive["entry"] and alive["exit"]   # both loops armed
        clock.advance(hours=5)                    # 16:00 — past the close
        return await asyncio.wait_for(task, timeout=2)

    summary = run(scenario())
    assert summary["status"] == "completed"
    assert not alive["entry"] and not alive["exit"]  # cleanly cancelled
    # bookends: one OPEN card (with playbook), one CLOSED card
    assert len(notes) == 2
    assert "OPEN" in notes[0] and "playbook line" in notes[0]
    assert "CLOSED" in notes[1] and "account line" in notes[1]


def test_stop_event_shuts_the_session_down_gracefully():
    clock = FakeClock(MONDAY_MIDDAY)
    stop = None
    notes = []

    async def scenario():
        nonlocal stop
        stop = asyncio.Event()
        task = asyncio.create_task(ms.run_trading_session(
            ("NIFTY BANK",), now_fn=clock, entry_loop=_idle_loop,
            exit_loop=_idle_loop, notify_fn=notes.append,
            playbook_fn=lambda u, **k: [], account_fn=lambda: [],
            stop_event=stop, poll_seconds=0.02))
        await asyncio.sleep(0.05)
        stop.set()                                # SIGINT/SIGTERM path
        return await asyncio.wait_for(task, timeout=2)

    summary = run(scenario())
    assert summary["status"] == "stopped"
    assert any("STOPPED" in n for n in notes)


def test_pre_open_launch_waits_and_stop_cancels_the_wait():
    clock = FakeClock(MONDAY_PRE_OPEN)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(ms.run_trading_session(
            ("NIFTY BANK",), now_fn=clock, entry_loop=_idle_loop,
            exit_loop=_idle_loop, notify_fn=lambda t: None,
            playbook_fn=lambda u, **k: [], account_fn=lambda: [],
            stop_event=stop, poll_seconds=0.02))
        await asyncio.sleep(0.05)
        assert not task.done()                    # still waiting for 09:15
        stop.set()
        return await asyncio.wait_for(task, timeout=2)

    assert run(scenario())["status"] == "stopped"


def test_a_dying_loop_brings_the_session_down_safely():
    clock = FakeClock(MONDAY_MIDDAY)

    async def doomed():
        await asyncio.sleep(0.03)
        raise RuntimeError("feed exploded")

    async def scenario():
        return await asyncio.wait_for(ms.run_trading_session(
            ("NIFTY BANK",), now_fn=clock, entry_loop=doomed,
            exit_loop=_idle_loop, notify_fn=lambda t: None,
            playbook_fn=lambda u, **k: [], account_fn=lambda: [],
            poll_seconds=0.02), timeout=2)

    summary = run(scenario())
    assert summary["status"] == "stopped"         # never a zombie session


# --- the bookend content builders -------------------------------------------

def test_playbook_lines_ride_the_planner():
    fake_state = {"analysis": {"ticker": "NIFTY BANK", "uptrend": True,
                               "fresh_cross": False, "rsi": 55.0,
                               "price": 52_340.0},
                  "vix": 14.5}
    lines = ms._playbook_lines(("NIFTY BANK",), fetch_fn=lambda u: fake_state)
    assert len(lines) == 1
    # neutral view + tradeable-IV -> the planner's iron condor with economics
    assert "iron condor" in lines[0]
    assert "max profit" in lines[0]


def test_playbook_reports_a_dead_feed_without_raising():
    lines = ms._playbook_lines(("NIFTY BANK",), fetch_fn=lambda u: None)
    assert lines == ["NIFTY BANK: no live read this minute"]


# --- Update 1: Unrealized P&L + Net Equity on the CLOSED card ------------

_FAKE_ACCT = {"equity": 1_000_000.0, "realized_pnl": 5_000.0,
              "available_cash": 800_000.0, "locked_margin": 200_000.0,
              "open_locks": 3, "trading_halted": False}


def _patch_account(marks):
    from unittest import mock

    class _Conn:
        def close(self):
            pass
    return (mock.patch("src.brain_map.connect", return_value=_Conn()),
            mock.patch("src.portfolio_manager.account_summary",
                       return_value=dict(_FAKE_ACCT)),
            mock.patch("src.portfolio_report.get_live_marks",
                       return_value=(marks, "engine_snapshot")))


def test_account_lines_add_unrealized_and_net_equity():
    from unittest import mock
    marks = [{"live_pnl_rs": 1500.0}, {"live_pnl_rs": -400.0},
             {"live_pnl_rs": None}]  # one unmarked
    p1, p2, p3 = _patch_account(marks)
    with p1, p2, p3:
        lines = ms._account_lines()
    text = "\n".join(lines)
    assert "Unrealized P&L Rs.+1,100.00 (2 marked, 1 unmarked)" in text
    # net equity = 1,000,000 realized + 1,100 unrealized
    assert "Net Equity Rs.1,001,100.00" in text
    assert "realized P&L Rs.5,000.00" in text


def test_account_lines_omit_unrealized_when_no_marks_fail_open():
    p1, p2, p3 = _patch_account([])   # no marks at all
    with p1, p2, p3:
        lines = ms._account_lines()
    text = "\n".join(lines)
    assert "Unrealized" not in text and "Net Equity" not in text
    assert "Equity Rs.1,000,000.00" in text   # realized line still there


def test_account_lines_survive_a_broken_marks_read():
    from unittest import mock

    class _Conn:
        def close(self):
            pass
    with mock.patch("src.brain_map.connect", return_value=_Conn()), \
         mock.patch("src.portfolio_manager.account_summary",
                    return_value=dict(_FAKE_ACCT)), \
         mock.patch("src.portfolio_report.get_live_marks",
                    side_effect=RuntimeError("snapshot boom")):
        lines = ms._account_lines()
    # the card still renders — marks failure is swallowed, realized shown
    assert any("Equity Rs.1,000,000.00" in l for l in lines)
    assert not any("Net Equity" in l for l in lines)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

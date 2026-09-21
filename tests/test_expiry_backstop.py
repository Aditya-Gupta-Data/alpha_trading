"""
Expiry backstop (2026-09-11 hotfix, ledger Issue 28): a spread whose expiry
date has passed is force-settled by the WALL CLOCK even when the price feed
is dead, and its margin lock is released. Normal pre-expiry logic must be
untouched.

Offline — no Dhan, no Gemini, no email; the tracker runs against a fake
journal and a temp Brain Map, and NEVER touches the live data/ files.

Run from the project folder:
    python -m pytest tests/test_expiry_backstop.py -q
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import analyst
from src import brain_map
import src.plan_tracker as plan_tracker
from src import portfolio_manager as pm
from tests.test_options_spreads import (make_open_spread, FakeJournal,
                                        RANGE_BARS, CRASH_BARS)

EXPIRY = date(2026, 7, 26)          # make_open_spread's expiry


@pytest.fixture(autouse=True)
def _restore_seams():
    saved = {k: getattr(plan_tracker, k) for k in (
        "journal", "_daily_bars", "_settle_spread_cash",
        "_close_paper_position", "_brain_connect", "_today")}
    saved_pm = pm.release_entry
    saved_an = analyst.generate_post_mortem
    yield
    for k, v in saved.items():
        setattr(plan_tracker, k, v)
    pm.release_entry = saved_pm
    analyst.generate_post_mortem = saved_an


def run_tracker_on(tmp, entries, bars, today: date, feed_raises=False):
    """run_tracker with every external surface patched out, the calendar
    pinned to `today`, and margin releases captured."""
    db_path = Path(tmp) / "brain_map.db"
    fj = FakeJournal(entries)
    settled, released = [], []
    plan_tracker.journal = fj
    if feed_raises:
        def _dead(ticker, start):
            raise RuntimeError("DH-902 Invalid_Access")
        plan_tracker._daily_bars = _dead
    else:
        plan_tracker._daily_bars = lambda ticker, start: bars
    plan_tracker._settle_spread_cash = lambda pnl: settled.append(pnl) or True
    plan_tracker._close_paper_position = lambda entry, price, *a, **k: True
    plan_tracker._brain_connect = lambda: brain_map.connect(db_path)
    plan_tracker._today = lambda: today
    pm.release_entry = lambda ref, pnl=0.0, conn=None: released.append((ref, pnl)) or {}
    analyst.generate_post_mortem = lambda plan, execution: None
    resolved = plan_tracker.run_tracker(email=False)
    return resolved, fj, settled, released


# ------------------------------------------------ the backstop itself

def test_no_data_before_expiry_still_waits():
    """The wall clock only bites AFTER expiry: an open spread with a dead
    feed on expiry day itself is left alone, exactly as before."""
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        resolved, fj, settled, released = run_tracker_on(tmp, [entry], [], EXPIRY)
        assert resolved == 0 and fj.rewritten is None
        assert settled == [] and released == []


def test_no_data_inside_grace_window_still_waits():
    """One day past expiry with NO bars: give the feed a chance to come back
    (a one-day outage recovers the real close via path 1)."""
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        day = date(2026, 7, 27)
        assert (day - EXPIRY).days < plan_tracker.EXPIRY_BACKSTOP_GRACE_DAYS
        resolved, *_ = run_tracker_on(tmp, [entry], [], day)
        assert resolved == 0


def test_no_data_past_grace_settles_at_max_loss_and_releases_margin():
    """Issue 28 exactly: expiry long gone, feed dead. Settle conservatively
    at the defined max loss, name the basis, release the lock."""
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread(lots=2)
        spread = entry["spread"]
        max_loss_total = float(spread["max_loss"]) * 2
        resolved, fj, settled, released = run_tracker_on(
            tmp, [entry], [], date(2026, 8, 25))
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["resolution"] == "expiry_backstop"
        assert o["settlement_basis"] == "no_price_data_max_loss"
        assert o["settlement_close_date"] is None
        assert o["settled_on"] == "2026-08-25"
        assert o["exit_date"] == "2026-07-26"        # settled AT expiry
        assert o["pnl_rs"] == pytest.approx(-max_loss_total)
        assert o["frictions_rs"] == 0.0 and o["slippage_rs"] == 0.0
        assert o["r_multiple"] == pytest.approx(-1.0)
        assert o["position_closed"] is True
        assert "NO PRICE DATA" in o["verdict"] and "max loss" in o["verdict"]
        assert settled == [pytest.approx(-max_loss_total)]
        assert released == [("sp123456", pytest.approx(-max_loss_total))]


def test_feed_exception_does_not_kill_the_sweep_and_backstop_still_fires():
    """A raising price feed (the dead-token shape) is caught per entry; the
    wall clock still settles the expired spread."""
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        resolved, fj, _, released = run_tracker_on(
            tmp, [entry], [], date(2026, 8, 25), feed_raises=True)
        assert resolved == 1
        assert fj.rewritten[0]["outcome"]["settlement_basis"] == "no_price_data_max_loss"
        assert len(released) == 1


def test_gap_over_exit_window_settles_at_last_close_before_expiry():
    """Bars stop on the 10th (too early for the 65% profit-take, and the
    2-day pre-expiry trigger never had a bar to fire on). Past expiry,
    settle at intrinsic on that last close, dated AT expiry, and say which
    close was used."""
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        bars = [b for b in RANGE_BARS if b[0] <= "2026-07-10"]
        resolved, fj, settled, released = run_tracker_on(
            tmp, [entry], bars, date(2026, 7, 27))
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["resolution"] == "expiry_backstop"
        assert o["settlement_basis"] == "last_close_on_or_before_expiry"
        assert o["settlement_close_date"] == "2026-07-10"
        assert o["exit_date"] == "2026-07-26"
        # Condor body 24800/25200 with spot 25000 at expiry: every leg expires
        # worthless, the full net credit is kept -> max profit, less costs.
        max_profit = float(entry["spread"]["max_profit"])
        assert 0 < o["pnl_rs"] < max_profit
        assert o["frictions_rs"] > 0                 # costs ARE charged when a price exists
        assert released and released[0][0] == "sp123456"
        assert "intrinsic" in o["verdict"]


def test_post_expiry_bars_do_not_mark_the_exit_on_a_post_expiry_close(monkeypatch):
    """Feed returns AFTER expiry with only post-expiry bars: the bar walk
    would have 'pre-expiry exited' on a bar dated after expiry. The
    backstop takes over with the last close ON OR BEFORE expiry."""
    # The per-trade stop (decision #103) is pinned OFF: the pre-expiry crash
    # bars would stop this spread out first, and the backstop is the point.
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.0)
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        entry["spread"]["max_profit"] = 0.0          # profit-take can never fire
        bars = [b for b in CRASH_BARS if b[0] <= "2026-07-15"] + \
               [("2026-07-28", 25900.0, 26100.0, 26000.0),
                ("2026-07-29", 25900.0, 26100.0, 26000.0)]
        resolved, fj, *_ = run_tracker_on(tmp, [entry], bars, date(2026, 7, 30))
        o = fj.rewritten[0]["outcome"]
        assert resolved == 1
        assert o["resolution"] == "expiry_backstop"
        assert o["settlement_close_date"] == "2026-07-15"
        assert o["exit_date"] == "2026-07-26"


def test_skipped_spread_backstops_hypothetically_with_zero_release():
    """A rejected proposal never locked capital: it still gets an outcome
    (learning), but settles no cash and releases at 0."""
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread(decision="rejected")
        resolved, fj, settled, released = run_tracker_on(
            tmp, [entry], [], date(2026, 8, 25))
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["hypothetical"] is True and o["position_closed"] is False
        assert settled == [] and released == [("sp123456", 0.0)]


# ------------------------------------------------ normal logic untouched

def test_normal_pre_expiry_exit_is_unchanged_when_bars_reach_the_window():
    """With bars through the exit window, the 2-day rule fires exactly as
    before — dated 07-24, resolution pre_expiry_exit, no backstop keys —
    even when the calendar is already past expiry."""
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        entry["spread"]["max_profit"] = 0.0
        resolved, fj, *_ = run_tracker_on(tmp, [entry], RANGE_BARS, date(2026, 8, 25))
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["resolution"] == "pre_expiry_exit"
        assert o["exit_date"] == "2026-07-24"
        assert "settlement_basis" not in o


def test_live_spread_before_expiry_with_bars_is_still_live():
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        entry["spread"]["max_profit"] = 0.0
        bars = [b for b in RANGE_BARS if b[0] <= "2026-07-15"]
        resolved, fj, *_ = run_tracker_on(tmp, [entry], bars, date(2026, 7, 16))
        assert resolved == 0 and fj.rewritten is None


def test_backstop_helper_is_none_on_or_before_expiry():
    entry = make_open_spread()
    assert plan_tracker._expiry_backstop(entry, RANGE_BARS, EXPIRY) is None
    assert plan_tracker._expiry_backstop(entry, [], EXPIRY) is None


def test_outcome_line_names_the_backstop():
    entry = make_open_spread()
    entry["outcome"] = {"resolution": "expiry_backstop", "exit_date": "2026-07-26",
                        "pnl_rs": -6225.0, "r_multiple": -1.0, "days_in_trade": 20,
                        "frictions_rs": 0.0, "slippage_rs": 0.0,
                        "verdict": "EXPIRY BACKSTOP — NO PRICE DATA"}
    line = plan_tracker._spread_outcome_line(entry)
    assert line.startswith("EXPIRY BACKSTOP (wall-clock settlement)")

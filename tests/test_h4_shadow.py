"""H4 pyramid-continuation shadow (src/validation/h4_shadow.py).

Hermetic: in-memory sqlite, injected entries/bars/today — no journal, no
Dhan, no real brain_map. The muzzle test proves the suite CANNOT reach the
real DB through this seam even when every injection is forgotten (the
07-27 opportunity-cost lesson, applied from the first commit this time).
"""

import sqlite3
from datetime import date

import pytest

from src.validation import h4_shadow, trial
from src.discovery import shadow_runner

TODAY = date(2026, 7, 30)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    trial.ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS outcomes ("
                 "journal_ref TEXT, result TEXT, r_multiple REAL, date TEXT)")
    return conn


def _spread_entry(short_id="aaaa1111", strategy="bear_put_spread"):
    return {
        "short_id": short_id,
        "ticker": "NIFTY 50",
        "date": "2026-07-20",
        "outcome": None,
        "spread": {
            "strategy": strategy,
            "expiry": "2026-08-27",
            "lot_size": 75,
            "lots": 1,
            "max_loss": 3000.0,
            "max_profit": 4500.0,
            "entry_spot": 24600.0,
            "legs": [
                {"side": "BUY", "option_type": "PE", "strike": 24500.0,
                 "premium": 300.0},
                {"side": "SELL", "option_type": "PE", "strike": 24300.0,
                 "premium": 220.0},
            ],
        },
    }


def _bars(closes, end=TODAY):
    """Daily bars ending on `end`, one per calendar day (dates only need
    to be distinct and ordered — the pass keys on the LAST bar's date)."""
    from datetime import timedelta
    out = []
    for i, c in enumerate(closes):
        d = (end - timedelta(days=len(closes) - 1 - i)).isoformat()
        out.append({"date": d, "open": c, "high": c, "low": c, "close": c})
    return out


# A fresh 10-day LOW with real spot movement: bearish spread improves.
FALLING = [24900, 24880, 24860, 24840, 24820, 24800, 24780, 24760,
           24740, 24720, 24700, 24500, 24200]
# A fresh 10-day low but only just below entry spot: time decay dominates,
# the spread mark is NOT improved (intrinsics still zero, tv decayed).
SHALLOW = [24900, 24880, 24860, 24840, 24820, 24800, 24780, 24760,
           24740, 24720, 24700, 24650, 24580]
# No fresh extreme: last close bounces above recent lows.
BOUNCE = [24900, 24880, 24860, 24840, 24820, 24800, 24780, 24760,
          24740, 24200, 24700, 24710, 24750]


def test_fires_and_records_one_host_linked_row():
    conn = _conn()
    s = h4_shadow.run_shadow_pass(
        conn, entries=[_spread_entry()], bars_fn=lambda t: _bars(FALLING),
        today=TODAY)
    assert s["fired"] == 1 and s["scanned"] == 1
    row = conn.execute("SELECT * FROM shadow_trades").fetchone()
    assert row["pattern_id"] == "signal:h4_pyramid_lb10"
    assert row["host_ref"] == "aaaa1111"
    assert row["direction"] == "bearish"
    assert row["mode"] == trial.SIGNAL_MODE
    assert row["journal_ref"].startswith("shadow:")  # corpus-excluded prefix


def test_no_fire_without_fresh_extreme():
    conn = _conn()
    s = h4_shadow.run_shadow_pass(
        conn, entries=[_spread_entry()], bars_fn=lambda t: _bars(BOUNCE),
        today=TODAY)
    assert s["fired"] == 0
    assert s["skips"].get("no_fresh_extreme") == 1


def test_no_fire_when_mark_not_improved():
    conn = _conn()
    s = h4_shadow.run_shadow_pass(
        conn, entries=[_spread_entry()], bars_fn=lambda t: _bars(SHALLOW),
        today=TODAY)
    assert s["fired"] == 0
    assert s["skips"].get("mark_not_improved") == 1


def test_yesterdays_bar_is_the_NORMAL_case_and_fires():
    """THE REGRESSION THAT MATTERED (2026-07-30): Dhan's daily history only
    carries COMPLETED sessions, so the 20:00 IST pass sees yesterday's bar.
    The first version required bars[-1].date == today and would therefore
    have skipped every night forever — a shadow that silently never fires.
    A T-1 bar is normal operation, and the fire is dated by the BAR."""
    conn = _conn()
    s = h4_shadow.run_shadow_pass(
        conn, entries=[_spread_entry()],
        bars_fn=lambda t: _bars(FALLING, end=date(2026, 7, 29)),
        today=TODAY)
    assert s["fired"] == 1
    row = conn.execute("SELECT * FROM shadow_trades").fetchone()
    assert row["fire_date"] == "2026-07-29"   # the bar's date, not TODAY


def test_genuinely_stuck_feed_is_named():
    conn = _conn()
    s = h4_shadow.run_shadow_pass(
        conn, entries=[_spread_entry()],
        bars_fn=lambda t: _bars(FALLING, end=date(2026, 7, 20)),
        today=TODAY)
    assert s["fired"] == 0
    assert s["skips"].get("stale_feed") == 1


def test_bar_at_or_before_entry_is_not_continuation():
    conn = _conn()
    entry = _spread_entry()
    entry["date"] = "2026-07-29"        # entered the same day as the bar
    s = h4_shadow.run_shadow_pass(
        conn, entries=[entry],
        bars_fn=lambda t: _bars(FALLING, end=date(2026, 7, 29)),
        today=TODAY)
    assert s["fired"] == 0
    assert s["skips"].get("bar_not_after_entry") == 1


def test_rerun_on_the_same_bar_dedups():
    """Sat/Sun re-runs see Friday's bar again — they must not double-count."""
    conn = _conn()
    kw = dict(entries=[_spread_entry()], bars_fn=lambda t: _bars(FALLING),
              today=TODAY)
    assert h4_shadow.run_shadow_pass(conn, **kw)["fired"] == 1
    s2 = h4_shadow.run_shadow_pass(conn, **kw)
    assert s2["fired"] == 0
    assert s2["skips"].get("already_recorded_for_this_bar") == 1
    assert conn.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0] == 1


def test_stack_cap_stops_a_third_add():
    conn = _conn()
    for day in ("2026-07-28", "2026-07-29"):
        trial.record_signal_fire(conn, h4_shadow.SIGNAL, day, "NIFTY 50",
                                 direction="bearish", host_ref="aaaa1111")
    s = h4_shadow.run_shadow_pass(
        conn, entries=[_spread_entry()], bars_fn=lambda t: _bars(FALLING),
        today=TODAY)
    assert s["fired"] == 0
    assert s["skips"].get("stack_cap_reached") == 1


def test_resolved_entries_are_not_scanned():
    conn = _conn()
    entry = _spread_entry()
    entry["outcome"] = {"result": "win"}
    s = h4_shadow.run_shadow_pass(conn, entries=[entry],
                                  bars_fn=lambda t: _bars(FALLING),
                                  today=TODAY)
    assert s["scanned"] == 0
    assert s["skips"].get("no_open_spreads") == 1


def test_task_i_sweep_resolves_the_shadow_from_its_host():
    """The whole design premise: NO new resolver. A fired row inherits the
    host's outcome through the existing Sleep-Phase Task I sweep."""
    conn = _conn()
    h4_shadow.run_shadow_pass(conn, entries=[_spread_entry()],
                              bars_fn=lambda t: _bars(FALLING), today=TODAY)
    conn.execute("INSERT INTO outcomes VALUES (?, ?, ?, ?)",
                 ("aaaa1111", "win", 1.8, "2026-08-05"))
    assert shadow_runner.resolve_from_outcomes(conn) == 1
    row = conn.execute("SELECT * FROM shadow_trades").fetchone()
    assert row["resolved"] == 1
    assert row["result"] == "win"
    assert row["r_multiple"] == pytest.approx(1.8)


def test_the_suite_can_never_write_the_real_brain_map(monkeypatch):
    """Every injection forgotten -> the muzzle returns before the DB door
    is even imported. The bomb proves it."""
    from src import brain_map

    def _bomb(*a, **k):
        raise AssertionError("real brain_map.connect() reached from a test")

    monkeypatch.setattr(brain_map, "connect", _bomb)
    s = h4_shadow.run_shadow_pass(
        entries=[_spread_entry()], bars_fn=lambda t: _bars(FALLING),
        today=TODAY)
    assert s["fired"] == 0
    assert s["skips"].get("muzzled_under_pytest") == 1


def test_one_bad_entry_never_voids_the_pass():
    conn = _conn()
    bad = _spread_entry(short_id="bbbb2222")

    def flaky_bars(ticker):
        raise RuntimeError("feed down")

    s = h4_shadow.run_shadow_pass(conn, entries=[bad], bars_fn=flaky_bars,
                                  today=TODAY)
    assert s["fired"] == 0
    assert s["skips"].get("entry_error:RuntimeError") == 1

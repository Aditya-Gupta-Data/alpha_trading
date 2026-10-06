"""
Chunk 2 Fix E (2026-10-06) — audit F22 + F23.

F22: the expiry backstop settles a spread on the EXPIRY SESSION's own close
(the listed expiry date, or the last NSE session before it when that date
is not a trading day). Dhan serves day D's bar only around 05:30 on D+1 and
the hourly Auto-Sync runs all night, so the first sweep after midnight used
to book D-1's close as the expiry settlement, for good. Now it waits for the
expiry bar; only after the grace window may it fall back to an earlier
close, and then it says so (`stale_close_after_grace`) and dates it
(`settlement_close_date`). The no-bars-at-all max-loss path is unchanged.

F23: the forced pre-expiry exit also fires on the LAST NSE session strictly
before expiry. An expiry a holiday moved onto a Monday (NIFTY weekly Mon
2026-10-19, Dussehra; every November monthly Mon 2026-11-23, Guru Nanak)
had no session inside the 2-calendar-day window before expiry day itself.
One helper, `plan_tracker.in_forced_exit_window`, decides it for the bar
walk, the live arm (`live_pricer.evaluate`) and the live bridge's advisory.

Hermetic: brain_map.connect(':memory:'), bars and chains injected, the
primary's money side-effects stubbed, the journal stamp muzzled.
"""
import json
import tempfile
from datetime import date, datetime, timedelta

import pytest

from src import brain_map, live_bridge as lb, nse_calendar as cal, oms
from src import plan_tracker as pt, portfolio_manager as pm
from src.execution import live_pricer as lp, paper_venue as pv
from tests.test_expiry_backstop import run_tracker_on, _restore_seams  # noqa: F401  (autouse seam restore)

LIVE = pm.ACCOUNT_PAPER_2L_LIVE


# =================================================================== fixtures

def _bear_put(ref="fE22", expiry="2026-10-27"):
    """24f931bb's shape: BUY 13725PE 208.75 / SELL 13625PE 169.10 -> debit
    39.65, width 100 (max profit 60.35), lot 120."""
    spread = {"strategy": "bear_put_spread", "direction": "bearish", "expiry": expiry,
              "lot_size": 120, "lots": 1, "spread_width": 100.0,
              "max_profit": round(60.35 * 120, 2), "max_loss": round(39.65 * 120, 2),
              "legs": [{"side": "BUY", "option_type": "PE", "strike": 13725.0, "premium": 208.75},
                       {"side": "SELL", "option_type": "PE", "strike": 13625.0, "premium": 169.10}]}
    return {"short_id": ref, "ticker": "NIFTY MID SELECT", "date": "2026-10-01", "decision": "approved",
            "outcome": None, "ratchet": None, "spread": spread}


# Daily bars as pt._daily_bars returns them: (date, low, high, close). Expiry Tue 10-27.
TO_THU = [("2026-10-21", 13600, 13800, 13760.0), ("2026-10-22", 13500, 13800, 13760.0)]
TO_MON = TO_THU + [("2026-10-23", 13600, 13800, 13740.0), ("2026-10-26", 13600, 13780, 13700.0)]
EXPIRY_BAR = ("2026-10-27", 13450, 13720, 13550.0)        # below both strikes: max profit


def _bull_call_monday(ref="fE23", ticker="NIFTY 50", expiry="2026-10-19", entry_day="2026-10-12"):
    spread = {"strategy": "bull_call_spread", "direction": "bullish", "expiry": expiry,
              "lot_size": 65, "lots": 1, "spread_width": 200.0, "entry_spot": 25000.0,
              "max_profit": 130.0 * 65, "max_loss": 70.0 * 65,
              "legs": [{"side": "BUY", "option_type": "CE", "strike": 25000.0, "premium": 100.0},
                       {"side": "SELL", "option_type": "CE", "strike": 25200.0, "premium": 30.0}]}
    return {"short_id": ref, "ticker": ticker, "date": entry_day, "decision": "approved",
            "outcome": None, "ratchet": None, "action": "SPREAD", "spread": spread}


@pytest.fixture
def primary_stubs(monkeypatch):
    released = []
    monkeypatch.setattr(pt, "_settle_spread_cash", lambda pnl: True)
    monkeypatch.setattr(pm, "release_entry", lambda sid, pnl, **k: released.append((sid, pnl)) or {})
    monkeypatch.setattr(pt, "_execute_paper_exit", lambda *a, **k: {"mode": "model"})
    return released


@pytest.fixture
def world(monkeypatch):
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.0)
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_now", lambda: datetime(2026, 10, 28, 0, 31))
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: None)
    lp.reset_cache()
    yield c
    lp.reset_cache()
    c.close()


def _open_live(c, ref="fE22"):
    e = _bear_put(ref)
    view = {"lots": 1, "legs": [{"side": "BUY", "option_type": "PE", "strike": 13725.0, "avg_fill_price": 208.75},
                                {"side": "SELL", "option_type": "PE", "strike": 13625.0, "avg_fill_price": 169.10}]}
    pm.paper_request_entry(c, LIVE, ref, 16764.0, lots=1, primary_lots=1)
    opened = lp.open_position(c, LIVE, e, view, now=datetime(2026, 10, 1, 11, 0))
    assert opened["entry_mark_ps"] == 39.65 and opened["max_profit_ps"] == 60.35
    return e


def _exit_event(c, ref):
    row = c.execute("SELECT detail FROM paper_account_events WHERE account_id = ? AND journal_ref = ? "
                    "AND event_type = ?", (LIVE, ref, lp.EVENT_EXIT)).fetchone()
    return json.loads(row[0])


# =================================================================== the calendar

def test_next_trading_day_skips_weekends_and_listed_holidays():
    assert cal.next_trading_day(date(2026, 10, 16)) == date(2026, 10, 19)      # Fri -> Mon
    assert cal.next_trading_day(date(2026, 10, 19)) == date(2026, 10, 21)      # over Dussehra Tue 10-20
    assert cal.next_trading_day(date(2026, 10, 1)) == date(2026, 10, 5)        # over Gandhi Jayanti + weekend
    assert cal.next_trading_day(datetime(2026, 10, 26, 11, 0)) == date(2026, 10, 27)
    assert cal.next_trading_day(date(2026, 10, 27)) == date(2026, 10, 28)      # strictly after


def test_the_expiry_session_is_the_listed_date_or_the_session_before_it():
    assert pt.expiry_session("2026-10-27") == date(2026, 10, 27)               # a normal Tuesday
    assert pt.expiry_session("2026-10-19") == date(2026, 10, 19)               # already moved by NSE
    assert pt.expiry_session("2026-10-20") == date(2026, 10, 19)               # listed on Dussehra
    assert pt.expiry_session(date(2026, 7, 26)) == date(2026, 7, 24)           # a Sunday-dated fixture


# =================================================================== F22 — the backstop

def test_backstop_waits_for_the_expiry_session_bar_inside_the_grace_window():
    """The repro: bars through Mon 10-26, swept Wed 10-28 ~00:30 — the 10-27
    bar is not served yet. Settling on Monday's close booked -14.65/share on
    a trade the expiry bar makes a max-profit +60.35. Now: wait."""
    e = _bear_put()
    assert pt._expiry_backstop(e, list(TO_MON), today=date(2026, 10, 28)) is None
    assert pt._expiry_backstop(e, list(TO_MON), today=date(2026, 10, 29)) is None     # day 2 of 3


def test_backstop_settles_on_the_expiry_bar_once_it_arrives():
    e = _bear_put()
    res = pt._expiry_backstop(e, TO_MON + [EXPIRY_BAR], today=date(2026, 10, 28))
    resolution, m_exit, frac, exit_day, close, basis, close_day = res
    assert resolution == pt.EXPIRY_BACKSTOP_RESOLUTION and exit_day == "2026-10-27"
    assert close == 13550.0 and close_day == "2026-10-27" and frac == 0.0
    assert basis == "last_close_on_or_before_expiry"
    assert m_exit - pt._spread_entry_mark(e["spread"]) == pytest.approx(60.35)        # max profit
    # post-expiry bars never price it
    res2 = pt._expiry_backstop(e, TO_MON + [EXPIRY_BAR, ("2026-10-28", 1, 2, 14000.0)],
                               today=date(2026, 10, 29))
    assert res2[4] == 13550.0 and res2[6] == "2026-10-27"


def test_backstop_falls_back_after_grace_and_names_and_dates_the_stale_close():
    e = _bear_put()
    res = pt._expiry_backstop(e, list(TO_MON), today=date(2026, 10, 30))               # 3 days past
    assert res[5] == pt.STALE_CLOSE_BASIS == "stale_close_after_grace"
    assert res[6] == "2026-10-26" and res[4] == 13700.0 and res[3] == "2026-10-27"
    assert res[1] - pt._spread_entry_mark(e["spread"]) == pytest.approx(-14.65)
    # the no-bars-at-all path is unchanged: max loss, no close
    nb = pt._expiry_backstop(e, [], today=date(2026, 10, 30))
    assert nb[5] == "no_price_data_max_loss" and nb[4] is None and nb[6] is None
    assert pt._expiry_backstop(e, [], today=date(2026, 10, 29)) is None


def test_backstop_on_an_expiry_listed_on_a_holiday_uses_the_session_before_it():
    """A listed expiry that is not a trading day settles on the last session
    before it; a bar on the listed date itself (the calendar was wrong — the
    exchange traded) wins over the calendar."""
    e = _bear_put(expiry="2026-10-20")                                                # Dussehra
    bars = [("2026-10-16", 1, 2, 13700.0), ("2026-10-19", 1, 2, 13550.0)]
    res = pt._expiry_backstop(e, bars, today=date(2026, 10, 21))
    assert res[5] == "last_close_on_or_before_expiry" and res[6] == "2026-10-19"
    assert pt._expiry_backstop(e, bars[:1], today=date(2026, 10, 21)) is None         # Monday not served
    res = pt._expiry_backstop(e, bars + [("2026-10-20", 1, 2, 13800.0)], today=date(2026, 10, 21))
    assert res[6] == "2026-10-20" and res[5] == "last_close_on_or_before_expiry"


def test_primary_row_waits_names_the_wait_then_settles_stale_after_grace(primary_stubs, monkeypatch):
    """The primary's settle door (_settle_spread_row): bars with a gap over the
    exit window (the walk never fires) end Thu 10-22. On Wed 10-28 it WAITS
    and says why; after the grace window it settles stale, named and dated."""
    e = _bear_put()
    monkeypatch.setattr(pt, "_today", lambda: date(2026, 10, 28))
    seen = {}
    pt._settle_spread_row(e, list(TO_THU), seen)       # (True only to persist the walk's ratchet stamp)
    assert seen["status"] == "awaiting_expiry_close" and e["outcome"] is None and primary_stubs == []
    monkeypatch.setattr(pt, "_today", lambda: date(2026, 10, 30))
    seen = {}
    assert pt._settle_spread_row(e, list(TO_THU), seen) is True
    o = e["outcome"]
    assert o["resolution"] == "expiry_backstop" and o["exit_date"] == "2026-10-27"
    assert o["settlement_basis"] == "stale_close_after_grace" and o["settlement_close_date"] == "2026-10-22"
    assert o["settled_on"] == "2026-10-30"
    assert "STALE CLOSE" in o["verdict"] and "review this row by hand" in o["verdict"]
    assert primary_stubs == [("fE22", o["pnl_rs"])]


def test_primary_row_never_settles_on_a_post_expiry_bar_while_the_backstop_waits(primary_stubs, monkeypatch):
    """The walk can 'pre-expiry exit' on a bar dated AFTER expiry when the
    series skips the window. That close prices a session the spread did not
    exist in: while the backstop waits for the expiry bar, nothing settles."""
    e = _bear_put()
    monkeypatch.setattr(pt, "_today", lambda: date(2026, 10, 29))
    seen = {}
    bars = TO_THU + [("2026-10-28", 13000, 13200, 13100.0)]
    pt._settle_spread_row(e, bars, seen)
    assert seen["status"] == "awaiting_expiry_close" and e["outcome"] is None and primary_stubs == []
    # the expiry bar arrives: the walk itself settles on it (expiry day is in the window)
    seen = {}
    assert pt._settle_spread_row(e, TO_THU + [EXPIRY_BAR, ("2026-10-28", 13000, 13200, 13100.0)], seen) is True
    assert e["outcome"]["exit_date"] == "2026-10-27"
    assert e["outcome"]["price"] - pt._spread_entry_mark(e["spread"]) == pytest.approx(60.35)


def test_run_tracker_prints_the_wait_for_the_expiry_close(capsys):
    """End to end through run_tracker: the waiting row is named, not called 'still live'."""
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, settled, released = run_tracker_on(tmp, [_bear_put()], list(TO_THU), date(2026, 10, 28))
    assert resolved == 0 and settled == [] and released == []
    out = capsys.readouterr().out
    assert ("NIFTY MID SELECT spread expired 2026-10-27 — waiting for the expiry session's own close "
            "(2026-10-27) before settling") in out
    assert "still live" not in out


def test_live_eod_sweep_waits_then_settles_on_the_expiry_bar(world):
    """The live arm's backstop (eod_sweep) is the same door: the 00:31 sweep
    waits (row open, lock held, reason named); the 05:35 sweep with the
    expiry bar settles at max profit and records which session priced it."""
    c = world
    _open_live(c)
    out = lp.eod_sweep(c, today=date(2026, 10, 28), bars_fn=lambda t, s: list(TO_MON),
                       now=datetime(2026, 10, 28, 0, 31))
    assert out["settled"] == [] and out["waiting"] == ["fE22"] and out["errors"] == []
    assert out["reasons"]["fE22"] == (
        "NIFTY MID SELECT expired 2026-10-27 and the expiry session's own close (2026-10-27) has not "
        "arrived yet — the newest close is 2026-10-26 (day 1 of the 3-day grace window; after it the "
        "newest close settles, named stale_close_after_grace)")
    assert lp.positions(c)[0]["state"] == "open"
    assert pm._active_shadow_lock(c, LIVE, "fE22") is not None
    out = lp.eod_sweep(c, today=date(2026, 10, 28), bars_fn=lambda t, s: TO_MON + [EXPIRY_BAR],
                       now=datetime(2026, 10, 28, 5, 35))
    s = out["settled"][0]
    assert s["close"] == 13550.0 and s["profit_ps"] == pytest.approx(60.35)
    assert s["basis"] == "last_close_on_or_before_expiry" and s["settlement_close_date"] == "2026-10-27"
    assert _exit_event(c, "fE22")["settlement_close_date"] == "2026-10-27"
    assert pm._active_shadow_lock(c, LIVE, "fE22") is None


def test_live_eod_sweep_after_grace_settles_stale_named_and_dated(world):
    c = world
    _open_live(c)
    out = lp.eod_sweep(c, today=date(2026, 10, 30), bars_fn=lambda t, s: list(TO_MON),
                       now=datetime(2026, 10, 30, 9, 0))
    s = out["settled"][0]
    assert s["basis"] == "stale_close_after_grace" and s["settlement_close_date"] == "2026-10-26"
    assert s["close"] == 13700.0 and s["profit_ps"] == pytest.approx(-14.65)
    row = lp.positions(c)[0]
    assert row["state"] == "closed" and row["settlement_basis"] == "stale_close_after_grace"
    assert _exit_event(c, "fE22")["settlement_close_date"] == "2026-10-26"


# =================================================================== F23 — the forced-exit window

def _old_rule(ticker, expiry, day):
    """The pre-F23 predicate: calendar days only."""
    return (expiry - day).days <= pt._forced_exit_days(ticker)


def _sessions(start, end):
    d = start
    while d <= end:
        if cal.is_trading_day(d):
            yield d
        d += timedelta(days=1)


def test_a_monday_expiry_puts_the_friday_session_in_the_window():
    mon = date(2026, 10, 19)                                       # NIFTY weekly, Dussehra Tue 10-20
    assert pt.in_forced_exit_window("NIFTY 50", mon, date(2026, 10, 16))        # Fri: 3 days out
    assert not pt.in_forced_exit_window("NIFTY 50", mon, date(2026, 10, 15))    # Thu
    assert pt.in_forced_exit_window("NIFTY 50", "2026-10-19", "2026-10-16")     # ISO strings too
    # every November monthly: Mon 11-23 (Guru Nanak Tue 11-24); and the 11-09 weekly
    for idx in ("NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY MID SELECT"):
        assert pt.in_forced_exit_window(idx, date(2026, 11, 23), date(2026, 11, 20))
        assert not pt.in_forced_exit_window(idx, date(2026, 11, 23), date(2026, 11, 19))
    assert pt.in_forced_exit_window("NIFTY 50", date(2026, 11, 9), date(2026, 11, 6))


def test_a_holiday_monday_before_a_tuesday_expiry_puts_friday_in_the_window():
    """Republic Day Mon 2026-01-26: a Tue 01-27 expiry's last session before
    expiry is Fri 01-23 (4 calendar days out). Before F23 the first session in
    the window was expiry day itself."""
    assert pt.in_forced_exit_window("NIFTY 50", date(2026, 1, 27), date(2026, 1, 23))
    assert not _old_rule("NIFTY 50", date(2026, 1, 27), date(2026, 1, 23))


@pytest.mark.parametrize("expiry", [date(2026, 10, 27), date(2026, 11, 3), date(2026, 9, 29),
                                    date(2026, 10, 29), date(2026, 7, 23)])     # Tuesdays + Thursdays
def test_normal_tuesday_and_thursday_expiries_are_unchanged(expiry):
    for day in _sessions(expiry - timedelta(days=20), expiry):
        assert pt.in_forced_exit_window("NIFTY 50", expiry, day) == _old_rule("NIFTY 50", expiry, day), day


@pytest.mark.parametrize("expiry", [date(2026, 10, 27), date(2026, 11, 23), date(2026, 12, 29),
                                    date(2026, 10, 19)])
def test_stock_options_seven_day_rule_is_unaffected(expiry):
    """A stock option's 7-day window always contains its last session."""
    for day in _sessions(expiry - timedelta(days=30), expiry):
        assert pt.in_forced_exit_window("RELIANCE.NS", expiry, day) == _old_rule("RELIANCE.NS", expiry, day), day
    assert pt.in_forced_exit_window("RELIANCE.NS", expiry, expiry - timedelta(days=7))


def test_primary_walk_exits_a_monday_expiry_on_fridays_close():
    """The repro: bars through Fri 10-16 for the Mon 10-19 weekly. The walk
    used to return None (Friday is 3 days out) and the backstop took Friday's
    INTRINSIC on Tuesday night; now the walk exits on Friday's close."""
    e = _bull_call_monday()
    bars = [("2026-10-13", 24900, 25100, 24980.0), ("2026-10-14", 24900, 25100, 25000.0),
            ("2026-10-15", 24900, 25100, 24990.0), ("2026-10-16", 24900, 25100, 25010.0)]
    assert pt._resolve_spread(dict(e), bars[:3]) is None                           # Thursday: still held
    hit = pt._resolve_spread(dict(e), bars)
    assert hit[0] == "pre_expiry_exit" and hit[3] == "2026-10-16"
    # the trail-aware shadow resolver (Glassbreaking grader) runs its own walk
    # when the entry carries a trail spec — it asks the same predicate
    trailed = dict(e, plan={"trailing": {"atr_mult": 3.0, "atr_n": 14}})
    assert pt._resolve_spread_trailed(dict(trailed), bars[:3]) is None
    assert pt._resolve_spread_trailed(dict(trailed), bars)[0::3] == ("pre_expiry_exit", "2026-10-16")


def test_primary_run_tracker_settles_a_monday_expiry_on_friday_not_by_backstop():
    with tempfile.TemporaryDirectory() as tmp:
        bars = [("2026-10-14", 24900, 25100, 25000.0), ("2026-10-16", 24900, 25100, 25010.0)]
        resolved, fj, settled, released = run_tracker_on(tmp, [_bull_call_monday()], bars, date(2026, 10, 16))
    o = fj.rewritten[0]["outcome"]
    assert resolved == 1 and o["resolution"] == "pre_expiry_exit" and o["exit_date"] == "2026-10-16"
    assert "settlement_basis" not in o


def test_normal_tuesday_expiry_walk_still_exits_on_monday():
    e = _bull_call_monday(expiry="2026-10-27", entry_day="2026-10-19")
    bars = [("2026-10-22", 1, 2, 25000.0), ("2026-10-23", 1, 2, 25000.0), ("2026-10-26", 1, 2, 25000.0)]
    assert pt._resolve_spread(dict(e), bars[:2]) is None                           # Fri 10-23: 4 days out
    assert pt._resolve_spread(dict(e), bars)[3] == "2026-10-26"


def _live_row(ticker, expiry):
    return {"ticker": ticker, "expiry": expiry, "strategy": "bull_call_spread", "direction": "bullish",
            "entry_mark_ps": 70.0, "max_loss_ps": 70.0, "max_profit_ps": 130.0,
            "ratchet_peak_pct": None, "ratchet_lock_pct": None, "account_id": LIVE, "journal_ref": "x",
            "legs": [{"side": "BUY", "option_type": "CE", "strike": 25000.0, "entry_fill": 100.0},
                     {"side": "SELL", "option_type": "CE", "strike": 25200.0, "entry_fill": 30.0}]}


# A flat book: crossed mark 100 - 31 = 69 < the 70 debit -> no profit, no ratchet arm.
FLAT = {"oc": {"25000.000000": {"ce": {"top_bid_price": 100.0, "top_ask_price": 101.0, "last_price": 100.5}},
               "25200.000000": {"ce": {"top_bid_price": 30.0, "top_ask_price": 31.0, "last_price": 30.5}}}}


def test_live_arm_exits_a_monday_expiry_on_friday():
    row = _live_row("NIFTY 50", "2026-10-19")
    fri = lp.evaluate(row, FLAT, date(2026, 10, 16))
    assert fri["signal"] == "pre_expiry_exit" and fri["days_left"] == 3
    assert lp.evaluate(row, FLAT, date(2026, 10, 15))["signal"] == "hold"
    assert lp._exit_window(row, date(2026, 10, 16)) == (True, 3)                  # the F02 telemetry agrees
    assert lp._exit_window(row, date(2026, 10, 15)) == (False, 4)


def test_live_bridge_advisory_exits_a_monday_expiry_on_friday():
    e = _bull_call_monday()
    assert lb.evaluate_position(e, 25000.0, today=date(2026, 10, 16))["signal"] == "pre_expiry_exit"
    assert lb.evaluate_position(e, 25000.0, today=date(2026, 10, 15))["signal"] == "hold"
    # the non-ratchet branch (a condor) asks the same predicate
    condor = dict(e, spread=dict(e["spread"], strategy="iron_condor", direction="neutral"))
    assert lb.evaluate_position(condor, 25000.0, today=date(2026, 10, 16))["signal"] == "pre_expiry_exit"
    assert lb.evaluate_position(condor, 25000.0, today=date(2026, 10, 15))["signal"] == "hold"


@pytest.mark.parametrize("ticker,expiry", [
    ("NIFTY 50", date(2026, 10, 19)), ("NIFTY 50", date(2026, 10, 27)), ("NIFTY 50", date(2026, 11, 9)),
    ("NIFTY BANK", date(2026, 11, 23)), ("NIFTY 50", date(2026, 1, 27)), ("NIFTY 50", date(2026, 10, 29)),
    ("RELIANCE.NS", date(2026, 11, 23)), ("TCS.NS", date(2026, 10, 27))])
def test_the_primary_walk_the_live_arm_and_the_bridge_agree_on_every_session(ticker, expiry):
    """On every session in the month before expiry, a flat position is exited
    by the primary's bar walk exactly when the live arm and the live bridge
    say pre_expiry_exit — one predicate, three callers."""
    entry_day = expiry - timedelta(days=35)
    for day in _sessions(entry_day + timedelta(days=1), expiry):
        e = _bull_call_monday(ticker=ticker, expiry=expiry.isoformat(), entry_day=entry_day.isoformat())
        hit = pt._resolve_spread(e, [(day.isoformat(), 1, 2, 25000.0)])
        walk = hit is not None and hit[0] == "pre_expiry_exit"
        live = lp.evaluate(_live_row(ticker, expiry.isoformat()), FLAT, day)["signal"] == "pre_expiry_exit"
        bridge = lb.evaluate_position(e, 25000.0, today=day)["signal"] == "pre_expiry_exit"
        expected = pt.in_forced_exit_window(ticker, expiry, day)
        assert walk == live == bridge == expected, (ticker, expiry, day, walk, live, bridge)

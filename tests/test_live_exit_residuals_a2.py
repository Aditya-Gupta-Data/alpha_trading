"""Chunk 2 lows — residuals A2 (2026-10-08): the expiry backstop cancels a
still-working exit ticket past expiry (F08 residual), each unrecorded LIVE
fill is repaired on its own (F15 residual), and the test pins the
close-out verifiers asked for (F04, F05, F07, F08).

Hermetic: tmp brain map (conftest), chains/bars injected, journal stamp
muzzled. Run ONLY via pytest (conftest's write guard)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src import brain_map, oms, plan_tracker as pt, portfolio_manager as pm
from src import strategy_router as sr
from src.execution import live_pricer as lp, paper_venue as pv
from tests.test_live_exit_ownership_and_fills import (  # noqa: F401
    _Clock, _FakeDT, _bull_call, _chain, ENTRY, LATER, PX, LIVE, OPEN, _fail_second_leg_fill)

IST = timezone(timedelta(hours=5, minutes=30))
TWO_L = pm.ACCOUNT_PAPER_2L
UPTO_27 = [("2026-10-27", 24000.0, 24300.0, 24250.0)]
BARS = UPTO_27 + [("2026-10-28", 24000.0, 24400.0, 24350.0)]
REF = "f08cl1"


class _Raise:
    @staticmethod
    def sweep(conn, **kw):
        raise sqlite3.OperationalError("database is locked")


class _FillThenRaise:
    @staticmethod
    def sweep(conn, **kw):
        pv.sweep(conn, **kw)
        raise sqlite3.OperationalError("database is locked")


class _WallClock:
    t = datetime(2026, 10, 29, 10, 0, tzinfo=IST)


@pytest.fixture
def db(monkeypatch):
    """The tmp DEFAULT brain map (conftest points brain_map.DEFAULT_DB_PATH at
    tmp_path): `brain_map.connect()` with no path opens it, exactly what
    eod_sweep_standalone does in the API process."""
    c = brain_map.connect()
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.01)
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_now", lambda: _WallClock.t)          # IST-aware, like production
    stamps = []
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: stamps.append(row["journal_ref"]) or True)
    monkeypatch.setattr(sr, "datetime", _FakeDT)
    _Clock.t = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    lp.reset_cache()
    yield c, stamps
    lp.reset_cache()
    c.close()


def _open(c, ref=REF):
    e = {"short_id": ref, "date": "2026-09-29", "ticker": "NIFTY 50", "spread": _bull_call(), "signal": "t"}
    pm.request_entry(c, ref, 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, ref, 17550.0, lots=1, primary_lots=1)
    rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: ENTRY)
    assert rq["ok"], rq
    prop = {"ticker": e["ticker"], "short_id": ref, "signal": "t", "spread": dict(e["spread"], legs=rq["legs"])}
    issued = sr.issue(c, prop, journal_ref=ref, source="t", account_id=LIVE, lots=1)
    pv.sweep(c, stamp=False)
    assert oms.ticket_status(c, issued["ticket_id"]) == oms.FILLED
    lp.open_position(c, LIVE, e, oms.ticket_view(c, issued["ticket_id"]), quote_ts=rq["quote_ts"],
                     now=OPEN.replace(tzinfo=IST))
    return ref


def _row(c, ref=REF):
    return [r for r in lp.open_rows(c, LIVE) if r["journal_ref"] == ref][0]


def _state(c, ref=REF):
    r = c.execute("SELECT state, pnl_net FROM paper_live_positions WHERE account_id = ? AND journal_ref = ?",
                  (LIVE, ref)).fetchone()
    return tuple(r) if r else None


def _events(c, kind, ref=REF):
    return c.execute("SELECT COUNT(*) FROM paper_account_events WHERE account_id = ? AND journal_ref = ? "
                     "AND event_type = ?", (LIVE, ref, kind)).fetchone()[0]


def _lock(c, ref=REF):
    return pm._active_shadow_lock(c, LIVE, ref)


def _released_pnl(c, ref=REF):
    r = c.execute("SELECT pnl_net FROM paper_margin_locks WHERE account_id = ? AND journal_ref = ? "
                  "AND released_at IS NOT NULL", (LIVE, ref)).fetchall()
    return [x[0] for x in r]


def _arm_off(monkeypatch):
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    assert pm.live_account_enabled() is False


def _leave_exiting(c, monkeypatch, how):
    """Leave the LIVE row 'exiting' on EXPIRY DAY in one of three ways."""
    on_expiry = datetime(2026, 10, 28, 15, 26, tzinfo=IST)
    if how == "unfilled_exit_error":
        assert lp._exit(c, _row(c), PX, "pre_expiry_exit", on_expiry, venue_mod=_Raise)["status"] == "exit_error"
    elif how == "filled_then_crash":
        assert lp._exit(c, _row(c), PX, "pre_expiry_exit", on_expiry,
                        venue_mod=_FillThenRaise)["status"] == "exit_error"
    elif how == "basket_cut":
        real = _fail_second_leg_fill(monkeypatch)
        assert lp._exit(c, _row(c), PX, "pre_expiry_exit", on_expiry)["status"] == "exit_error"
        monkeypatch.setattr(oms, "apply_fill", real)
        # the 15:28 tick: past the cutoff -> the resume names it partial, nothing re-sold
        t = lp.tick(now=datetime(2026, 10, 28, 15, 28, tzinfo=IST), conn=c, chain_fn=lambda tk, x: LATER,
                    sleep_fn=lambda s: None, now_epoch_fn=lambda: 9000.0, interval_s=300)
        assert t["resumes"][0]["status"] == "partial", t
    assert _row(c)["state"] == "exiting" and _lock(c) is not None


def _production_run_tracker(monkeypatch, today, bars):
    """run_tracker exactly as the API's hourly loop calls it, its eod_sweep
    UNMUZZLED (own connection, `now` from `_now()`); the primary book is
    empty and the lock housekeeping is stubbed so ONLY the backstop can
    move this row or its lock."""
    monkeypatch.setattr(lp, "_is_test_env", lambda: False)
    monkeypatch.setattr(pt, "_today", lambda: today)
    monkeypatch.setattr(pt, "_daily_bars", lambda ticker, since: bars)
    monkeypatch.setattr(pt.journal, "read_all", lambda: [])
    monkeypatch.setattr(pt, "expire_pending_margin", lambda *a, **k: {})
    monkeypatch.setattr(pt, "reconcile_orphan_locks", lambda *a, **k: {})
    return pt.run_tracker(email=False)



# ------------------------------------------------------------- F08 residual
@pytest.mark.parametrize("how", ["unfilled_exit_error", "basket_cut"])
def test_a_working_exit_ticket_is_cancelled_past_expiry_while_the_backstop_waits(db, monkeypatch, how):
    """Past expiry the backstop cancels the row's still-working EXIT ticket
    even while it waits for the expiry session's close: a later venue sweep
    can no longer fill it at a pre-expiry limit, and the row settles at the
    expiry close's intrinsic (open legs) when the bar arrives."""
    c, _ = db
    _open(c)
    _leave_exiting(c, monkeypatch, how)
    working = [r[0] for r in c.execute("SELECT DISTINCT t.ticket_id FROM trade_tickets t JOIN trade_legs l "
                                       "ON l.ticket_id = t.ticket_id WHERE t.account_id = ? AND t.note LIKE "
                                       "'EXIT %' AND l.state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')",
                                       (LIVE,))]
    if how == "unfilled_exit_error":
        assert len(working) == 1
    _arm_off(monkeypatch)
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: UPTO_27,
                       now=datetime(2026, 10, 29, 0, 35, tzinfo=IST))
    assert out["waiting"] == [REF] and _row(c)["state"] == "exiting"
    for tid in working:
        assert oms.ticket_status(c, tid) in (oms.CANCELLED, oms.PARTIAL)   # nothing left working
    left = c.execute("SELECT COUNT(*) FROM trade_legs l JOIN trade_tickets t ON t.ticket_id = l.ticket_id "
                     "WHERE t.account_id = ? AND l.state NOT IN ('FILLED', 'CANCELLED', 'REJECTED')",
                     (LIVE,)).fetchone()[0]
    assert left == 0
    pv.sweep(c, today=date(2026, 10, 29), stamp=False)       # a later venue pass fills nothing of it
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: BARS,
                       now=datetime(2026, 10, 29, 5, 35, tzinfo=IST))
    (s,) = out["settled"]
    assert s["resolution"] == "expiry_backstop" and s["settlement_close_date"] == "2026-10-28"
    if how == "unfilled_exit_error":
        # nothing ever filled: the whole structure at the expiry close (24350:
        # long 350, short 150 -> 200), not the pre-expiry limits (150 - 60 = 90)
        assert s["exit_mark_ps"] == 200.0 and "partial" not in s
    else:
        # the short bought back at its fill (60), the long at the expiry close's intrinsic (350)
        assert s["partial"] == {"closed_at_fills": {"SELL 24200CE": 60.0},
                                "open_at_expiry": {"BUY 24000CE": 350.0}}
        assert s["exit_mark_ps"] == 290.0
    assert _lock(c) is None


# ------------------------------------------------------------- F15 residual
def _approve_unrecorded(a, monkeypatch, ref):
    """A LIVE entry whose ticket FILLED at approval but whose position row
    was never written (open_position raised): lock active, no row."""
    from src import options_proposer as op
    from tests.test_live_exit_ownership_and_fills import _bull_call as _bc
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    monkeypatch.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: ENTRY)
    e = {"short_id": ref, "date": "2026-09-29", "ticker": "NIFTY 50", "action": "SPREAD", "spread": _bc(),
         "signal": "t", "accounts": {LIVE: {"status": "approved", "lots": 1, "margin_rs": 17550.0}}}
    pm.request_entry(a, ref, 17550.0)
    pm.paper_request_entry(a, LIVE, ref, 17550.0, lots=1, primary_lots=1)
    real_open = lp.open_position
    monkeypatch.setattr(lp, "open_position",
                        lambda *x, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    op._execute_paper_entry(e, conn=a)
    monkeypatch.setattr(lp, "open_position", real_open)
    assert pm._active_shadow_lock(a, LIVE, ref) is not None and lp.position_state(a, LIVE, ref) is None


def test_one_ref_whose_repair_keeps_failing_never_blocks_the_others(monkeypatch, tmp_path):
    from tests.test_live_exit_ownership_and_fills import _setup as _s
    c = brain_map.connect(tmp_path / "bm.db")
    _s(c, monkeypatch)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    try:
        _approve_unrecorded(c, monkeypatch, "f15hol1")
        _approve_unrecorded(c, monkeypatch, "f15hol2")
        real_open = lp.open_position

        def flaky(conn, account, entry, view, **k):
            if entry.get("short_id") == "f15hol1":
                raise RuntimeError("persistent non-refusal failure")
            return real_open(conn, account, entry, view, **k)
        monkeypatch.setattr(lp, "open_position", flaky)
        for minute, epoch in ((5, 1000.0), (6, 1060.0)):
            t = lp.tick(now=OPEN.replace(minute=minute), conn=c, chain_fn=lambda tk, x: ENTRY,
                        sleep_fn=lambda s: None, now_epoch_fn=lambda e=epoch: e, interval_s=300)
        assert lp.position_state(c, LIVE, "f15hol2") == "open"           # repaired despite hol1
        assert lp.position_state(c, LIVE, "f15hol1") is None
        assert pm._active_shadow_lock(c, LIVE, "f15hol1") is not None    # its lock kept, retried
        n = c.execute("SELECT COUNT(*) FROM paper_account_events WHERE account_id = ? AND journal_ref = ? "
                      "AND event_type = ?", (LIVE, "f15hol1", lp.EVENT_REPAIR_FAILED)).fetchone()[0]
        assert n == 1                                                     # named once a day, not per tick
    finally:
        lp.reset_cache()
        c.close()


# ------------------------------------------------------------- pins (close-out verifiers)
def test_f05_a_partly_filled_leg_still_working_counts_as_in_flight_and_is_withdrawn(monkeypatch, tmp_path):
    """Pins `_exit_fills`' in-flight rule for a PARTIAL leg (verifier mutant M7:
    only PENDING counted): a half-filled, still-working leg must be withdrawn
    by the resume before any completion closes the remainder, else a later
    sweep fills the old remainder AND the completion — closed past the position.
    (The paper venue fills legs whole; a real venue would reach this.)"""
    from tests.test_live_exit_ownership_and_fills import (_setup as _s, _open as _o, _row as _r,
                                                          _exit_tickets as _xt)
    c = brain_map.connect(tmp_path / "bm.db")
    _s(c, monkeypatch)
    try:
        ref = _o(c)

        class HalfThenDie:
            @staticmethod
            def sweep(conn, **kw):
                pv.sweep(conn, fill_fraction_fn=lambda leg: 0.5, **kw)
                raise sqlite3.OperationalError("database is locked")
        res = lp._exit(c, _r(c), PX, "profit_take", OPEN.replace(minute=1), venue_mod=HalfThenDie)
        assert res["status"] == "exit_error", res
        (t1, st1), = _xt(c)
        legs = oms.ticket_view(c, t1)["legs"]
        assert st1 == oms.PARTIAL and all(l["state"] == oms.PARTIAL for l in legs), legs
        ex = lp._exit_fills(c, _r(c))
        assert ex["in_flight"] == [t1] and ex["filled_any"] and not ex["complete"]
        res = lp._resume_exiting(c, _r(c), OPEN.replace(minute=2), 300)
        assert res["status"] == "partial" and res["cancelled"] == {t1: oms.PARTIAL}
        assert all(l["state"] == oms.CANCELLED for l in oms.ticket_view(c, t1)["legs"])
    finally:
        lp.reset_cache()
        c.close()


def test_f07_a_settle_rolled_back_by_its_event_never_stamps_the_journal(monkeypatch, tmp_path):
    """Pins the order (verifier mutant M8): the journal stamp runs only AFTER
    the one commit — a settle rolled back by a failed live_exit event must not
    leave the journal saying 'closed' with a P&L that was never booked."""
    from tests.test_live_exit_ownership_and_fills import _setup as _s, _open as _o, _row as _r, _state as _st
    c = brain_map.connect(tmp_path / "bm.db")
    _s(c, monkeypatch)
    stamps = []
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: stamps.append(row["journal_ref"]) or True)
    try:
        ref = _o(c)
        real_log = pm.paper_log_event

        def log(conn, account, event_type, journal_ref, detail, *a, **k):
            if event_type == lp.EVENT_EXIT:
                raise sqlite3.OperationalError("disk I/O error")
            return real_log(conn, account, event_type, journal_ref, detail, *a, **k)
        monkeypatch.setattr(pm, "paper_log_event", log)
        res = lp._exit(c, _r(c), PX, "ratchet_hit", OPEN)
        assert res["status"] == "exit_error" and "rolled back" in res["reason"]
        assert stamps == [] and _st(c, ref)[0] == "exiting"
        assert pm._active_shadow_lock(c, LIVE, ref) is not None            # nothing booked
    finally:
        lp.reset_cache()
        c.close()


def test_f07_a_post_commit_record_failure_still_reports_settled_and_stamps(monkeypatch, tmp_path):
    """Pins the post-commit guard (verifier mutant M10): once the one commit
    has landed, a failure in a DERIVED record (paper_after_release: the curve
    point, the halt check) never turns the settled exit into 'exit_error' and
    never skips the journal stamp."""
    from tests.test_live_exit_ownership_and_fills import _setup as _s, _open as _o, _row as _r, _state as _st
    c = brain_map.connect(tmp_path / "bm.db")
    _s(c, monkeypatch)
    stamps = []
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: stamps.append(row["journal_ref"]) or True)
    try:
        ref = _o(c)
        monkeypatch.setattr(pm, "paper_after_release",
                            lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        res = lp._exit(c, _r(c), PX, "ratchet_hit", OPEN)
        assert res["status"] == "settled", res
        assert stamps == [ref] and _st(c, ref)[0] == "closed"
        assert pm._active_shadow_lock(c, LIVE, ref) is None
    finally:
        lp.reset_cache()
        c.close()

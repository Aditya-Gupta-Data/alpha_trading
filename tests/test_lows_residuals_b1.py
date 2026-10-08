"""Chunk 2 lows — residuals B1 (2026-10-08): the approval path.

  F12  an approval killed after issuing its entry tickets left them behind;
       the re-approval issued new ones and the sweep filled BOTH (a doubled
       entry; for a refused live arm, an orphan fill with no lock and no
       row). Now: one entry basket per account per ref (oms.withdraw_stale_entry).
  F14  (a) the switch-off arm watched only at session open — now per cycle
       (tested in test_live_entry_rules_l3); (b) a venue that RAISES before the
       live arm's ticket left an 'approved' verdict with no ticket and a held
       lock — now refused by name, the lock released.
  F16  a read error in expire_pending_lock's live-arm guard kept 'going' after
       SQLite had rolled the whole transaction back — now it propagates and
       NOTHING is expired.
Hermetic: the `desk` fixture of test_live_entry_rules_l3 (tmp brain map,
injected chains, the journal on tmp)."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src import brain_map, journal, oms, portfolio_manager as pm, strategy_router as sr
from src import options_proposer as op
from src.execution import live_pricer as lp, paper_venue as pv
from src.strategy import StrategyConstructor
from tests.test_live_entry_rules_l3 import desk, _propose, _call_book, EXPIRY, LIVE  # noqa: F401

IST = timezone(timedelta(hours=5, minutes=30))


class _Crash(BaseException):
    pass


def _bull_call(buy, sell):
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, buy, sell)
    for leg in s["legs"]:
        leg["fill_basis"] = "quoted"
    s.update(lots=1, expiry=EXPIRY, entry_spot=24000.0)
    return s


def _clock(monkeypatch, at):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(sr, "datetime", _DT)


def _tickets(c, ref, acct):
    return [tuple(r) for r in c.execute("SELECT ticket_id, status FROM trade_tickets WHERE journal_ref = ? "
                                        "AND account_id = ? AND note NOT LIKE 'EXIT %' ORDER BY issued_at",
                                        (ref, acct)).fetchall()]


def _crash_then_reapprove(desk, monkeypatch, ref, second_book, crash_at="sweep"):
    """The first approval is killed (BaseException, as a process kill) at its
    venue sweep — after its tickets are issued — and re-approved 5 min later."""
    _propose(ref, _bull_call(40.0, 20.0))
    desk["book"]["chain"] = _call_book(85.0, 25.0)
    _clock(monkeypatch, datetime(2026, 10, 21, 10, 0, 0, tzinfo=IST))
    real_sweep = pv.sweep
    monkeypatch.setattr(pv, "sweep", lambda *a, **k: (_ for _ in ()).throw(_Crash("killed")))
    with pytest.raises(_Crash):
        op.decide_pending(ref, approve=True, why="tap", human=True)
    assert journal.get_entry(ref)["decision"] == "pending_approval"
    monkeypatch.setattr(pv, "sweep", real_sweep)
    _clock(monkeypatch, datetime(2026, 10, 21, 10, 5, 0, tzinfo=IST))
    desk["book"]["chain"] = second_book
    return op.decide_pending(ref, approve=True, why="tap again", human=True)


def test_f12_a_re_approval_after_a_killed_one_fills_one_basket_per_account(desk, monkeypatch):
    c, ref = desk["c"], "b1r1"
    out = _crash_then_reapprove(desk, monkeypatch, ref, _call_book(85.0, 25.0))
    assert out["status"] == "approved"
    for acct in (oms.PRIMARY_ACCOUNT, LIVE):
        t = _tickets(c, ref, acct)
        assert [s for _, s in t].count(oms.FILLED) == 1, (acct, t)        # ONE entry basket
        assert all(s in (oms.FILLED, oms.CANCELLED) for _, s in t), (acct, t)   # the stale one withdrawn
    assert [int(r["lots"]) for r in lp.open_rows(c, LIVE) if r["journal_ref"] == ref] == [1]


def test_f12_a_refused_live_arm_leaves_no_orphan_fill_behind(desk, monkeypatch):
    """The market rallied below the R:R floor before the re-approval: LIVE is
    refused and its lock released — and its stale ticket from the killed
    approval is withdrawn first, so the sweep cannot fill it into an orphan."""
    c, ref = desk["c"], "b1r2"
    _crash_then_reapprove(desk, monkeypatch, ref, _call_book(165.0, 25.0))
    t = _tickets(c, ref, LIVE)
    assert t and all(s == oms.CANCELLED for _, s in t), t
    assert lp.position_state(c, LIVE, ref) is None and pm._active_shadow_lock(c, LIVE, ref) is None
    assert lp._repair_unrecorded(c, LIVE, datetime(2026, 10, 21, 10, 6)) == []


def test_f12_an_entry_ticket_with_a_fill_is_reused_never_doubled(desk, monkeypatch):
    """Killed AFTER the venue pass: the primary's ticket already FILLED. The
    re-approval reuses it rather than issuing a second basket."""
    c, ref = desk["c"], "b1r3"
    _propose(ref, _bull_call(40.0, 20.0))
    desk["book"]["chain"] = _call_book(85.0, 25.0)
    _clock(monkeypatch, datetime(2026, 10, 21, 10, 0, 0, tzinfo=IST))
    real_view = oms.ticket_view
    calls = {"n": 0}

    def view(conn, tid):                     # the process dies right after the sweep
        calls["n"] += 1
        raise _Crash("killed after the sweep")
    monkeypatch.setattr(oms, "ticket_view", view)
    with pytest.raises(_Crash):
        op.decide_pending(ref, approve=True, why="tap", human=True)
    monkeypatch.setattr(oms, "ticket_view", real_view)
    (first, st), = _tickets(c, ref, oms.PRIMARY_ACCOUNT)
    assert st == oms.FILLED
    _clock(monkeypatch, datetime(2026, 10, 21, 10, 5, 0, tzinfo=IST))
    out = op.decide_pending(ref, approve=True, why="tap again", human=True)
    assert out["status"] == "approved"
    assert _tickets(c, ref, oms.PRIMARY_ACCOUNT) == [(first, oms.FILLED)]      # reused, not doubled
    assert out["entry"]["execution"]["ticket_id"] == first


def test_withdraw_stale_entry_never_touches_exit_tickets_and_names_what_it_did(desk):
    c = desk["c"]
    assert oms.withdraw_stale_entry(c, LIVE, "nothing-here") == {"filled": None, "cancelled": {}}


def test_f14_a_venue_that_raises_before_the_live_ticket_refuses_the_live_arm(desk, monkeypatch):
    c, ref = desk["c"], "b1v1"
    _propose(ref, _bull_call(40.0, 20.0))
    desk["book"]["chain"] = _call_book(85.0, 25.0)

    def boom(conn, prop, **kw):
        raise RuntimeError("venue door down")
    monkeypatch.setattr(sr, "issue", boom)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    v = out["entry"]["accounts"][LIVE]
    assert out["entry"]["execution"]["error"] == "RuntimeError: venue door down"
    assert v["status"] == "rejected" and "the paper venue failed before the live arm's ticket" in v["reason"]
    assert pm._active_shadow_lock(c, LIVE, ref) is None and lp.position_state(c, LIVE, ref) is None


# --------------------------------------------------------------------- F16
def _expiry_world(tmp_path, name):
    c = brain_map.connect(str(tmp_path / f"{name}.db"))
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    pm.request_entry(c, name, 17550.0)
    for acct in (pm.ACCOUNT_PAPER_2L, LIVE, pm.ACCOUNT_PAPER_2L_ROT):
        pm.paper_request_entry(c, acct, name, 17550.0, lots=1, primary_lots=1)
    return c


def _active(c, ref):
    prim = c.execute("SELECT released_at FROM margin_locks WHERE journal_ref = ?", (ref,)).fetchone()[0]
    sh = dict(c.execute("SELECT account_id, released_at FROM paper_margin_locks WHERE journal_ref = ?",
                        (ref,)).fetchall())
    return prim, sh


@pytest.mark.parametrize("live_reader", ["has_open_position", "filled_entry_ticket"])
def test_f16_a_read_error_in_the_live_guard_expires_nothing(desk, monkeypatch, tmp_path, live_reader):
    """SQLite rolls the WHOLE transaction back on an I/O error, even for a
    SELECT: the guard used to swallow it and carry on, committing the
    shadows after it on a fresh transaction and reporting expiries that
    never happened. Now the error propagates: nothing is expired."""
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    c = _expiry_world(tmp_path, f"b1x_{live_reader}")
    try:
        real = getattr(lp, live_reader)

        def broken(conn, account, journal_ref):
            conn.rollback()                                   # what SQLite did under the I/O error
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(lp, live_reader, broken)
        if live_reader == "filled_entry_ticket":
            monkeypatch.setattr(lp, "has_open_position", lambda *a, **k: False)
        with pytest.raises(sqlite3.OperationalError):
            pm.expire_pending_lock(c, f"b1x_{live_reader}", why="test")
        prim, sh = _active(c, f"b1x_{live_reader}")
        assert prim is None and all(v is None for v in sh.values()), (prim, sh)     # NOTHING expired
        monkeypatch.setattr(lp, live_reader, real)
        monkeypatch.setattr(lp, "has_open_position", getattr(lp, "has_open_position"))
        out = pm.expire_pending_lock(c, f"b1x_{live_reader}", why="retry")         # the next run
        assert set(out) == {pm.ACCOUNT_PAPER_10L, pm.ACCOUNT_PAPER_2L, LIVE, pm.ACCOUNT_PAPER_2L_ROT}
    finally:
        c.close()

"""Chunk 2 lows — F06 residual (2026-10-08): the live arm's exit ownership
holds WITHOUT the single-instance tick lock.

The tick lock fails OPEN when it cannot be taken (a lock file this user cannot
open, no fcntl), so two ticks can run at once. The close-out verifier showed
three ways that still broke the F06 invariant — one PAPER_2L_LIVE position,
ONE filled exit — when an actor stalls inside its exit door:

  Q1  A is frozen between its `exiting` stamp and its ticket; B, a quote
      interval later, resumes the attempt as a crash, reopens and exits;
      A then issues and fills a SECOND basket;
  Q2  at normal cadence B's resume cancels A's still-working ticket — A's
      exit is lost for an interval;
  Q3  as Q1 but B's basket is cut between its legs; A fills a full basket and
      its FILLED-path settle closes the partial row B owns.

The fix: every exit ticket is attached to its attempt by a fence BEFORE the
venue may fill it (plan_tracker._execute_paper_exit's `fence`); `_exit`
settles only the attempt it stamped (`_settle(expect_attempt=)`); an
UNGUARDED resume leaves a young attempt (a ticket still working, or none
yet) untouched for one quote interval. The verifier's probes defeated the
lock with a real second OS process; here the lock is simply unavailable.
"""
import sqlite3

import pytest

from src import brain_map, oms, plan_tracker as pt, portfolio_manager as pm
from src.execution import live_pricer as lp, paper_venue as pv
from tests.test_live_exit_ownership_and_fills import (  # noqa: F401  (helpers only)
    _setup, _open, _row, _state, _exit_tickets, _events, _exit_qty_by_leg, _chain, OPEN, LIVE, PX)

# ratchet off -> static 65% take: long bid 200, short ask 42 -> mark 158, +88 of max 130
PT_CHAIN = _chain({(24000, "CE"): (200.0, 202.0, 201.0), (24200, "CE"): (40.0, 42.0, 41.0)})
POSITION_QTY = 65


@pytest.fixture
def pair(monkeypatch, tmp_path):
    """Two processes on one brain map, ticking UNGUARDED (the lock fails open)."""
    db = tmp_path / "bm.db"
    a = brain_map.connect(db)
    _setup(a, monkeypatch)
    b = brain_map.connect(db)
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    monkeypatch.setattr(lp, "_tick_lock", lambda path: (None, None))
    yield a, b
    lp.reset_cache()
    b.close()
    a.close()


def _tick(c, now, epoch):
    return lp.tick(now=now, conn=c, chain_fn=lambda t, x: PT_CHAIN, sleep_fn=lambda s: None,
                   now_epoch_fn=lambda: float(epoch), interval_s=300)


def _filled(c, ref):
    return [t for t, s in _exit_tickets(c, ref) if s == oms.FILLED]


def _freeze_a_at_its_ticket_write(monkeypatch, a, run_b):
    """A is frozen right after its `exiting` stamp, at its FIRST write after
    it (the EXIT ticket's oms.issue_ticket); B runs then."""
    real, seen = oms.issue_ticket, {}

    def issue(conn, ticket):
        if conn is a and str(ticket.get("note") or "").startswith("EXIT") and "b" not in seen:
            seen["a_state_at_freeze"] = _row(a)["state"]
            seen["b"] = run_b()
        return real(conn, ticket)
    monkeypatch.setattr(oms, "issue_ticket", issue)
    return seen


def test_q1_a_frozen_past_an_interval_never_fills_a_second_basket(pair, monkeypatch):
    a, b = pair
    ref = _open(a)
    seen = _freeze_a_at_its_ticket_write(
        monkeypatch, a, lambda: _tick(b, OPEN.replace(minute=10, second=1), 1301))
    ta = _tick(a, OPEN.replace(minute=5), 1000)
    tb = seen["b"]
    assert seen["a_state_at_freeze"] == "exiting"                  # A owned the exit when it froze
    assert [e["status"] for e in tb["exits"]] == ["settled"]       # B took over the crashed-looking attempt
    assert [e["status"] for e in ta["exits"]] == ["exit_not_owned"]
    assert "cancelled unfilled" in ta["exits"][0]["reason"]
    # THE F06 INVARIANT: one FILLED exit, each leg closed once
    assert len(_filled(a, ref)) == 1
    assert all(q == POSITION_QTY for q in _exit_qty_by_leg(a, ref).values())
    assert _events(a, lp.EVENT_EXIT, ref) == 1


def test_q2_an_unguarded_resume_never_cancels_a_young_working_ticket(pair, monkeypatch):
    """B's tick lands 60 s after A's stamp, between A's ticket and A's sweep:
    the attempt is young and its ticket is working, so B leaves it alone."""
    a, b = pair
    ref = _open(a)
    real_sweep, seen = pv.sweep, {}

    def sweep(conn, **kw):
        if conn is a and "b" not in seen and _exit_tickets(a, ref):
            seen["b"] = _tick(b, OPEN.replace(minute=6), 1060)
        return real_sweep(conn, **kw)
    monkeypatch.setattr(pv, "sweep", sweep)
    ta = _tick(a, OPEN.replace(minute=5), 1000)
    assert [e["status"] for e in ta["exits"]] == ["settled"]       # A's own exit was not lost
    assert not seen["b"].get("resumes")                            # untouched, not counted
    assert len(_filled(a, ref)) == 1


def test_q3_a_frozen_actor_never_settles_a_partial_row_another_owns(pair, monkeypatch):
    a, b = pair
    ref = _open(a)
    real_fill, n = oms.apply_fill, {"b": 0}

    def flaky(conn, leg_id, *args, **kw):                 # B's basket is cut between its legs
        if conn is b:
            n["b"] += 1
            if n["b"] == 2:
                raise sqlite3.OperationalError("database is locked")
        return real_fill(conn, leg_id, *args, **kw)
    monkeypatch.setattr(oms, "apply_fill", flaky)
    seen = _freeze_a_at_its_ticket_write(
        monkeypatch, a, lambda: _tick(b, OPEN.replace(minute=10, second=1), 1301))
    ta = _tick(a, OPEN.replace(minute=5), 1000)
    monkeypatch.setattr(oms, "apply_fill", real_fill)
    assert [e["status"] for e in seen["b"]["exits"]] == ["exit_error"]   # B's partial basket, row 'exiting'
    assert [e["status"] for e in ta["exits"]] == ["exit_not_owned"]       # A fenced: never 'settled'
    assert _state(a, ref)[0] == "exiting"
    pv.sweep(a, stamp=False)                              # any later venue pass (the primary's exits)
    assert all(q <= POSITION_QTY for q in _exit_qty_by_leg(a, ref).values())
    assert _events(a, lp.EVENT_EXIT, ref) == 0            # nothing booked on B's row by A


def test_with_the_lock_held_a_crash_is_still_resumed_at_once(monkeypatch, tmp_path):
    """The young-attempt rule is for the UNGUARDED tick only: holding the lock,
    a tick's resume cancels and reopens a 60-s-old crashed attempt as before."""
    c = brain_map.connect(tmp_path / "bm.db")
    _setup(c, monkeypatch)
    try:
        ref = _open(c)

        class _Raise:
            @staticmethod
            def sweep(conn, **kw):
                raise RuntimeError("venue down")
        first = lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_Raise)
        assert first["status"] == "exit_error" and _row(c)["state"] == "exiting"
        out = lp.tick(now=OPEN.replace(minute=1), conn=c, chain_fn=lambda t, x: PT_CHAIN,
                      sleep_fn=lambda s: None, now_epoch_fn=lambda: 1060.0, interval_s=300)
        assert [r["status"] for r in out["resumes"]] == ["reopened"]
        assert _state(c, ref)[0] == "open"
    finally:
        lp.reset_cache()
        c.close()


def test_an_unguarded_resume_rules(monkeypatch, tmp_path):
    c = brain_map.connect(tmp_path / "bm.db")
    _setup(c, monkeypatch)
    try:
        ref = _open(c)

        class _Raise:
            @staticmethod
            def sweep(conn, **kw):
                raise RuntimeError("venue down")
        lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_Raise)      # ticket issued, still working
        row = _row(c)
        young = lp._resume_exiting(c, row, OPEN.replace(minute=1), 300, guarded=False)
        assert young["status"] == "exit_in_flight" and young["young"] is True
        assert "still working" in young["reason"]
        assert (oms.ticket_view(c, row["exit_ticket_id"]) or {}).get("status") == oms.PENDING   # not cancelled
        # one interval later it is a crash: cancelled and reopened
        old = lp._resume_exiting(c, row, OPEN.replace(minute=5), 300, guarded=False)
        assert old["status"] == "reopened" and _state(c, ref)[0] == "open"
    finally:
        lp.reset_cache()
        c.close()


def test_a_young_attempt_whose_legs_all_filled_is_settled_at_any_age(monkeypatch, tmp_path):
    """Settling from fills is safe however young the attempt is: fills are facts."""
    c = brain_map.connect(tmp_path / "bm.db")
    _setup(c, monkeypatch)
    try:
        ref = _open(c)

        class _FillThenRaise:
            @staticmethod
            def sweep(conn, **kw):
                pv.sweep(conn, **kw)              # the basket fills...
                raise RuntimeError("door lost track of it")   # ...and the door errors after
        res = lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_FillThenRaise)
        assert _row(c)["state"] == "exiting", res
        got = lp._resume_exiting(c, _row(c), OPEN.replace(minute=1), 300, guarded=False)
        assert got["status"] == "settled" and _state(c, ref)[0] == "closed"
    finally:
        lp.reset_cache()
        c.close()


def test_settle_expect_attempt_refuses_another_attempt(monkeypatch, tmp_path):
    c = brain_map.connect(tmp_path / "bm.db")
    _setup(c, monkeypatch)
    try:
        ref = _open(c)
        c.execute("UPDATE paper_live_positions SET state = 'exiting', exit_started_at = ? WHERE journal_ref = ?",
                  ("2026-09-29T11:00:00", ref))
        c.commit()
        res = lp._settle(c, _row(c), 150.0, "ratchet_hit", "live_bid_ask", 0.0, OPEN,
                         expect_state=lp.STATE_EXITING, expect_attempt="2026-09-29T10:55:00")
        assert res["status"] == "state_moved" and "attempt stamped 2026-09-29T10:55:00" in res["reason"]
        assert _state(c, ref)[0] == "exiting"
        ok = lp._settle(c, _row(c), 150.0, "ratchet_hit", "live_bid_ask", 0.0, OPEN,
                        expect_state=lp.STATE_EXITING, expect_attempt="2026-09-29T11:00:00")
        assert ok["status"] == "settled"
    finally:
        lp.reset_cache()
        c.close()


def test_a_failed_fence_cancels_every_issued_ticket_and_never_sweeps(monkeypatch, tmp_path):
    c = brain_map.connect(tmp_path / "bm.db")
    _setup(c, monkeypatch)
    try:
        ref = _open(c)
        row = _row(c)
        swept = []

        class _Venue:
            @staticmethod
            def sweep(conn, **kw):
                swept.append(1)
        rec = pt._execute_paper_exit(lp._entry_like(row), {k: max(0.05, v) for k, v in PX.items()},
                                     "ratchet_hit", conn=c, venue_mod=_Venue, today=OPEN.date(),
                                     accounts=[(LIVE, 1)], fence=lambda tids: False)
        assert rec["fenced"] is True and swept == []
        assert (oms.ticket_view(c, rec["ticket_id"]) or {}).get("status") == oms.CANCELLED
        assert _state(c, ref)[0] == "open"                 # the row was never this call's to move
    finally:
        lp.reset_cache()
        c.close()


def test_a_completion_whose_claim_was_taken_over_is_fenced_and_fills_nothing(monkeypatch, tmp_path):
    """The completion's own fence: another actor moves the partly filled row
    after this completion's claim and before its ticket can fill — the
    completion ticket is cancelled unswept, the open leg is not sold."""
    from tests.test_live_exit_ownership_and_fills import _fail_second_leg_fill, _tick as _tk, ENTRY, LATER
    c = brain_map.connect(tmp_path / "bm.db")
    _setup(c, monkeypatch)
    try:
        ref = _open(c)
        assert _tk(c, 0, ENTRY, 1000)["marked"] == 1
        real_fill = _fail_second_leg_fill(monkeypatch)
        lp._exit(c, _row(c), PX, "profit_take", OPEN.replace(minute=1))   # cut: the short closed, the long not
        monkeypatch.setattr(oms, "apply_fill", real_fill)
        _tk(c, 2, LATER, 1060)                                            # resume: partial, row exiting
        real_issue = oms.issue_ticket

        def issue(conn, ticket):
            out = real_issue(conn, ticket)
            # another claimant lands between this completion's claim and its fence
            conn.execute("UPDATE paper_live_positions SET last_exit_attempt_ts = '2026-09-29T11:06:30' "
                         "WHERE journal_ref = ?", (ref,))
            conn.commit()
            return out
        monkeypatch.setattr(oms, "issue_ticket", issue)
        t = _tk(c, 6, LATER, 1400)
        (ex,) = t["exits"]
        assert ex["status"] == "exit_not_owned" and "cancelled unfilled" in ex["reason"]
        tickets = _exit_tickets(c, ref)
        assert len(tickets) == 2 and tickets[-1][1] == oms.CANCELLED
        assert all(q <= POSITION_QTY for q in _exit_qty_by_leg(c, ref).values())
        assert _state(c, ref)[0] == "exiting" and _events(c, lp.EVENT_EXIT, ref) == 0
    finally:
        lp.reset_cache()
        c.close()


def test_exits_filled_path_settles_only_its_own_attempt(monkeypatch, tmp_path):
    """Defence in depth behind the fence: if the row is re-stamped by another
    attempt after this one's ticket filled, `_exit` books nothing (the row's
    own resume books every ticket's fills together)."""
    c = brain_map.connect(tmp_path / "bm.db")
    _setup(c, monkeypatch)
    try:
        ref = _open(c)

        class _FillThenRestamp:
            @staticmethod
            def sweep(conn, **kw):
                pv.sweep(conn, **kw)
                conn.execute("UPDATE paper_live_positions SET exit_started_at = '2026-09-29T11:00:30' "
                             "WHERE journal_ref = ?", (ref,))
                conn.commit()
        res = lp._exit(c, _row(c), PX, "ratchet_hit", OPEN, venue_mod=_FillThenRestamp)
        assert res["status"] == "state_moved" and "attempt stamped" in res["reason"]
        assert _state(c, ref)[0] == "exiting" and _events(c, lp.EVENT_EXIT, ref) == 0
    finally:
        lp.reset_cache()
        c.close()

"""Chunk 2 lows — residuals B2 (2026-10-08): what the close-out's
cross-batch critic and per-finding verifiers found across the batches.

  (a) one pending ref whose margin expiry raises no longer stops every later
      ref of the D7 sweep (plan_tracker.expire_pending_margin);
  (b) a LIVE position that already CLOSED, lock settled, is not 'already
      open': a refusal over it leaves its verdict as it is;
  (c) cards fired from inside decide_pending's journal lock are sent only
      after the lock is released (notifier.deferred_broadcasts, #122);
  (d) F10: a READABLE live book is the dashboard's truth for the live arm;
      only an unreadable one falls back to the journal verdict, named;
  (e) F13: the rotation account's held-lock halt check, pinned.
Hermetic: the `desk` fixture of test_live_entry_rules_l3."""
import asyncio
from datetime import datetime

import pytest

from src import journal, notifier, oms, plan_tracker as pt, portfolio_manager as pm
from src import options_proposer as op
from src.dashboard import data as dd
from src.execution import live_pricer as lp
from tests.test_live_entry_rules_l3 import desk, _propose, _bull_call, _tickets, _events, LIVE  # noqa: F401

REAL_FIRE = notifier.fire_broadcast
ROT = pm.ACCOUNT_PAPER_2L_ROT


# --------------------------------------------------------------------- (a)
def test_a_the_d7_sweep_isolates_a_ref_whose_expiry_fails(desk, monkeypatch, capsys):
    c = desk["c"]
    for ref in ("b2d7a", "b2d7b"):
        _propose(ref, _bull_call())
    real = pm.expire_pending_lock

    def flaky(conn, journal_ref, why=""):
        if journal_ref == "b2d7a":
            raise RuntimeError("disk I/O error")
        return real(conn, journal_ref, why=why)
    monkeypatch.setattr(pm, "expire_pending_lock", flaky)
    late = datetime(2026, 10, 21, 15, 45, tzinfo=pm.IST)
    out = pt.expire_pending_margin(now=late, conn=c)
    printed = capsys.readouterr().out
    assert "b2d7a" not in out and "b2d7b" in out                 # the failing ref did not stop the next
    assert "b2d7a" in printed and "margin expiry FAILED, nothing expired for it" in printed
    assert c.execute("SELECT COUNT(*) FROM paper_margin_locks WHERE journal_ref = 'b2d7a' AND "
                     "released_at IS NULL").fetchone()[0] > 0


# --------------------------------------------------------------------- (b)
def test_b_a_closed_live_position_is_never_relabelled_already_open(desk):
    c, ref = desk["c"], "b2cl1"
    _propose(ref, _bull_call())
    assert op.decide_pending(ref, approve=True, why="tap", human=True)["status"] == "approved"
    live = [r for r in lp.open_rows(c, LIVE) if r["journal_ref"] == ref][0]
    s = lp._settle(c, live, 100.0, "profit_take", "live_bid_ask", 0.0, datetime(2026, 10, 21, 11, 0))
    assert s["status"] == "settled" and pm._active_shadow_lock(c, LIVE, ref) is None
    rel = pm.release_unopened_lock(c, LIVE, ref)
    assert rel.get("closed_position") is True and "backs_position" not in rel
    entry = journal.get_entry(ref)
    before = dict(entry["accounts"][LIVE])
    op._live_refuse(c, LIVE, entry, "the live arm is switched off")
    assert entry["accounts"][LIVE] == before                     # neither 'already_open' nor 'rejected'
    journal.update_entry(ref, lambda e: e.update(accounts=entry["accounts"]) or True)
    rows = dd.open_trades(snapshot_marks={}, now=datetime(2026, 10, 21, 12, 5))
    (primary,) = [r for r in rows if r.get("id") == ref]
    assert LIVE not in primary["accounts"] and "note" not in primary


# --------------------------------------------------------------------- (c)
def test_c_deferred_broadcasts_queue_until_the_outermost_block_exits(monkeypatch):
    sent = []

    async def rec(payload):
        sent.append(payload["event"])
    monkeypatch.setattr(notifier, "broadcast_alert", rec)
    with notifier.deferred_broadcasts():
        REAL_FIRE({"event": "one"})
        with notifier.deferred_broadcasts():
            REAL_FIRE({"event": "two"})
        assert sent == []                                        # the inner block does not send
    assert sent == ["one", "two"]
    REAL_FIRE({"event": "three"})                                # outside any block: sent at once
    assert sent == ["one", "two", "three"]


def test_c_an_approval_halt_card_is_sent_only_after_the_journal_lock(desk, monkeypatch):
    """The F13 held-lock halt card fires from pm.request_entry INSIDE
    decide_pending's journal lock; it now leaves only once the lock is free."""
    c, ref = desk["c"], "b2card1"
    _propose(ref, _bull_call())
    pm.latch_halt(c, "test latch")
    depth_at_send = []

    async def rec(payload):
        depth_at_send.append((payload.get("event"), getattr(journal._tls, "depth", 0)))
    monkeypatch.setattr(notifier, "fire_broadcast", REAL_FIRE)
    monkeypatch.setattr(notifier, "broadcast_alert", rec)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == op.MARGIN_BLOCKED
    assert depth_at_send and all(d == 0 for _, d in depth_at_send), depth_at_send


# --------------------------------------------------------------------- (d)
def test_d_a_readable_live_book_without_a_row_never_lists_the_live_arm(desk):
    """The arm's verdict reads 'approved' on the journal but its book (read
    fine) has no open row for the ref — e.g. the row closed and the journal
    stamp was missed: the primary's row must not name it on the primary's
    figures."""
    c, ref = desk["c"], "b2dash1"
    _propose(ref, _bull_call())
    assert op.decide_pending(ref, approve=True, why="tap", human=True)["status"] == "approved"
    c.execute("UPDATE paper_live_positions SET state = 'closed', pnl_net = 0 WHERE journal_ref = ?", (ref,))
    c.commit()
    assert journal.get_entry(ref)["accounts"][LIVE]["status"] in dd.HOLDING_VERDICTS
    rows = dd.open_trades(snapshot_marks={}, now=datetime(2026, 10, 21, 12, 5))
    (primary,) = [r for r in rows if r.get("id") == ref]
    assert LIVE not in primary["accounts"] and not [r for r in rows if r.get("account") == LIVE]


def test_d_an_unreadable_live_book_names_the_fallback_on_the_row(desk):
    c, ref = desk["c"], "b2dash2"
    _propose(ref, _bull_call())
    assert op.decide_pending(ref, approve=True, why="tap", human=True)["status"] == "approved"
    rows = dd.open_trades(snapshot_marks={}, live={"error": "database is locked"},
                          now=datetime(2026, 10, 21, 12, 5))
    (primary,) = [r for r in rows if r.get("id") == ref]
    assert LIVE in primary["accounts"]
    assert primary["note"].startswith("live book unreadable — PAPER_2L_LIVE named from the journal verdict")


def test_d_a_closed_live_row_is_in_neither_the_table_nor_the_unrealized(desk):
    c, ref = desk["c"], "b2dash3"
    _propose(ref, _bull_call())
    assert op.decide_pending(ref, approve=True, why="tap", human=True)["status"] == "approved"
    c.execute("UPDATE paper_live_positions SET state = 'closed', last_profit_ps = 50.0 WHERE journal_ref = ?",
              (ref,))
    c.commit()
    assert [r["journal_ref"] for r in dd._live_positions(c) if r["journal_ref"] == ref] == []


# --------------------------------------------------------------------- (e)
def test_e_a_halted_rotation_shadow_is_refused_at_approval_and_its_lock_released(desk, monkeypatch):
    c, ref = desk["c"], "b2rot1"
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    _propose(ref, _bull_call())
    assert pm._active_shadow_lock(c, ROT, ref) is not None
    pm.paper_log_event(c, ROT, pm.HALT_LATCH_EVENT, None, "drawdown 10.40% >= 10% (test latch)")
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == "approved"
    v = out["entry"]["accounts"][ROT]
    assert v["status"] == "rejected" and "(audit F13); lock released at zero" in v["reason"]
    assert pm._active_shadow_lock(c, ROT, ref) is None and _tickets(c, ref, ROT) == 0

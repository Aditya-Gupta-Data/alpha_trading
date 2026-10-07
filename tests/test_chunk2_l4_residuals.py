"""
The L3 verifier's residuals (2026-10-06, folded into batch L4).

(a) A shadow account switched OFF between proposal and approval — the
    rotation arm (capital_rotation_enabled) or the whole 2L experiment
    (paper_2l_account_enabled) — was left out of the approval judgement:
    its verdict dropped from the row, no ticket, its proposal-time lock
    held, and the primary's exit then settled that lock at the SCALED
    primary P&L (a trade it never took). Now it is refused by name at
    approval (verdict kept as refused, lock released at zero), and
    release_shadow_locks never books P&L for an account whose verdict on
    the row is not 'approved' — at every tracker settlement site.
(b) The live arm refused over a recorded/FILLED position (switch or venue
    off) read 'rejected' with 0 lots while its lock was rightly kept; it
    now reads 'already_open' with the lock's lots and margin.
(c) (tests/test_chunk2_l4_lows.py: the dashboard holds 'already_open'.)
(d) Two surviving mutants of L3's F14 switch-off fix pinned: an entry with
    no margin field still releases the LIVE lock; only an APPROVED
    proposal-time verdict is refused.

Hermetic: the per-test FILE brain_map of tests/test_live_entry_rules_l3's
`desk` (or ':memory:' / tmp), tmp journal (conftest), injected chains.
"""
import json
from datetime import date

import pytest

from src import brain_map, journal, oms, plan_tracker as pt, portfolio_manager as pm
from src import options_proposer as op
from src.execution import live_pricer as lp
from tests.test_live_entry_rules_l3 import (  # noqa: F401  (the desk fixture)
    AT, LIVE, TWO_L, _bull_call, _events, _filled_live_ticket, _propose, _tickets, desk)

ROT = pm.ACCOUNT_PAPER_2L_ROT


def _realized(c, acct):
    return float(c.execute("SELECT realized_pnl FROM paper_accounts WHERE account_id = ?", (acct,)).fetchone()[0])


def _lock(c, acct, ref):
    r = c.execute("SELECT margin_rs, lots, released_at, pnl_net FROM paper_margin_locks WHERE account_id = ? "
                  "AND journal_ref = ?", (acct, ref)).fetchone()
    return None if r is None else tuple(r)


# =================================================================== (a) the switch off at approval

def test_rotation_switched_off_at_approval_is_refused_by_name_and_books_nothing(desk, monkeypatch):
    """repro scratch/L3m test_rotation_switch_off_at_approval_strands_rot_lock_and_books_pnl."""
    c, ref = desk["c"], "l4rot001"
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    pend = _propose(ref, _bull_call())
    assert pend["accounts"][ROT]["status"] == "approved" and pm._active_shadow_lock(c, ROT, ref) is not None
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == "approved"
    v = out["entry"]["accounts"][ROT]
    assert (v["status"], v["lots"], v["margin_rs"], v["ticket_id"], v["proposed_lots"]) == ("rejected", 0, None,
                                                                                            None, 1)
    assert v["reason"] == ("refused at approval: PAPER_2L_ROT is switched off since the proposal "
                           "(capital_rotation_enabled false) — it takes no position, so it books none of this "
                           "trade's P&L (L3 residual (a))")
    assert journal.get_entry(ref)["accounts"][ROT]["status"] == "rejected"     # kept on the row, not dropped
    assert _tickets(c, ref, ROT) == 0 and pm._active_shadow_lock(c, ROT, ref) is None
    (ev,) = _events(c, ROT, op.EVENT_SWITCHED_OFF, ref)
    assert ev.endswith("(L3 residual (a)) — lock released at zero")
    # the other arms are untouched by the rotation switch
    assert out["entry"]["accounts"][TWO_L]["status"] == "approved" and _tickets(c, ref, TWO_L) == 1
    assert out["entry"]["accounts"][LIVE]["status"] == "approved"
    # the primary exits: ROT books nothing, 2L its scaled share
    res = pm.release_entry(ref, 3000.0, conn=c, verdicts=journal.get_entry(ref)["accounts"])
    assert ROT not in res.get("shadow_accounts", {}) and _realized(c, ROT) == 0.0
    assert res["shadow_accounts"][TWO_L]["released"] is True and _realized(c, TWO_L) != 0.0


def test_the_whole_experiment_switched_off_refuses_every_shadow_by_name(desk, monkeypatch):
    c, ref = desk["c"], "l4all001"
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    _propose(ref, _bull_call())
    assert all(pm._active_shadow_lock(c, a, ref) is not None for a in (TWO_L, ROT, LIVE))
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", False)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    acc = out["entry"]["accounts"]
    assert set(acc) == {TWO_L, ROT, LIVE} and {v["status"] for v in acc.values()} == {"rejected"}
    assert "(paper_2l_account_enabled false)" in acc[TWO_L]["reason"]
    assert "(paper_2l_account_enabled false)" in acc[ROT]["reason"]
    assert "(paper_2l_account_enabled false)" in acc[LIVE]["reason"]
    for a in (TWO_L, ROT, LIVE):
        assert pm._active_shadow_lock(c, a, ref) is None and _tickets(c, ref, a) == 0
    assert _tickets(c, ref, oms.PRIMARY_ACCOUNT) == 1
    pm.release_entry(ref, 5000.0, conn=c, verdicts=journal.get_entry(ref)["accounts"])
    assert _realized(c, TWO_L) == _realized(c, ROT) == _realized(c, LIVE) == 0.0


def test_a_shadow_switched_on_at_approval_is_unaffected(desk):
    c, ref = desk["c"], "l4on0001"
    _propose(ref, _bull_call())
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["entry"]["accounts"][TWO_L]["status"] == "approved"
    assert _events(c, TWO_L, op.EVENT_SWITCHED_OFF) == []


def test_a_failed_refusal_still_books_nothing_the_settlement_guards_it(desk, monkeypatch):
    """Belt and braces: with the approval-time refusal broken (fail-open),
    the dropped verdict still keeps the primary's P&L off the ROT lock — at
    the tracker's own EOD settlement (plan_tracker._settle_spread_row)."""
    c, ref = desk["c"], "l4grd001"
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    _propose(ref, _bull_call())
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    monkeypatch.setattr(op, "_switched_off_refusals", lambda entry, proposed, conn=None: None)
    op.decide_pending(ref, approve=True, why="tap", human=True)
    row = journal.get_entry(ref)
    assert ROT not in row["accounts"] and pm._active_shadow_lock(c, ROT, ref) is not None   # the old stranding
    monkeypatch.setattr(pt, "journal", journal)
    monkeypatch.setattr(pt, "_settle_spread_cash", lambda pnl: True)
    seen = {}
    bars = [("2026-10-22", 24300.0, 24500.0, 24450.0), ("2026-10-26", 24300.0, 24500.0, 24450.0)]
    journal.update_matching(lambda e: journal.row_key(e) == ref, lambda e: pt._settle_spread_row(e, bars, seen))
    assert seen["status"] == "resolved"
    assert _lock(c, ROT, ref)[3] == 0.0 and _realized(c, ROT) == 0.0
    (ev,) = _events(c, ROT, pm.EVENT_LOCK_NOT_TAKEN, ref)
    assert "verdict on the entry is 'missing'" in ev
    assert _lock(c, TWO_L, ref)[3] != 0.0                                     # the approved 2L: its share


@pytest.fixture
def mem():
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    yield c
    c.close()


@pytest.mark.parametrize("verdicts, expect_zero", [
    (None, False),                                                     # no row given: settles as before
    ({TWO_L: {"status": "approved", "lots": 1}}, False),
    ({TWO_L: {"status": "rejected", "lots": 0}}, True),
    ({TWO_L: {"status": "error"}}, True),
    ({}, True),                                                        # the verdict is missing
])
def test_release_shadow_locks_books_pnl_only_for_an_approved_verdict(mem, monkeypatch, verdicts, expect_zero):
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    c = mem
    assert pm.request_entry(c, "g0001", 1000.0)["approved"]
    pm.paper_request_entry(c, TWO_L, "g0001", 1000.0, lots=1, primary_lots=2)
    out = pm.release_shadow_locks(c, "g0001", 4000.0, 0.0, verdicts=verdicts)
    assert out[TWO_L]["released"] is True
    pnl = _lock(c, TWO_L, "g0001")[3]
    assert (pnl == 0.0) is expect_zero and (pnl == 2000.0) is (not expect_zero)
    assert len(_events(c, TWO_L, pm.EVENT_LOCK_NOT_TAKEN, "g0001")) == (1 if expect_zero else 0)


def test_reconcile_settles_a_not_taken_shadow_lock_at_zero(mem, monkeypatch):
    """The orphan-lock retry is a settlement site too."""
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pt, "journal", journal)
    from datetime import datetime
    c = mem
    for ref, status in (("rc000001", "rejected"), ("rc000002", "approved")):
        journal.log({"short_id": ref, "date": "2026-10-01", "ticker": "NIFTY 50", "decision": "approved",
                     "spread": {"strategy": "iron_condor", "legs": [], "lot_size": 75, "lots": 2},
                     "accounts": {TWO_L: {"status": status, "lots": 1}},
                     "outcome": {"resolution": "profit_take", "pnl_rs": 5000.0, "frictions_rs": 0.0,
                                 "hypothetical": False}})
        assert pm.request_entry(c, ref, 1000.0)["approved"]
        pm.paper_request_entry(c, TWO_L, ref, 1000.0, lots=1, primary_lots=2)
    pt.reconcile_orphan_locks(now=datetime(2026, 10, 2, 12, 0, tzinfo=pm.IST), conn=c)
    assert _lock(c, TWO_L, "rc000001")[3] == 0.0 and _lock(c, TWO_L, "rc000002")[3] != 0.0
    assert len(_events(c, TWO_L, pm.EVENT_LOCK_NOT_TAKEN, "rc000001")) == 1


def test_the_intraday_square_off_settles_a_not_taken_shadow_lock_at_zero(tmp_path, monkeypatch):
    """The third tracker settlement site (#69 square-off, real quotes)."""
    from tests.test_intraday_exit import WIN_QUOTES, _sandbox, _spread_entry
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    monkeypatch.setattr(pt, "journal", journal)
    e = dict(_spread_entry(), accounts={TWO_L: {"status": "rejected", "lots": 0}})
    _sandbox(tmp_path, monkeypatch, [e])
    c = brain_map.connect(str(tmp_path / "brain.db"))
    pm.ensure_accounts_schema(c)
    pm.paper_request_entry(c, TWO_L, "iday0001", 1000.0, lots=1, primary_lots=1)
    c.close()
    out = pt.resolve_intraday_profit_take("iday0001", WIN_QUOTES, model_capture_pct=83.0, today=date(2026, 7, 14))
    assert out["status"] == "squared_off" and out["pnl_rs"] > 0
    c = brain_map.connect(str(tmp_path / "brain.db"))
    try:
        assert _lock(c, TWO_L, "iday0001")[3] == 0.0 and _realized(c, TWO_L) == 0.0
        assert len(_events(c, TWO_L, pm.EVENT_LOCK_NOT_TAKEN, "iday0001")) == 1
    finally:
        c.close()


# =================================================================== (b) a kept lock is an open position

@pytest.mark.parametrize("off", ["switch", "venue"])
def test_a_refusal_over_a_recorded_live_position_reads_already_open_with_the_locks_size(desk, monkeypatch, off):
    c, ref = desk["c"], f"l4b{off[:4]}1"
    _propose(ref, _bull_call())
    held = pm._active_shadow_lock(c, LIVE, ref)
    _, view = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    lp.open_position(c, LIVE, {"short_id": ref, "ticker": "NIFTY 50", "spread": _bull_call()}, view, now=AT)
    if off == "switch":
        monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    else:
        monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    v = out["entry"]["accounts"][LIVE]
    assert v["status"] == op.LIVE_ALREADY_OPEN and (v["margin_rs"], v["lots"]) == held
    assert v["reason"].startswith("no second live entry: ") and \
        v["reason"].endswith("a live position for this entry is recorded (open) — the lock backs it")
    assert journal.get_entry(ref)["accounts"][LIVE]["status"] == op.LIVE_ALREADY_OPEN
    assert pm._active_shadow_lock(c, LIVE, ref) == held                        # kept, as before
    (ev,) = _events(c, LIVE, "live_entry_refused", ref)
    assert "lock kept (a live position for this entry is recorded (open)" in ev


def test_a_refusal_with_no_position_still_reads_rejected(desk, monkeypatch):
    c, ref = desk["c"], "l4bnone1"
    _propose(ref, _bull_call())
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert (v["status"], v["lots"], v["margin_rs"]) == ("rejected", 0, None)


# =================================================================== (d) L3's surviving mutants

def test_switch_off_releases_the_live_lock_of_an_entry_with_no_margin_field(desk, monkeypatch):
    """Surviving mutant 1: the switch-off refusal must not sit inside the
    margin branch — an entry without `spread.margin` skips the judgement."""
    c, ref = desk["c"], "l4dmrg01"
    _propose(ref, _bull_call())

    def strip(e):
        e["spread"] = dict(e["spread"], margin=None)
    journal.update_entry(ref, strip)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == "approved" and out["entry"]["accounts"][LIVE]["status"] == "rejected"
    assert pm._active_shadow_lock(c, LIVE, ref) is None and _tickets(c, ref, LIVE) == 0
    assert len(_events(c, LIVE, "live_entry_refused", ref)) == 1


def test_switch_off_refuses_only_an_approved_proposal_time_verdict(desk, monkeypatch):
    """Surviving mutant 2: the `status == 'approved'` filter. A LIVE verdict
    REJECTED at proposal (a leg priced off last-price) holds no lock and is
    not refused a second time when the switch goes off."""
    c, ref = desk["c"], "l4dflt01"
    s = _bull_call()
    s["legs"][1]["fill_basis"] = "last_price"
    pend = _propose(ref, s)
    assert pend["accounts"][LIVE]["status"] == "rejected" and pm._active_shadow_lock(c, LIVE, ref) is None
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == "approved"
    assert _events(c, LIVE, "live_entry_refused", ref) == []
    assert LIVE not in out["entry"]["accounts"]
    assert out["entry"]["accounts"][TWO_L]["status"] == "approved"


def test_release_unopened_lock_marks_a_lock_that_backs_a_position(desk):
    c, ref = desk["c"], "l4bmark1"
    _propose(ref, _bull_call())
    assert pm.release_unopened_lock(c, LIVE, "nolock01") == {"released": False,
                                                            "reason": "no active lock for this ref"}
    _, view = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    rel = pm.release_unopened_lock(c, LIVE, ref)                       # FILLED, no row yet (F15)
    assert rel["released"] is False and rel["backs_position"] is True
    lp.open_position(c, LIVE, {"short_id": ref, "ticker": "NIFTY 50", "spread": _bull_call()}, view, now=AT)
    rel = pm.release_unopened_lock(c, LIVE, ref)
    assert rel["released"] is False and rel["backs_position"] is True
    assert json.dumps(rel)                                               # plain data on the result

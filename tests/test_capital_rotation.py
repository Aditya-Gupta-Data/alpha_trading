"""
CAPITAL ROTATION — the eviction protocol (decision #115, 2026-09-25).

A third paper account, PAPER_2L_ROT, identical to PAPER_2L except that when it
cannot margin a new signal it may evict its weakest open trade (lowest
REMAINING reward:risk) if the new trade's reward:risk is >= 1.5x that.
PAPER_10L and PAPER_2L stay first-come-first-served. Hermetic: sqlite
':memory:', journal / quotes / venue slippage injected.
"""
import json
import sqlite3

import pytest

from src import brain_map, oms, plan_tracker as pt, portfolio_manager as pm
from src.dashboard import data as dash
from src.execution import paper_venue as pv
from src.strategy import StrategyConstructor

ROT, TWO_L = pm.ACCOUNT_PAPER_2L_ROT, pm.ACCOUNT_PAPER_2L


def _spread():
    # bull call 24000/24200 @ 100/30: debit 70, max loss 4,550, max profit
    # 8,450 per 65-lot, reward:risk 1.857, SPAN Rs.17,550 per lot
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(
        24000, 24200, 100.0, 30.0)
    s.update(lots=1, expiry="2026-10-28", entry_spot=24000.0)
    return s


def _entry(ref):
    return {"short_id": ref, "date": "2026-09-20", "ticker": "NIFTY 50", "action": "SPREAD",
            "decision": "approved", "outcome": None, "spread": _spread(),
            "accounts": {TWO_L: {"status": "approved", "lots": 1, "ticket_id": f"t2l-{ref}"},
                         ROT: {"status": "approved", "lots": 1, "ticket_id": f"trot-{ref}"}}}


# old1 is deep in profit (little left to earn, a lot to give back) -> weakest
QUOTES = {"old1": {(24000.0, "CE"): 250.0, (24200.0, "CE"): 60.0},    # +120 of 130 -> rr_left 10/190
          "old2": {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}}    # flat -> rr_left 130/70


class FakeJournal:
    def __init__(self, rows):
        self.rows = rows

    def read_all(self):
        return self.rows

    def update_entry(self, sid, fn):
        e = next((r for r in self.rows if r["short_id"] == sid), None)
        return e if e is not None and fn(e) is not False else None


@pytest.fixture
def world(monkeypatch):
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.001)
    j = FakeJournal([_entry("old1"), _entry("old2")])
    monkeypatch.setattr(pt, "journal", j)
    # every account holds old1 + old2 and is FULL: Rs.5,000 liquid < Rs.17,550/lot
    for acct in (TWO_L, ROT):
        assert pm.paper_request_entry(c, acct, "old1", 100000.0, lots=1, primary_lots=1)["approved"]
        assert pm.paper_request_entry(c, acct, "old2", 95000.0, lots=1, primary_lots=1)["approved"]
    assert pm.request_entry(c, "old1", 17550.0)["approved"]
    assert pm.request_entry(c, "old2", 17550.0)["approved"]
    yield c, j
    c.close()


def _evict_fn(j):
    def fn(conn, account, ref, lots, max_rr_left, reason):
        return pt.evict_for_rotation(conn, account, ref, lots, max_rr_left=max_rr_left,
                                     reason=reason, quotes_fn=lambda e: QUOTES[e["short_id"]],
                                     entries=j.rows)
    return fn


def _marks(refs):
    return {"old1": 10 / 190, "old2": 130 / 70}


def _new_proposal():
    return {"ticker": "NIFTY 50", "short_id": "new1", "spread": _spread(), "lots": 1, "vix": 13.0}


# ------------------------------------------------------------- the math

def test_reward_risk_left_is_what_the_trade_can_still_make_over_still_lose():
    s = _spread()                                   # per share: max profit 130, max loss 70
    assert pm.proposal_reward_risk(s) == pytest.approx(130 / 70)
    assert pm.reward_risk_left(s, 120.0) == pytest.approx(10 / 190)
    assert pm.reward_risk_left(s, 130.0) == 0.0     # nothing left to earn
    assert pm.reward_risk_left(s, -70.0) == float("inf")   # nothing left to lose: never evicted
    assert pm.reward_risk_left(s, 500.0) == 0.0     # clamped to the structure
    assert pm.reward_risk_left(dict(s, max_loss=0), 0.0) is None


def test_evaluate_eviction_picks_the_lowest_rr_left_and_applies_the_1_5x_rule(world):
    conn, _ = world
    v = pm.evaluate_eviction(conn, ROT, _spread(), _marks(None), need_rs=17550.0)
    assert v["evict"] and v["weakest"]["journal_ref"] == "old1"
    assert v["new_rr"] == pytest.approx(1.8571, abs=1e-4)
    # weakest at rr_left 1.3: 1.5 x 1.3 = 1.95 > 1.857 -> held
    v = pm.evaluate_eviction(conn, ROT, _spread(), {"old1": 1.3, "old2": 2.0}, need_rs=17550.0)
    assert not v["evict"] and "< 1.5x" in v["reason"] and v["weakest"]["journal_ref"] == "old1"
    # exactly 1.5x passes (">= 1.5x")
    v = pm.evaluate_eviction(conn, ROT, _spread(), {"old1": (130 / 70) / 1.5}, need_rs=17550.0)
    assert v["evict"]
    # an unmarked trade is never a candidate
    v = pm.evaluate_eviction(conn, ROT, _spread(), {}, need_rs=17550.0)
    assert not v["evict"] and "no open trade has a current mark" in v["reason"]
    # the freed margin must actually fit one lot
    v = pm.evaluate_eviction(conn, ROT, _spread(), {"old2": 0.1}, need_rs=200000.0)
    assert not v["evict"] and "frees" in v["reason"]


def test_the_fcfs_accounts_can_never_evict(world):
    conn, _ = world
    for acct in (TWO_L, pm.ACCOUNT_PAPER_10L):
        v = pm.evaluate_eviction(conn, acct, _spread(), _marks(None), need_rs=17550.0)
        assert not v["evict"] and "first-come-first-served" in v["reason"]
    assert pt.evict_for_rotation(conn, TWO_L, "old1", 1, quotes_fn=lambda e: QUOTES["old1"],
                                 entries=[_entry("old1")])["status"] == "not_rotation_account"


# ------------------------------------------------------------- the A/B, end to end

def test_10l_rejects_on_zero_margin_and_nothing_is_evicted(world):
    conn, _ = world
    pm.request_entry(conn, "filler", pm.available_cash(conn) - 1000.0)      # 10L now Rs.1,000 liquid
    v = pm.request_entry(conn, "new1", 17550.0)
    assert not v["approved"] and "margin exhaustion" in v["reason"]
    assert conn.execute("SELECT COUNT(*) FROM margin_locks WHERE released_at IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_account_events WHERE event_type = ?",
                        (pm.ROTATION_EVICTION_EVENT,)).fetchone()[0] == 0


def test_2l_rejects_while_2l_rot_evicts_the_weak_trade_and_funds_the_strong_one(world):
    conn, j = world
    before_10l = pm.account_summary(conn)
    out = pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                      marks_fn=_marks, evict_fn=_evict_fn(j))
    # PAPER_2L: first-come-first-served -> refused, both old trades still held
    assert out[TWO_L]["status"] == "rejected" and "rotation" not in out[TWO_L]
    assert pm._active_shadow_lock(conn, TWO_L, "old1") is not None
    assert pm._active_shadow_lock(conn, TWO_L, "old2") is not None
    # PAPER_2L_ROT: old1 evicted, new1 funded at 1 lot
    assert out[ROT]["status"] == "approved" and out[ROT]["lots"] == 1
    assert out[ROT]["rotation"]["evicted"] == "old1"
    assert pm._active_shadow_lock(conn, ROT, "old1") is None
    assert pm._active_shadow_lock(conn, ROT, "old2") is not None
    assert pm._active_shadow_lock(conn, ROT, "new1") == (17550.0, 1)
    # the 10L ledger never moved
    assert pm.account_summary(conn) == before_10l
    assert conn.execute("SELECT COUNT(*) FROM margin_locks WHERE released_at IS NULL").fetchone()[0] == 2
    # ONE exit ticket, for the rotation account alone
    tix = conn.execute("SELECT account_id, kind, lots, journal_ref FROM trade_tickets").fetchall()
    assert [tuple(t) for t in tix] == [(ROT, oms.EXIT, 1, "old1")]
    # P&L on the real quotes: +120/share x 65, minus frictions and the venue's slippage
    frictions, slippage = pt._spread_exit_costs_quoted(_spread(), QUOTES["old1"], exit_slipped=True)
    venue_slip = (250.0 * 0.001 + 60.0 * 0.001) * 65
    expected = round(120 * 65 - frictions - slippage - venue_slip, 2)
    pnl = conn.execute("SELECT pnl_net FROM paper_margin_locks WHERE account_id = ? AND "
                       "journal_ref = 'old1'", (ROT,)).fetchone()[0]
    assert pnl == pytest.approx(expected, abs=0.02) and 7000 < pnl < 7800
    assert pm.paper_equity(conn, ROT) == pytest.approx(200000.0 + pnl)
    assert pm.paper_equity(conn, TWO_L) == 200000.0
    # the audit row names the killed trade and the funded one
    ev = conn.execute("SELECT journal_ref, detail FROM paper_account_events WHERE account_id = ? "
                      "AND event_type = ?", (ROT, pm.ROTATION_EVICTION_EVENT)).fetchall()
    assert len(ev) == 1 and ev[0][0] == "old1"
    d = json.loads(ev[0][1])
    assert d["evicted"] == "old1" and d["funded"] == "new1" and d["multiple"] == 1.5
    assert d["evicted_rr_left_real"] == pytest.approx(10 / 190, abs=1e-4)
    # the journal row of the evicted trade says so for that account only
    old1 = j.rows[0]
    assert old1["accounts"][ROT]["status"] == "evicted" and old1["accounts"][TWO_L]["status"] == "approved"
    assert old1["outcome"] is None                                    # the trade itself is still open


def test_a_weak_trade_that_is_strong_on_real_quotes_is_not_evicted(world, monkeypatch):
    conn, j = world
    # the model calls old2 weak; the chain says it is flat (rr_left 1.857 > 1.857/1.5)
    out = pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                      marks_fn=lambda refs: {"old2": 0.05, "old1": 5.0},
                                      evict_fn=_evict_fn(j))
    assert out[ROT]["status"] == "rejected" and out[ROT]["rotation"]["evicted"] is None
    assert pm._active_shadow_lock(conn, ROT, "old2") is not None
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0
    row = conn.execute("SELECT detail FROM paper_account_events WHERE account_id = ? AND event_type = ?",
                       (ROT, pm.ROTATION_DECLINED_EVENT)).fetchone()
    assert "stronger_on_real_quotes" in row[0]


def test_no_chain_quotes_means_no_eviction(world):
    conn, j = world

    def evict(conn, account, ref, lots, max_rr_left, reason):
        return pt.evict_for_rotation(conn, account, ref, lots, max_rr_left=max_rr_left,
                                     quotes_fn=lambda e: None, entries=j.rows)
    out = pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                      marks_fn=_marks, evict_fn=evict)
    assert out[ROT]["status"] == "rejected"
    assert pm._active_shadow_lock(conn, ROT, "old1") is not None


def test_a_rejudge_at_approval_never_evicts_twice(world):
    conn, j = world
    kw = dict(conn=conn, marks_fn=_marks, evict_fn=_evict_fn(j))
    pm.evaluate_shadow_accounts("new1", _new_proposal(), **kw)
    again = pm.evaluate_shadow_accounts("new1", _new_proposal(), **kw)   # decide_pending re-judge
    assert again[ROT] == {"status": "approved", "lots": 1, "margin_rs": 17550.0,
                          "reason": "margin already locked for this entry"}
    assert conn.execute("SELECT COUNT(*) FROM paper_account_events WHERE event_type = ?",
                        (pm.ROTATION_EVICTION_EVENT,)).fetchone()[0] == 1
    assert pm._active_shadow_lock(conn, ROT, "old2") is not None


def test_a_rejudge_keeps_a_held_2l_entry_approved(world):
    """The idempotency check now runs BEFORE sizing: previously a re-judge
    at approval re-sized on cash that already excluded the entry's own lock
    and could stamp 'rejected' on a trade the 2L account holds."""
    conn, _ = world
    v = pm.evaluate_shadow_accounts("old1", _new_proposal(), conn=conn,
                                    marks_fn=_marks, evict_fn=lambda *a: {"status": "x"})
    assert v[TWO_L]["status"] == "approved" and v[TWO_L]["margin_rs"] == 100000.0


def test_the_primary_exit_later_skips_the_evicted_account(world):
    conn, j = world
    pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                marks_fn=_marks, evict_fn=_evict_fn(j))
    old1 = j.rows[0]
    old1["accounts"][ROT]["status"] = "approved"      # even if a stale rewrite lost the stamp
    rec = pt._execute_paper_exit(old1, QUOTES["old1"], "profit_take", conn=conn)
    assert rec["mode"] == "paper_venue" and set(rec["accounts"]) == {TWO_L}
    exits = conn.execute("SELECT account_id FROM trade_tickets WHERE kind = ? AND journal_ref = 'old1' "
                         "ORDER BY account_id", (oms.EXIT,)).fetchall()
    assert [r[0] for r in exits] == ["PAPER_10L", TWO_L, ROT]        # ROT's is the eviction's, not a 2nd
    res = pm.release_entry("old1", 7000.0, conn=conn)
    assert set(res["shadow_accounts"]) == {TWO_L}                     # ROT is not settled twice


def test_rotation_marks_use_the_live_spot_and_abstain_without_one():
    rows = [_entry("old1"), _entry("old2"), dict(_entry("gone"), outcome={"x": 1})]
    from datetime import date
    m = pt.rotation_marks(["old1", "old2", "gone", "nope"], entries=rows,
                          spot_fn=lambda t: 24150.0, today=date(2026, 10, 27))
    assert set(m) == {"old1", "old2"}              # resolved / unknown refs are not candidates
    p = pt._profit_ps_now(_spread(), "2026-09-20", 24150.0, date(2026, 10, 27))
    assert 80.0 < p < 82.0                         # intrinsic 150 - 70 debit + 1/38 of time value
    assert m["old1"] == pytest.approx(pm.reward_risk_left(_spread(), p))
    assert pt.rotation_marks(["old1"], entries=rows, spot_fn=lambda t: None) == {}


def test_the_switch_off_keeps_the_rotation_account_out(world, monkeypatch):
    conn, _ = world
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    assert pm.shadow_account_ids() == (TWO_L,)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", False)
    assert pm.shadow_account_ids() == ()


def test_the_audit_log_shows_the_eviction(world, tmp_path):
    conn, j = world
    pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                marks_fn=_marks, evict_fn=_evict_fn(j))
    db = tmp_path / "b.db"
    conn.commit()
    disk = sqlite3.connect(db)
    conn.backup(disk)
    disk.close()
    rows = [r for r in dash.audit_events(db_path=db) if r["event_type"] == pm.ROTATION_EVICTION_EVENT]
    assert len(rows) == 1 and rows[0]["account"] == ROT
    assert '"evicted": "old1"' in rows[0]["detail"] and '"funded": "new1"' in rows[0]["detail"]
    t = dash.treasury(db_path=db)
    assert ROT in t and t[ROT]["open_locks"] == 2

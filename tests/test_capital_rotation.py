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

from tests.fake_journal import FakeJournalBase
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


class FakeJournal(FakeJournalBase):
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
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)   # #120's arm has its own file
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
    def fn(conn, account, ref, lots, max_rr_left, reason, need_rs=None):
        return pt.evict_for_rotation(conn, account, ref, lots, max_rr_left=max_rr_left,
                                     reason=reason, quotes_fn=lambda e: QUOTES[e["short_id"]],
                                     entries=j.rows, need_rs=need_rs)
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
                                      marks_fn=_marks, evict_fn=_evict_fn(j),
                                      allow_rotation=True)
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


def _serve_book(monkeypatch, nodes: dict, calls: list = None):
    """Fake ONLY the Dhan chain call, so the eviction runs the REAL quote door
    (live_bridge._leg_quotes_for). nodes: {(strike, 'CE'): (bid, ask, last)}."""
    from src import dhan_client
    oc = {}
    for (strike, kind), (bid, ask, ltp) in nodes.items():
        oc.setdefault(f"{float(strike):.6f}", {})[kind.lower()] = {
            "top_bid_price": bid, "top_ask_price": ask, "last_price": ltp}

    def fake(ticker, expiry):
        if calls is not None:
            calls.append((ticker, expiry))
        return {"last_price": 24300.0, "oc": oc}
    monkeypatch.setattr(dhan_client, "get_option_chain", fake)


def test_an_eviction_through_the_real_door_uses_crossed_quotes(world, monkeypatch):
    """Architect ruling 1 (2026-10-05): the eviction closes the slice on
    CROSSED quotes — the long 24000 CE sold at its bid, the short 24200 CE
    bought back at its ask, never the last traded 250 / 60. rr_left is
    re-verified on that realizable value, the EXIT ticket's limits are the
    crossed prices, and no exit-side ladder is charged on top of them."""
    conn, j = world
    calls = []
    _serve_book(monkeypatch, {(24000.0, "CE"): (248.0, 252.0, 250.0),
                              (24200.0, "CE"): (58.0, 62.0, 60.0)}, calls)
    res = pt.evict_for_rotation(conn, ROT, "old1", 1, max_rr_left=5.0, entries=j.rows)   # default door
    assert calls == [("NIFTY 50", "2026-10-28")]
    assert res["status"] == "evicted", res
    assert res["exit_price_basis"] == "crossed"
    # crossed: 248 - 62 = 186 vs the 70 debit -> +116/share (last prices said +120)
    assert res["rr_left"] == pytest.approx((130 - 116) / (70 + 116), abs=1e-4)
    limits = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT strike, side, limit_price FROM trade_legs")}
    assert limits == {(24000.0, "SELL"): 248.0, (24200.0, "BUY"): 62.0}
    crossed = {(24000.0, "CE"): 248.0, (24200.0, "CE"): 62.0}
    frictions, entry_side = pt._spread_exit_costs_quoted(_spread(), crossed, exit_crossed=True)
    _, with_exit_ladder = pt._spread_exit_costs_quoted(_spread(), crossed)
    assert with_exit_ladder > entry_side                               # the ladder the ruling drops
    venue_slip = (248.0 * 0.001 + 62.0 * 0.001) * 65
    pnl = conn.execute("SELECT pnl_net FROM paper_margin_locks WHERE account_id = ? AND "
                       "journal_ref = 'old1'", (ROT,)).fetchone()[0]
    assert pnl == pytest.approx(round(116 * 65 - frictions - entry_side - venue_slip, 2), abs=0.02)
    assert j.rows[0]["accounts"][ROT]["exit_price_basis"] == "crossed"


def test_an_eviction_is_refused_when_the_door_cannot_cross_a_leg(world, monkeypatch, capsys):
    """The long leg has no bid: the door refuses the whole quote set, so
    nothing is evicted and no ticket is issued."""
    conn, j = world
    _serve_book(monkeypatch, {(24000.0, "CE"): (0, 252.0, 250.0),
                              (24200.0, "CE"): (58.0, 62.0, 60.0)})
    res = pt.evict_for_rotation(conn, ROT, "old1", 1, max_rr_left=5.0, entries=j.rows)
    assert res["status"] == "no_chain_quotes"
    assert "refused on 24000CE: no bid to sell the long leg" in capsys.readouterr().out
    assert pm._active_shadow_lock(conn, ROT, "old1") is not None
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0


def test_a_non_venue_eviction_books_no_exit_ladder_on_crossed_quotes(world, monkeypatch):
    """Without the paper venue the crossing is the whole exit cost: the
    booked slippage is the entry-side ladder alone."""
    conn, j = world
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    crossed = {(24000.0, "CE"): 248.0, (24200.0, "CE"): 62.0}
    res = pt.evict_for_rotation(conn, ROT, "old1", 1, max_rr_left=5.0,
                                quotes_fn=lambda e: crossed, entries=j.rows)
    assert res["status"] == "evicted" and res["execution_mode"] == "model"
    frictions, entry_side = pt._spread_exit_costs_quoted(_spread(), crossed, exit_crossed=True)
    assert res["slippage_rs"] == pytest.approx(entry_side, abs=0.01)
    assert res["pnl_rs"] == pytest.approx(round(116 * 65 - frictions - entry_side, 2), abs=0.02)


def test_the_funding_estimate_does_not_charge_the_exit_ladder_on_crossed_quotes(world, monkeypatch):
    """The estimate bounds what the settle books: on crossed quotes that is
    the entry-side ladder + the venue's tier slip (+ a tick per leg) — the
    exit-side ladder, which neither path books any more, is not added."""
    conn, j = world
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.0)   # venue slip = ticks only
    quotes = QUOTES["old1"]
    mine = dict(_spread(), lots=1)
    f, entry_side = pt._spread_exit_costs_quoted(mine, quotes, exit_crossed=True)
    _, with_exit_ladder = pt._spread_exit_costs_quoted(mine, quotes)
    ticks = 0.05 * 2 * 65
    assert with_exit_ladder > entry_side + ticks                      # the case that matters
    profit = 120.0 * 65
    freed = pm.paper_available_cash(conn, ROT) + 100000.0 + profit - f - entry_side - ticks
    ok = pt.evict_for_rotation(conn, ROT, "old1", 1, max_rr_left=5.0, quotes_fn=lambda e: quotes,
                               entries=j.rows, need_rs=freed - 0.01)
    assert ok["status"] == "evicted", ok


def test_a_weak_trade_that_is_strong_on_real_quotes_is_not_evicted(world, monkeypatch):
    conn, j = world
    # the model calls old2 weak; the chain says it is flat (rr_left 1.857 > 1.857/1.5)
    out = pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                      marks_fn=lambda refs: {"old2": 0.05, "old1": 5.0},
                                      evict_fn=_evict_fn(j), allow_rotation=True)
    assert out[ROT]["status"] == "rejected" and out[ROT]["rotation"]["evicted"] is None
    assert pm._active_shadow_lock(conn, ROT, "old2") is not None
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0
    row = conn.execute("SELECT detail FROM paper_account_events WHERE account_id = ? AND event_type = ?",
                       (ROT, pm.ROTATION_DECLINED_EVENT)).fetchone()
    assert "stronger_on_real_quotes" in row[0]


def test_no_chain_quotes_means_no_eviction(world):
    conn, j = world

    def evict(conn, account, ref, lots, max_rr_left, reason, need_rs=None):
        return pt.evict_for_rotation(conn, account, ref, lots, max_rr_left=max_rr_left,
                                     quotes_fn=lambda e: None, entries=j.rows)
    out = pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                      marks_fn=_marks, evict_fn=evict, allow_rotation=True)
    assert out[ROT]["status"] == "rejected"
    assert pm._active_shadow_lock(conn, ROT, "old1") is not None


def test_a_rejudge_at_approval_never_evicts_twice(world):
    conn, j = world
    kw = dict(conn=conn, marks_fn=_marks, evict_fn=_evict_fn(j),
                                      allow_rotation=True)
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
                                    marks_fn=_marks, evict_fn=lambda *a, **k: {"status": "x"})
    assert v[TWO_L]["status"] == "approved" and v[TWO_L]["margin_rs"] == 100000.0


def test_the_primary_exit_later_skips_the_evicted_account(world):
    conn, j = world
    pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn,
                                marks_fn=_marks, evict_fn=_evict_fn(j),
                                      allow_rotation=True)
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
                                marks_fn=_marks, evict_fn=_evict_fn(j),
                                      allow_rotation=True)
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


# ------------------------------------------- D6 (decision #122): approval only

def test_a_proposal_never_evicts_the_margin_wall_is_a_named_deferral(world):
    """#122 amends #115: at PROPOSAL time the rotation account is judged
    like PAPER_2L — no live position is closed to fund a signal that may
    yet be rejected. The eviction happens only at approval."""
    conn, j = world
    calls = []
    out = pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn, marks_fn=_marks,
                                      evict_fn=lambda *a, **k: calls.append(a) or {"status": "evicted"})
    assert calls == []
    assert out[ROT]["status"] == "rejected" and "judged at approval" in out[ROT]["reason"]
    assert "rotation" not in out[ROT]
    assert pm._active_shadow_lock(conn, ROT, "old1") is not None
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0
    # the approval judgement of the same entry does evict
    again = pm.evaluate_shadow_accounts("new1", _new_proposal(), conn=conn, marks_fn=_marks,
                                        evict_fn=_evict_fn(j), allow_rotation=True)
    assert again[ROT]["status"] == "approved" and again[ROT]["rotation"]["evicted"] == "old1"


def test_a_pending_trade_is_never_an_eviction_candidate(world):
    """A still-pending proposal's lock is a reservation, not a position:
    rotation_marks leaves it out, and evict_for_rotation refuses it."""
    conn, j = world
    j.rows[0]["decision"] = "pending_approval"
    from datetime import date
    m = pt.rotation_marks(["old1", "old2"], entries=j.rows, spot_fn=lambda t: 24150.0,
                          today=date(2026, 10, 27))
    assert set(m) == {"old2"}
    res = pt.evict_for_rotation(conn, ROT, "old1", 1, quotes_fn=lambda e: QUOTES["old1"],
                                entries=j.rows)
    assert res["status"] == "not_entered"
    assert pm._active_shadow_lock(conn, ROT, "old1") is not None
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0


def test_an_eviction_always_funds_the_entry_it_was_made_for(world):
    """#122 panel finding: the sizer counts lots on the UNSTRESSED per-lot
    margin, so after an eviction it could pick 2 lots whose VIX-stressed ask
    no longer fits — the gate then refused the very entry a live trade was
    closed for. The lots are now trimmed to what the stressed ask fits."""
    conn, j = world
    conn.execute("UPDATE paper_margin_locks SET margin_rs = 35000 WHERE account_id = ? "
                  "AND journal_ref = 'old1'", (ROT,))
    conn.execute("UPDATE paper_margin_locks SET margin_rs = 165000 WHERE account_id = ? "
                  "AND journal_ref = 'old2'", (ROT,))
    conn.commit()
    hot = dict(_new_proposal(), vix=30.0)                     # stress factor 1.3
    out = pm.evaluate_shadow_accounts("new1", hot, conn=conn, risk_pct=5.0, marks_fn=_marks,
                                      evict_fn=_evict_fn(j), allow_rotation=True)
    assert out[ROT]["rotation"]["evicted"] == "old1"
    assert out[ROT]["status"] == "approved" and out[ROT]["lots"] == 1
    assert out[ROT]["margin_rs"] == pm.required_margin_for({"spread": dict(_spread(), lots=1), "vix": 30.0})



def test_an_eviction_that_would_not_fund_the_entry_is_refused_before_any_exit(world):
    """#122 panel round 2: the funding check counted the evicted lock's
    margin but not the P&L its exit books. A slice closed at a loss could
    leave the one-lot ask unfunded after the trade was already gone. The
    exit is now refused BEFORE any ticket when cash + margin + the slice's
    P&L on the quotes cannot fund one stressed lot."""
    conn, j = world
    conn.execute("UPDATE paper_margin_locks SET margin_rs = 12600 WHERE account_id = ? "
                 "AND journal_ref = 'old1'", (ROT,))
    conn.execute("UPDATE paper_margin_locks SET margin_rs = 182400 WHERE account_id = ? "
                 "AND journal_ref = 'old2'", (ROT,))
    conn.commit()
    assert pm.paper_available_cash(conn, ROT) == 5000.0     # 5,000 + 12,600 = 17,600 >= 17,550 before P&L
    # old1 marks at 98 - 30 = 68 vs its 70 debit: a small loss plus exit costs
    res = pt.evict_for_rotation(conn, ROT, "old1", 1, max_rr_left=5.0,
                                quotes_fn=lambda e: {(24000.0, "CE"): 98.0, (24200.0, "CE"): 30.0},
                                entries=j.rows, need_rs=17550.0)
    assert res["status"] == "would_not_fund"
    assert pm._active_shadow_lock(conn, ROT, "old1") is not None
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0


def test_the_funding_estimate_counts_entry_side_slippage_beside_the_venue_exit(world, monkeypatch):
    """#122 panel round 5: when the venue's tier slippage beats the exit
    ladder, the estimate must still count the entry-side ladder the settle
    books (legs without a quoted/venue fill basis)."""
    conn, j = world
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.005)
    quotes = QUOTES["old1"]
    mine = dict(_spread(), lots=1)
    assert all(l.get("fill_basis") not in ("quoted", "venue", "live_crossed") for l in mine["legs"])
    f, ladder = pt._spread_exit_costs_quoted(mine, quotes, exit_slipped=False)
    _, entry_side = pt._spread_exit_costs_quoted(mine, quotes, exit_slipped=True)
    venue = sum(q * 0.005 + 0.05 for q in quotes.values()) * 65
    assert entry_side > 0 and venue > ladder                        # the case that mattered
    profit = (min(250.0 - 60.0 - 70.0, 130.0)) * 65
    cash, margin = pm.paper_available_cash(conn, ROT), 100000.0
    freed_without_entry_side = cash + margin + profit - f - max(ladder, venue)
    res = pt.evict_for_rotation(conn, ROT, "old1", 1, max_rr_left=5.0, quotes_fn=lambda e: quotes,
                                entries=j.rows, need_rs=freed_without_entry_side - 0.01)
    assert res["status"] == "would_not_fund"
    assert pm._active_shadow_lock(conn, ROT, "old1") is not None
    assert conn.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 0

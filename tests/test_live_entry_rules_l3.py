"""
PAPER_2L_LIVE — audit Chunk 2 lows F11 + F12 + F13 + F14 and the L2
verifier's residuals (2026-10-06, batch L3).

F11: debit vs credit came from the SIGN of the crossed price, never the
structure. A bear put re-quoted on an inverted book (net credit) was booked
as a credit structure, a condor re-quoted at a net debit was given max loss
= d (its real worst case is the width + d), and the proposer packaged and
sized a net-debit condor on that tiny 'max loss'. Now the structure decides
(strategy_router.premium_refusal): the re-quote, the door that writes the
row and the proposer all refuse such prices by name.

F12 (policy default): the live arm entered at the approval-time crossed
re-quote at the proposal's lots, whatever it cost. Now the re-quote is
judged by the desk's own rules first: below the #98 R:R floor it abstains
(lock released at zero, named); the #106 2% risk budget on the CROSSED max
loss re-sizes it DOWN (never up; 1-lot floor kept and recorded), the lock
shrinking with it.

F13 (policy default — the halt-stack rule): a lock held since the proposal
skipped every halt at approval. Now a halted shadow is refused for the
entry (lock released at zero), and a halted primary refuses the approval
as MARGIN_BLOCKED with the halt named (the entry stays pending; every door
already handles that status).

F14 (policy default — the off switch stops NEW live entries, never abandons
open ones): with the arm switched off its open positions went unmarked and
unexited until the expiry backstop. Now the live loop still ticks them
(manage-only) and says so on ONE card a day; with the paper venue off the
arm's proposal-time lock is released at zero at approval, named.

L2 residuals: (a) a failed kept-lock event inside expire_pending_lock's one
transaction raises (nothing expires) instead of being swallowed; (b)
open_position never overwrites a row — a re-approval of an entry whose LIVE
position is already recorded issues no second ticket; (c) the
held_impossible_mark memo is pinned, and a CLOSED row with its lock still
active is named as such.

Hermetic: a FILE brain_map at the per-test tmp DEFAULT_DB_PATH (or
':memory:'), tmp journal (conftest), injected chains and clocks, Discord
captured. No network, nothing under data/ or logs/.
"""
import asyncio
from datetime import date, datetime

import pytest

from src import brain_map, journal, oms, plan_tracker as pt, portfolio_manager as pm
from src import exposure_gate as eg, options_proposer as op, strategy_router as sr
from src import live_bridge as lb
from src.execution import live_pricer as lp, paper_venue as pv
from src.strategy import StrategyConstructor

_REAL_CONNECT = brain_map.connect          # captured before any fixture patches it
PRIMARY, TWO_L, LIVE = pm.ACCOUNT_PAPER_10L, pm.ACCOUNT_PAPER_2L, pm.ACCOUNT_PAPER_2L_LIVE
EXPIRY = "2026-10-27"
DAY = date(2026, 10, 21)                     # 6 days out: outside the forced-exit window
AT = datetime(2026, 10, 21, 10, 0)


class _KeepOpen:
    """The shared file connection survives every callee's close()."""

    def __init__(self, c):
        object.__setattr__(self, "_c", c)

    def __getattr__(self, n):
        return getattr(self._c, n)

    def __setattr__(self, n, v):
        setattr(self._c, n, v)

    def close(self):
        pass


def _chain(quotes, spot=24000.0):
    oc = {}
    for (k, t), (bid, ask, ltp) in quotes.items():
        oc.setdefault(f"{float(k):.6f}", {})[t.lower()] = {"top_bid_price": bid, "top_ask_price": ask,
                                                           "last_price": ltp}
    return {"last_price": spot, "oc": oc}


def _bull_call(buy=100.0, sell=30.0, expiry=EXPIRY):
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, buy, sell)
    for leg in s["legs"]:
        leg["fill_basis"] = "quoted"
    s.update(lots=1, expiry=expiry, entry_spot=24000.0)
    return s


def _condor(short=60.0, wing=10.0, expiry=EXPIRY):
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_iron_condor(23500, 24500, 200, short, wing, short, wing)
    for leg in s["legs"]:
        leg["fill_basis"] = "quoted"
    s.update(lots=1, expiry=expiry, entry_spot=24000.0)
    return s


def _call_book(buy_ask, sell_bid):
    """The bull call's two legs, crossed: BUY 24000 CE at `buy_ask`, SELL 24200 CE at `sell_bid`."""
    return _chain({(24000, "CE"): (buy_ask - 2.0, buy_ask, buy_ask - 1.0),
                   (24200, "CE"): (sell_bid, sell_bid + 2.0, sell_bid + 1.0)})


def _condor_book(short_bid, wing_ask):
    q = {}
    for k, t in ((23500, "PE"), (24500, "CE")):
        q[(k, t)] = (short_bid, short_bid + 2.0, short_bid + 1.0)
    for k, t in ((23300, "PE"), (24700, "CE")):
        q[(k, t)] = (wing_ask - 1.0, wing_ask, wing_ask - 0.5)
    return _chain(q)


@pytest.fixture
def desk(monkeypatch, tmp_path):
    """Primary + 2L + LIVE (rotation off), the venue on, the market open;
    every writer shares the per-test FILE brain_map."""
    raw = brain_map.connect(brain_map.DEFAULT_DB_PATH)
    c = _KeepOpen(raw)
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    mp = monkeypatch
    mp.setattr(brain_map, "connect", lambda *a, **k: c)
    mp.setattr(pt, "_brain_connect", lambda *a, **k: c)
    mp.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    mp.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    mp.setattr(pm, "CAPITAL_ROTATION_ENABLED", False)
    mp.setattr("src.config.PAPER_VENUE_ENABLED", True)
    mp.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.0)
    mp.setattr(lp, "_market_open", lambda now: True)
    mp.setattr(lp, "_now", lambda: AT)
    mp.setattr(lp, "_stamp_journal", lambda row, payload: None)
    mp.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    mp.setattr(op, "_today", lambda: DAY)
    book = {"chain": _call_book(100.0, 30.0)}
    mp.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: book["chain"])
    notes, cards = [], []
    mp.setattr(op, "_notify_discord", lambda text, *a, **k: notes.append(text))
    mp.setattr("src.notifier.fire_broadcast", lambda payload, *a, **k: cards.append(payload))
    mp.setattr(eg, "LEDGER_PATH", tmp_path / "exposure_blocks.jsonl")
    mp.setattr(eg, "_record_opportunity_cost", lambda *a, **k: None)
    lp.reset_cache()
    yield {"c": c, "mp": mp, "book": book, "notes": notes, "cards": cards}
    lp.reset_cache()
    raw.close()


def _propose(ref, spread, ticker="NIFTY 50"):
    """A pending spread exactly as run_headless leaves it: the primary's lock
    taken by the headless gate, every shadow judged (and locked) at proposal
    time, the verdicts stamped on the journaled row."""
    required = pm.required_margin_for({"spread": spread, "vix": 13.0})
    assert pm.gate_headless_entry(ref, required)[0]
    accounts = op._judge_shadow_accounts(ref, {"spread": spread, "lots": 1, "vix": 13.0})
    row = {"short_id": ref, "date": "2026-10-21", "ticker": ticker, "action": "SPREAD",
           "decision": "pending_approval", "why": "(headless proposal)", "outcome": None,
           "spread": spread, "signal": "t", "receipt": {"vix": 13.0}, "accounts": accounts}
    journal.log(row)
    return row


def _events(c, account, kind, ref=None):
    sql = "SELECT detail FROM paper_account_events WHERE account_id = ? AND event_type = ?"
    args = [account, kind]
    if ref is not None:
        sql += " AND journal_ref = ?"
        args.append(ref)
    return [r[0] for r in c.execute(sql, args).fetchall()]


def _primary_events(c, kind):
    return [r[0] for r in c.execute("SELECT detail FROM account_events WHERE event_type = ?", (kind,)).fetchall()]


def _tickets(c, ref, account=None):
    sql, args = "SELECT COUNT(*) FROM trade_tickets WHERE journal_ref = ?", [ref]
    if account:
        sql += " AND account_id = ?"
        args.append(account)
    return c.execute(sql, args).fetchone()[0]


def _primary_lock_active(c, ref):
    return c.execute("SELECT 1 FROM margin_locks WHERE journal_ref = ? AND released_at IS NULL",
                     (ref,)).fetchone() is not None


# =================================================================== F11 — the structure decides

INVERTED_PUTS = _chain({(13725, "PE"): (148.0, 150.0, 160.0), (13625, "PE"): (160.0, 165.0, 150.0)}, 13700.0)
FLIPPED_CONDOR = _chain({(23500, "PE"): (5.0, 6.5, 5.5), (23300, "PE"): (5.0, 6.0, 5.5),
                         (24500, "CE"): (5.0, 6.5, 5.5), (24700, "CE"): (5.0, 6.0, 5.5)})


def _bear_put():
    s = StrategyConstructor(vix=13.0, lot_size=120).construct_bear_put_spread(13725, 13625, 208.75, 169.10)
    for leg in s["legs"]:
        leg["fill_basis"] = "quoted"
    s.update(lots=1, expiry=EXPIRY, entry_spot=13700.0)
    return s


def test_a_debit_vertical_requoted_at_a_net_credit_is_refused_not_booked_as_a_credit():
    """repro test_bnd2_sign_rule_vertical_credit: the bear put on an inverted
    book (13725PE ask 150 below 13625PE bid 160) used to be accepted with
    max_profit 10 / max_loss 90 and every outcome booked +10."""
    e = {"short_id": "f11v01", "ticker": "NIFTY MID SELECT", "spread": _bear_put()}
    rq = lp.requote_entry(e, now=AT, chain_fn=lambda t, x: INVERTED_PUTS)
    assert rq["ok"] is False and rq["legs"] is None
    assert rq["reason"] == ("crossed prices invert the structure: bear_put_spread is a DEBIT structure but "
                            "these prices give a net credit of 10.00/share — an inverted or stale book (audit F11)")


def test_a_condor_requoted_at_a_net_debit_is_refused_not_capped_at_d():
    """repro test_bounds_net_debit_condor: short bids 5, wing asks 6 -> d +2,
    accepted with max_loss 2 where the real worst case is 202."""
    e = {"short_id": "f11c01", "ticker": "NIFTY 50", "spread": _condor(30.0, 10.0)}
    rq = lp.requote_entry(e, now=AT, chain_fn=lambda t, x: FLIPPED_CONDOR)
    assert rq["ok"] is False
    assert "iron_condor is a CREDIT structure but these prices give a net debit of 2.00/share" in rq["reason"]


def test_the_proposer_refuses_a_net_debit_condor_by_name_instead_of_sizing_it(monkeypatch):
    """repro test_f11_verify_sign_rule_bounds: build_proposal packaged the
    flipped condor at R:R 99 and sized it at 38 lots on a 2/share 'max loss'."""
    q = {}
    for k in range(23000, 25050, 50):
        q[(k, "PE")] = (40.0, 41.0, 40.5)
        q[(k, "CE")] = (40.0, 41.0, 40.5)
    q[(23500, "PE")] = (5.0, 6.5, 5.5)
    q[(23300, "PE")] = (5.0, 6.0, 5.5)
    q[(24500, "CE")] = (5.0, 6.5, 5.5)
    q[(24700, "CE")] = (5.0, 6.0, 5.5)
    out = op.build_proposal("NIFTY 50", analysis={"uptrend": True, "fresh_cross": False, "rsi": 50.0,
                                                  "price": 24000.0},
                            vix=13.0, expiry=EXPIRY, chain=_chain(q), book={"cash": 1_000_000.0, "positions": {}},
                            prices={}, account_equity=1_000_000.0, today=DAY, horizon="short", advisory=None)
    assert out["proposal"] is None and "rejected_spread" not in out
    assert out["reason"] == ("structure refused: iron_condor is a CREDIT structure but these prices give a net "
                             "debit of 2.00/share — an inverted or stale book (audit F11)")


@pytest.mark.parametrize("build, named", [
    (lambda sc: sc.construct_iron_condor(23500, 24500, 200, 5.0, 6.0, 5.0, 6.0), "a net debit of 2.00/share"),
    (lambda sc: sc.construct_iron_butterfly(24000, 200, 50.0, 50.0, 50.0, 50.0), "a zero net premium"),
    (lambda sc: sc.construct_bear_put_spread(13725, 13625, 150.0, 160.0), "a net credit of 10.00/share"),
    (lambda sc: sc.construct_bull_call_spread(24000, 24200, 30.0, 30.0), "a zero net premium"),
])
def test_package_refuses_premiums_that_invert_the_structure(build, named):
    sc = StrategyConstructor(vix=13.0, lot_size=65)
    assert build(sc) is None
    assert named in sc.last_refusal and "(audit F11)" in sc.last_refusal
    ok = sc.construct_bull_call_spread(24000, 24200, 100.0, 30.0)      # a sane one clears the memo
    assert ok is not None and sc.last_refusal is None and ok["net_debit"] == 70.0


def test_structure_bounds_come_from_the_structure_never_from_the_sign():
    # a debit structure keeps the debit formulas whatever d's sign: the
    # contradiction shows as a non-positive bound (the fill's TRUE bound)
    assert lp.structure_bounds(_bear_put(), -10.0) == {"width_ps": 100.0, "max_profit_ps": 110.0,
                                                       "max_loss_ps": -10.0}
    assert lp.structure_bounds(_condor(30.0, 10.0), 2.0) == {"width_ps": 200.0, "max_profit_ps": -2.0,
                                                             "max_loss_ps": 202.0}
    assert lp.structure_bounds(_condor(30.0, 10.0), -40.0)["max_loss_ps"] == 160.0
    with pytest.raises(ValueError, match="unknown structure 'strangle'"):
        lp.structure_bounds({"strategy": "strangle", "spread_width": 100.0}, 5.0)
    assert sr.premium_refusal("strangle", 5.0).startswith("unknown structure 'strangle'")
    assert sr.premium_refusal("bull_call_spread", 0.05) is None
    assert sr.premium_refusal("iron_condor", -0.05) is None


def _filled_live_ticket(c, ref, legs_px, spread=None):
    """A FILLED LIVE entry ticket at exactly `legs_px` (the venue fills LIVE at
    its limits) — no re-quote, so the door that writes is the only judge."""
    s = spread or _bear_put()
    legs = [dict(l, premium=legs_px[(float(l["strike"]), l["option_type"])]) for l in s["legs"]]
    prop = {"ticker": "NIFTY MID SELECT", "short_id": ref, "signal": "t", "spread": dict(s, legs=legs)}
    issued = sr.issue(c, prop, journal_ref=ref, source="t", account_id=LIVE, lots=1)
    pv.sweep(c, stamp=False)
    view = oms.ticket_view(c, issued["ticket_id"])
    assert view["status"] == oms.FILLED
    return issued["ticket_id"], view


def test_open_position_refuses_fills_that_invert_the_structure(desk):
    c = desk["c"]
    _, view = _filled_live_ticket(c, "f11o01", {(13725.0, "PE"): 150.0, (13625.0, "PE"): 160.0})
    e = {"short_id": "f11o01", "ticker": "NIFTY MID SELECT", "spread": _bear_put()}
    with pytest.raises(lp.LiveEntryRefused, match="the entry fills invert the structure: bear_put_spread is a "
                                                   "DEBIT structure"):
        lp.open_position(c, LIVE, e, view, now=AT)
    assert lp.position_state(c, LIVE, "f11o01") is None


def test_a_filled_entry_refused_after_the_fill_is_named_once_and_keeps_its_lock(desk):
    """The approval's post-fill door: a FILLED live ticket the row door
    refuses is never booked on sign bounds — one event + one card a day,
    the verdict says so, the lock is kept (F15), and the tick's repair
    neither raises nor pages again; other refs are still repaired."""
    c, cards = desk["c"], desk["cards"]
    pm.paper_request_entry(c, LIVE, "f11a01", 16764.0, lots=1, primary_lots=1)
    tid, view = _filled_live_ticket(c, "f11a01", {(13725.0, "PE"): 150.0, (13625.0, "PE"): 160.0})
    e = {"short_id": "f11a01", "ticker": "NIFTY MID SELECT", "spread": _bear_put(),
         "accounts": {LIVE: {"status": "approved", "lots": 1}}}
    op._live_after_fill(c, LIVE, e, tid, view, "2026-10-21T10:00:00")
    assert e["accounts"][LIVE]["status"] == "refused_after_fill"
    assert f"entry ticket {tid} FILLED but the live position was refused" in e["accounts"][LIVE]["reason"]
    assert lp.position_state(c, LIVE, "f11a01") is None and pm._active_shadow_lock(c, LIVE, "f11a01")
    assert len(_events(c, LIVE, lp.EVENT_FILLED_REFUSED, "f11a01")) == 1
    assert [x["event"] for x in cards] == ["live_entry_needs_review"]
    # a sane filled-but-unrecorded entry beside it: the repair opens that one
    pm.paper_request_entry(c, LIVE, "f11a02", 16764.0, lots=1, primary_lots=1)
    _filled_live_ticket(c, "f11a02", {(13725.0, "PE"): 208.75, (13625.0, "PE"): 169.10})
    for minute in (1, 2):
        out = lp.tick(now=AT.replace(minute=minute), conn=c, chain_fn=lambda t, x: None,
                      sleep_fn=lambda s: None, now_epoch_fn=lambda: 1000.0 + minute)
    assert out.get("repaired") is None and lp.position_state(c, LIVE, "f11a02") == "open"
    assert lp.position_state(c, LIVE, "f11a01") is None
    assert len(_events(c, LIVE, lp.EVENT_FILLED_REFUSED, "f11a01")) == 1 and len(cards) == 1


# =================================================================== F12 — the desk's rules on the re-quote

def test_a_requote_below_the_rr_floor_is_refused_for_the_live_arm_only(desk):
    """repro test_f12_verify_requote_rr_resize_e2e: proposed at debit 20 (3
    lots, R:R 9), approved after a rally at a crossed debit of 140 (R:R
    0.43): the arm entered at 3 lots, Rs.27,300 at risk."""
    c, ref = desk["c"], "f12a0001"
    pend = _propose(ref, _bull_call(40.0, 20.0))
    assert pend["accounts"][LIVE]["lots"] == 3 and pend["accounts"][TWO_L]["lots"] == 3
    desk["book"]["chain"] = _call_book(165.0, 25.0)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == "approved"
    v = out["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected" and v["lots"] == 0
    assert v["reason"] == ("live entry refused: the crossed re-quote (net +140.00/share) fails the desk's R:R "
                           "floor — REJECTED_POOR_RR: R:R below 1.5 threshold — bull_call_spread pays Rs.3,900 "
                           "against Rs.9,100 (R:R 0.43) (audit F12)")
    assert lp.position_state(c, LIVE, ref) is None and pm._active_shadow_lock(c, LIVE, ref) is None
    assert _tickets(c, ref, LIVE) == 0
    (ev,) = _events(c, LIVE, "live_entry_refused", ref)
    assert "fails the desk's R:R floor" in ev and ev.endswith("— lock released at zero")
    # the primary and the 2L shadow are untouched: their own tickets, their own lots
    assert out["entry"]["accounts"][TWO_L]["status"] == "approved" and out["entry"]["accounts"][TWO_L]["lots"] == 3
    assert _tickets(c, ref, oms.PRIMARY_ACCOUNT) == 1 and _tickets(c, ref, TWO_L) == 1
    assert pm._active_shadow_lock(c, TWO_L, ref)[1] == 3


def test_the_crossed_max_loss_resizes_the_live_arm_down_and_shrinks_its_lock(desk):
    """repro test_bounds_requote_resize: sized 3 lots on a 1,300/lot loss,
    entered at a re-quote whose loss is 3,900/lot — the 2% budget (4,000)
    carries ONE lot, and the lock is a third of the 3-lot one."""
    c, ref = desk["c"], "f12r0001"
    _propose(ref, _bull_call(40.0, 20.0))
    margin3, lots3 = pm._active_shadow_lock(c, LIVE, ref)
    assert lots3 == 3
    desk["book"]["chain"] = _call_book(85.0, 25.0)                    # d 60: mp 140 / ml 60 -> R:R 2.33
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    v = out["entry"]["accounts"][LIVE]
    assert v["status"] == "approved" and v["lots"] == 1 and v["venue_status"] == oms.FILLED
    assert v["requote_sizing"] == {"lots_approved": 3, "lots": 1, "crossed_max_loss_rs": 3900.0,
                                   "reward_risk": 2.3333, "by_risk": 1, "risk_capacity_rs": 4000.0,
                                   "risk_at_lots_rs": 3900.0, "floor_applied": False, "reason": "sized",
                                   "margin_rs": round(margin3 / 3, 2)}
    assert v["margin_rs"] == round(margin3 / 3, 2)
    assert pm._active_shadow_lock(c, LIVE, ref) == (round(margin3 / 3, 2), 1)
    row = lp.open_rows(c, LIVE)[0]
    assert row["lots"] == 1 and row["entry_mark_ps"] == 60.0
    (ev,) = _events(c, LIVE, "live_entry_resized", ref)
    assert ev.startswith("crossed max loss Rs.3,900.00/lot: 3 -> 1 lot(s) on the 2% budget Rs.4,000.00")
    assert pm._active_shadow_lock(c, TWO_L, ref)[1] == 3                # the other shadow keeps its sizing


def test_the_one_lot_floor_is_kept_and_recorded(desk):
    c, ref = desk["c"], "f12f0001"
    _propose(ref, _bull_call(40.0, 20.0))
    desk["book"]["chain"] = _call_book(100.0, 25.0)                   # d 75: loss 4,875/lot > 4,000
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert v["status"] == "approved" and v["lots"] == 1
    s = v["requote_sizing"]
    assert s["by_risk"] == 0 and s["floor_applied"] is True and s["risk_at_lots_rs"] == 4875.0
    assert s["reason"].startswith("1-lot floor: max loss Rs.4,875/lot exceeds the 2% risk capacity Rs.4,000")
    assert lp.open_rows(c, LIVE)[0]["lots"] == 1


def test_a_cheaper_requote_never_sizes_above_the_approved_lots(desk):
    c, ref = desk["c"], "f12u0001"
    _propose(ref, _bull_call(100.0, 30.0))                            # 4,550/lot: approved at the 1-lot floor
    held = pm._active_shadow_lock(c, LIVE, ref)
    assert held[1] == 1
    desk["book"]["chain"] = _call_book(40.0, 20.0)                    # d 20: the budget would carry 3
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert v["lots"] == 1 and v["requote_sizing"]["by_risk"] == 3
    assert v["requote_sizing"]["reason"] == "kept at the approved lots (below the budget)"
    assert pm._active_shadow_lock(c, LIVE, ref) == held and _events(c, LIVE, "live_entry_resized", ref) == []


@pytest.mark.parametrize("short_bid, opens", [(40.0, True), (35.0, False)])
def test_a_condor_requote_is_judged_on_the_neutral_floor(desk, short_bid, opens):
    """The proposer's own gate, so its own floors: 0.35 for range-bound —
    credit 60 on 200 wings (R:R 0.43) opens; credit 50 (0.33) is refused."""
    c, ref = desk["c"], f"f12n{int(short_bid)}"
    _propose(ref, _condor(60.0, 10.0))
    desk["book"]["chain"] = _condor_book(short_bid, 10.0)
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert (lp.position_state(c, LIVE, ref) == "open") is opens
    if opens:
        assert v["requote_sizing"]["reward_risk"] == 0.4286
    else:
        assert "R:R below 0.35 threshold" in v["reason"] and pm._active_shadow_lock(c, LIVE, ref) is None


# =================================================================== F13 — a held lock still passes the halts

def test_a_halted_live_arm_is_refused_on_its_held_lock_and_releases_it(desk):
    """repro test_lc2_d7_expiry_then_late_approval (halt case) and
    test_lifecycle_live_exposure_and_halt: the ruin halt latched after the
    proposal, and the held lock opened a NEW live position anyway."""
    c, ref = desk["c"], "f13r0001"
    _propose(ref, _bull_call())
    pm.paper_log_event(c, LIVE, pm.HALT_LATCH_EVENT, None, "drawdown 10.40% >= 10% (test latch)")
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == "approved"
    v = out["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected" and v["lots"] == 0
    assert v["reason"] == ("risk-of-ruin halt: drawdown 0.00% >= 10% — all entries blocked — its proposal-time "
                           "lock is held, but a halted account opens no new risk (audit F13); lock released at zero")
    assert pm._active_shadow_lock(c, LIVE, ref) is None and lp.position_state(c, LIVE, ref) is None
    assert _tickets(c, ref, LIVE) == 0
    (ev,) = _events(c, LIVE, "risk_of_ruin_halt", ref)
    assert ev.startswith(f"entry {ref} refused at approval (risk-of-ruin halt")
    assert out["entry"]["accounts"][TWO_L]["status"] == "approved" and _tickets(c, ref, TWO_L) == 1


def test_a_daily_breaker_tripped_on_the_arms_own_settlements_refuses_its_held_lock(desk):
    """repro test_f13_verify_breaker_held_lock_live_opens: LIVE realized
    -4.5% intraday after the proposal; the pending entry still opened."""
    c, ref = desk["c"], "f13b0001"
    _propose(ref, _bull_call())
    assert pm.paper_request_entry(c, LIVE, "old00001", 16764.0, lots=1, primary_lots=1)["approved"]
    pm.paper_release_margin(c, LIVE, "old00001", -9000.0)                 # 4.5% of 2L, today (IST)
    assert pm.paper_daily_breaker_status(c, LIVE)["halted"]
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected" and v["reason"].startswith("daily circuit breaker TRIPPED")
    assert "(audit F13); lock released at zero" in v["reason"]
    assert lp.position_state(c, LIVE, ref) is None and pm._active_shadow_lock(c, LIVE, ref) is None
    assert len(_events(c, LIVE, "daily_breaker_halt", ref)) == 1


def test_a_halted_live_arm_keeps_a_lock_that_backs_a_recorded_position(desk):
    """The held-lock refusal never releases at zero a lock a LIVE row owns
    (a kept-then-repaired position on a still-pending entry)."""
    c, ref = desk["c"], "f13k0001"
    _propose(ref, _bull_call())
    tid, view = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    lp.open_position(c, LIVE, {"short_id": ref, "ticker": "NIFTY 50", "spread": _bull_call()}, view, now=AT)
    pm.paper_log_event(c, LIVE, pm.HALT_LATCH_EVENT, None, "test latch")
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected"
    assert v["reason"].endswith("lock kept (a live position for this entry is recorded (open) — the lock backs it)")
    assert pm._active_shadow_lock(c, LIVE, ref) is not None and lp.position_state(c, LIVE, ref) == "open"
    assert _tickets(c, ref, LIVE) == 1


def test_a_halted_primary_refuses_the_approval_as_margin_blocked_and_leaves_it_pending(desk):
    c, ref = desk["c"], "f13p0001"
    _propose(ref, _bull_call())
    shadows_before = c.execute("SELECT COUNT(*) FROM paper_margin_locks WHERE journal_ref = ? AND "
                               "released_at IS NULL", (ref,)).fetchone()[0]
    pm.latch_halt(c, "test latch")
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == op.MARGIN_BLOCKED and out["status"] in op.APPROVAL_REFUSALS
    assert out["reason"] == ("risk-of-ruin halt: drawdown 0.00% >= 10% — all entries blocked — its "
                             "proposal-time lock is held, but a halted account opens no new risk (audit F13)")
    assert journal.get_entry(ref)["decision"] == "pending_approval"
    assert _primary_lock_active(c, ref) and _tickets(c, ref) == 0           # lock left for the 15:30 sweep
    assert c.execute("SELECT COUNT(*) FROM paper_margin_locks WHERE journal_ref = ? AND released_at IS NULL",
                     (ref,)).fetchone()[0] == shadows_before                 # shadows never judged
    assert lp.position_state(c, LIVE, ref) is None and desk["notes"] == []
    (ev,) = [e for e in _primary_events(c, "risk_of_ruin_halt") if ref in e]
    assert ev.startswith(f"entry {ref} refused at approval (risk-of-ruin halt") and "15:30 sweep" in ev
    pm.clear_halt(c, why="test clear")                                         # the halt clears: it approves
    assert op.decide_pending(ref, approve=True, why="tap", human=True)["status"] == "approved"


def test_a_primary_daily_breaker_refuses_a_held_lock_too(desk):
    c = desk["c"]
    assert pm.request_entry(c, "f13d0001", 1000.0)["reason"] == "margin locked"
    assert pm.request_entry(c, "old0001", 1000.0)["approved"]
    pm.release_margin(c, "old0001", -40000.0)                              # 4% of 10L, today
    v = pm.request_entry(c, "f13d0001", 1000.0)
    assert v["approved"] is False and v["reason"].startswith("daily circuit breaker TRIPPED")
    assert _primary_lock_active(c, "f13d0001")
    assert pm.request_entry(c, "f13d0002", 1000.0)["reason"].startswith("daily circuit breaker")   # new: as before


def test_a_held_shadow_lock_is_re_judged_by_paper_request_entry_too(desk):
    c = desk["c"]
    assert pm.paper_request_entry(c, TWO_L, "f13s0001", 1000.0)["reason"] == "margin locked"
    assert pm.paper_request_entry(c, TWO_L, "f13s0001", 1000.0)["reason"] == pm.HELD_LOCK_REASON
    pm.paper_log_event(c, TWO_L, pm.HALT_LATCH_EVENT, None, "test latch")
    v = pm.paper_request_entry(c, TWO_L, "f13s0001", 1000.0)
    assert v["approved"] is False and v["halt"]["event"] == "risk_of_ruin_halt"
    assert pm._active_shadow_lock(c, TWO_L, "f13s0001") is not None          # left as it is: the caller decides


def _api_post(monkeypatch, trade_id, shared):
    """POST the approval through the real API. The handler runs on a worker
    thread, so every callee opens its OWN connection to the shared file (as
    in production) — the shared one would fail its thread check, and the
    margin gate fails OPEN on any error, which would hide the refusal."""
    from fastapi.testclient import TestClient
    from src.api_server import app
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: _REAL_CONNECT(brain_map.DEFAULT_DB_PATH))
    try:
        return TestClient(app).post("/api/discord/action", headers={"X-API-Key": "k"},
                                    json={"action": "approve", "trade_id": trade_id, "why": "go"})
    finally:
        monkeypatch.setattr(brain_map, "connect", lambda *a, **k: shared)


def test_every_approval_door_handles_the_primary_halt_refusal(desk, monkeypatch, capsys):
    """api 409 + the Discord bot keeps the buttons + auto-approve declines +
    the CLI reports it — all on the REAL gate's refusal text."""
    c, ref = desk["c"], "f13door1"
    _propose(ref, _bull_call())
    pm.latch_halt(c, "test latch")
    r = _api_post(monkeypatch, ref, c)
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["status"] == "margin_blocked" and body["trade_id"] == ref
    assert body["error"].startswith("risk-of-ruin halt") and "(audit F13)" in body["error"]
    from src import discord_bot as bot
    note, retire = bot._decision_note(ref, 409, body)
    assert retire is False and "risk-of-ruin halt" in note and "Not journaled" in note
    auto = op.decide_pending(ref, approve=True, why=op.AUTO_APPROVE_WHY, human=False)
    assert auto["status"] == op.MARGIN_BLOCKED and auto["reason"] == body["error"]
    answers = iter(["y", "go"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert op.review_pending() == 0
    assert f"not decided: margin_blocked ({body['error']})" in capsys.readouterr().out
    assert journal.get_entry(ref)["decision"] == "pending_approval"


def test_run_headless_reports_a_halt_declined_auto_approval_honestly():
    from unittest import mock
    from tests.test_auto_approve import _headless
    why = ("risk-of-ruin halt: drawdown 10.40% >= 10% — all entries blocked — " + pm.HELD_LOCK_HALTED)
    gate = mock.Mock(side_effect=[(True, "margin locked"), (False, why)])
    result, entries, notes = _headless(env={op.AUTO_APPROVE_ENV_KEY: "1"}, gate=gate)
    assert result["auto_approved"] is False and result["reason"] == f"proposed; auto-approval declined ({why})"
    assert entries[0]["decision"] == "pending_approval" and not notes["broadcast"].called


# =================================================================== F14 — the off switch never abandons

class _Stop(BaseException):
    pass


def _wired(monkeypatch):
    """Drive the REAL run_live_loop one iteration; the live_account_fn it wired."""
    seen = {}

    def fake_cycle(*a, **kw):
        seen.update(kw)
        return []

    async def stop(_s):
        raise _Stop()

    monkeypatch.setattr(lb, "live_cycle", fake_cycle)
    monkeypatch.setattr(lb.asyncio, "sleep", stop)
    with pytest.raises(_Stop):
        asyncio.run(lb.run_live_loop(underlyings=("NIFTY 50",), interval=0, notify_fn=None))
    return seen["live_account_fn"]


def _open_live(c, ref, expiry=EXPIRY):
    s = _bull_call(expiry=expiry)
    pm.paper_request_entry(c, LIVE, ref, 17550.0, lots=1, primary_lots=1)
    _, view = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=s)
    lp.open_position(c, LIVE, {"short_id": ref, "ticker": "NIFTY 50", "spread": s}, view, now=AT)


@pytest.mark.parametrize("switch", ["PAPER_2L_LIVE_ACCOUNT_ENABLED", "PAPER_2L_ACCOUNT_ENABLED"])
def test_switching_the_arm_off_still_ticks_what_it_holds_and_says_so_once(desk, monkeypatch, capsys, switch):
    """repro test_f14_verify_switch_off_strands_live (A/B) and
    test_f14_refuter_switch_off_live_arm (1/2): with either switch off the
    loop wired no tick at all."""
    c, cards = desk["c"], desk["cards"]
    _open_live(c, "f14m0001")
    monkeypatch.setattr(pm, switch, False)
    assert _wired(monkeypatch) is lp.tick
    assert "armed MANAGE-ONLY" in capsys.readouterr().out
    (card,) = cards
    assert card["event"] == "live_arm_off_managing" and "f14m0001" in card["description"]
    assert _wired(monkeypatch) is lp.tick                       # a restart the same day: no second card
    assert len(cards) == 1 and len(_events(c, LIVE, lp.EVENT_ARM_OFF)) == 1


def test_an_off_arm_holding_nothing_wires_nothing_as_before(desk, monkeypatch):
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    assert _wired(monkeypatch) is None and desk["cards"] == []
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    assert _wired(monkeypatch) is lp.tick and desk["cards"] == []        # on: the normal arm, no card


def test_an_off_arm_whose_book_cannot_be_read_is_armed_anyway(desk, monkeypatch, capsys):
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)

    def boom(conn):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(lp, "refs_to_manage", boom)
    assert _wired(monkeypatch) is lp.tick
    assert "its book could not be read (disk I/O error) — armed MANAGE-ONLY" in capsys.readouterr().out


def test_the_tick_exits_an_off_arms_position_on_its_own_rules(desk, monkeypatch):
    """Manage-only is the real tick: the switch gates entries at approval,
    never marks or exits — the pre-expiry exit fires as it would armed."""
    c = desk["c"]
    _open_live(c, "f14x0001", expiry="2026-10-22")              # 1 day out on 10-21: inside the window
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    out = lp.tick(now=AT, conn=c, chain_fn=lambda t, x: _call_book(150.0, 60.0), sleep_fn=lambda s: None,
                  now_epoch_fn=lambda: 1000.0)
    (x,) = out["exits"]
    assert x["signal"] == "pre_expiry_exit" and x["status"] == "settled"
    assert lp.position_state(c, LIVE, "f14x0001") == "closed" and pm._active_shadow_lock(c, LIVE, "f14x0001") is None


def test_refs_to_manage_counts_every_live_obligation(desk):
    c = desk["c"]
    _open_live(c, "f14r_open")
    _open_live(c, "f14r_exit")
    c.execute("UPDATE paper_live_positions SET state = 'exiting' WHERE journal_ref = 'f14r_exit'")
    _open_live(c, "f14r_late")                                  # closed, lock not yet settled
    c.execute("UPDATE paper_live_positions SET state = 'closed', pnl_net = 10.0 WHERE journal_ref = 'f14r_late'")
    _open_live(c, "f14r_done")                                  # closed AND settled: nothing left
    c.execute("UPDATE paper_live_positions SET state = 'closed', pnl_net = 5.0 WHERE journal_ref = 'f14r_done'")
    c.commit()
    pm.paper_release_margin(c, LIVE, "f14r_done", 5.0)
    pm.paper_request_entry(c, LIVE, "f14r_fill", 17550.0, lots=1, primary_lots=1)   # filled, no row (F15)
    _filled_live_ticket(c, "f14r_fill", {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    pm.paper_request_entry(c, LIVE, "f14r_pend", 17550.0, lots=1, primary_lots=1)   # a bare pending lock
    assert lp.refs_to_manage(c) == ["f14r_exit", "f14r_fill", "f14r_late", "f14r_open"]


def test_with_the_venue_off_the_live_lock_is_released_at_approval_named(desk, monkeypatch):
    """repro test_f14_verify_switch_off_strands_live (C) / refuter (4): LIVE
    opened nothing and kept its proposal-time lock until the primary exited."""
    c, ref = desk["c"], "f14v0001"
    _propose(ref, _bull_call())
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    assert out["status"] == "approved" and out["entry"]["execution"]["mode"] == "legacy_instant"
    v = out["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected" and "the paper venue is off (paper_venue_enabled false)" in v["reason"]
    assert pm._active_shadow_lock(c, LIVE, ref) is None
    (ev,) = _events(c, LIVE, "live_entry_refused", ref)
    assert ev.endswith("(audit F14) — lock released at zero")
    assert pm._active_shadow_lock(c, TWO_L, ref) is not None                   # the 2L shadow: unchanged
    assert journal.get_entry(ref)["accounts"][LIVE]["status"] == "rejected"


def test_with_the_venue_off_a_lock_backing_a_recorded_position_is_kept(desk, monkeypatch):
    c, ref = desk["c"], "f14v0002"
    _propose(ref, _bull_call())
    _, view = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    lp.open_position(c, LIVE, {"short_id": ref, "ticker": "NIFTY 50", "spread": _bull_call()}, view, now=AT)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    op.decide_pending(ref, approve=True, why="tap", human=True)
    (ev,) = _events(c, LIVE, "live_entry_refused", ref)
    assert "lock kept (a live position for this entry is recorded (open)" in ev
    assert pm._active_shadow_lock(c, LIVE, ref) is not None


# =================================================================== L2 residuals

def _kept_lock_world(c, ref):
    """Pending entry; LIVE's entry ticket FILLED with no row (the F15 sibling
    expire_pending_lock keeps); then a 2L lock — iterated AFTER LIVE's."""
    pm.request_entry(c, ref, 17550.0)
    pm.paper_request_entry(c, LIVE, ref, 17550.0, lots=1, primary_lots=1)
    tid, _ = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    pm.paper_request_entry(c, TWO_L, ref, 17550.0, lots=1, primary_lots=1)
    return tid


def test_a_kept_lock_event_that_rolls_the_transaction_back_expires_nothing(desk):
    """L2 residual (a): the kept-lock event joins expire_pending_lock's ONE
    transaction; an insert that rolls it back (SQLITE_FULL / IOERR, here a
    RAISE(ROLLBACK)) used to be swallowed — the primary's expiry silently
    undone, the 2L expiry then committed on its own."""
    c, ref = desk["c"], "l2ra0001"
    _kept_lock_world(c, ref)
    c.execute("CREATE TEMP TRIGGER l3_ra BEFORE INSERT ON paper_account_events WHEN NEW.event_type = "
              f"'{lp.EVENT_LOCK_KEPT_FILLED}' BEGIN SELECT RAISE(ROLLBACK, 'simulated disk full'); END")
    c.commit()
    with pytest.raises(Exception, match="simulated disk full"):
        pm.expire_pending_lock(c, ref, why="test")
    assert c.in_transaction is False
    assert _primary_lock_active(c, ref) and pm._active_shadow_lock(c, TWO_L, ref) is not None
    assert pm._active_shadow_lock(c, LIVE, ref) is not None
    assert _events(c, TWO_L, pm.PENDING_LOCK_EXPIRED_EVENT, ref) == [] and \
        _primary_events(c, pm.PENDING_LOCK_EXPIRED_EVENT) == []
    c.execute("DROP TRIGGER l3_ra")
    c.commit()
    assert pm.expire_pending_lock(c, ref, why="test") == {PRIMARY: 17550.0, TWO_L: 17550.0}
    assert pm._active_shadow_lock(c, LIVE, ref) is not None


def test_outside_a_transaction_the_event_helper_still_fails_open(desk, capsys):
    c = desk["c"]
    c.execute("CREATE TEMP TRIGGER l3_fo BEFORE INSERT ON paper_account_events WHEN NEW.event_type = 'l3_probe' "
              "BEGIN SELECT RAISE(ABORT, 'simulated busy'); END")
    c.commit()
    assert lp._log_once_a_day(c, {"account_id": LIVE, "journal_ref": "x"}, "l3_probe", "d") is False
    assert "l3_probe event for x skipped: simulated busy" in capsys.readouterr().out
    with pytest.raises(Exception, match="simulated busy"):
        lp._log_once_a_day(c, {"account_id": LIVE, "journal_ref": "x"}, "l3_probe", "d", commit=False)


@pytest.mark.parametrize("state", ["open", "exiting"])
def test_a_re_approval_never_issues_a_second_live_ticket_over_a_recorded_position(desk, state):
    """L2 residual (b): a kept-then-repaired LIVE position on a still-pending
    entry; the owner approves again — a second LIVE entry ticket was issued
    and INSERT OR REPLACE overwrote the row (an exiting row's EXIT tickets
    then counted against the new one)."""
    c, ref = desk["c"], f"l2rb{state[:4]}"
    _propose(ref, _bull_call())
    _, view = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    lp.open_position(c, LIVE, {"short_id": ref, "ticker": "NIFTY 50", "spread": _bull_call()}, view, now=AT)
    c.execute("UPDATE paper_live_positions SET state = ?, exit_ticket_id = 'T-EXIT' WHERE journal_ref = ?",
              (state, ref))
    c.commit()
    before = dict(lp.positions(c)[0])
    out = op.decide_pending(ref, approve=True, why="tap", human=True)
    v = out["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected"
    assert v["reason"] == (f"live entry refused: a live position for this entry is already recorded ({state}) — "
                           "a ref is opened once; no second entry ticket")
    assert _tickets(c, ref, LIVE) == 1                                        # the original fill only
    assert dict(lp.positions(c)[0]) == before and pm._active_shadow_lock(c, LIVE, ref) is not None
    (ev,) = _events(c, LIVE, "live_entry_refused", ref)
    assert f"lock kept (a live position for this entry is recorded ({state}) — the lock backs it)" in ev


def test_a_re_approval_never_issues_a_second_ticket_over_a_filled_entry_awaiting_repair(desk, monkeypatch):
    """The F15 state (entry ticket FILLED, row not yet repaired) is 'opened'
    too: a re-approval — even one that finds the arm halted — issues no
    second LIVE ticket and never releases the filled ticket's lock at zero;
    the next tick's repair then opens the ONE position from that ticket."""
    c, ref = desk["c"], "l2rbfill"
    _propose(ref, _bull_call())
    tid, _ = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected" and v["reason"] == (
        f"live entry refused: entry ticket {tid} for this entry already FILLED (its position row is repaired "
        "from that ticket by the next live tick) — a ref is opened once; no second entry ticket")
    assert _tickets(c, ref, LIVE) == 1 and pm._active_shadow_lock(c, LIVE, ref) is not None
    (ev,) = _events(c, LIVE, "live_entry_refused", ref)
    assert f"lock kept (entry ticket {tid} FILLED but no live position row was recorded" in ev
    out = lp.tick(now=AT, conn=c, chain_fn=lambda t, x: None, sleep_fn=lambda s: None, now_epoch_fn=lambda: 1.0)
    assert out["repaired"] == [ref] and lp.position_state(c, LIVE, ref) == "open"


def test_a_halted_live_arms_filled_entry_lock_is_kept_not_released_at_zero(desk):
    """F13's release goes through release_unopened_lock: a FILLED entry
    ticket with no row yet (F15) keeps its lock, named."""
    c, ref = desk["c"], "f13fill1"
    _propose(ref, _bull_call())
    tid, _ = _filled_live_ticket(c, ref, {(24000.0, "CE"): 100.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    pm.paper_log_event(c, LIVE, pm.HALT_LATCH_EVENT, None, "test latch")
    v = op.decide_pending(ref, approve=True, why="tap", human=True)["entry"]["accounts"][LIVE]
    assert v["status"] == "rejected" and "(audit F13); lock kept (entry ticket" in v["reason"]
    assert pm._active_shadow_lock(c, LIVE, ref) is not None
    assert len(_events(c, LIVE, lp.EVENT_LOCK_KEPT_FILLED, ref)) == 1


def test_a_lock_is_only_ever_shrunk_and_only_while_active(desk):
    c = desk["c"]
    assert pm.paper_request_entry(c, LIVE, "f12s0001", 30000.0, lots=3, primary_lots=1)["approved"]
    assert pm.paper_resize_lock(c, LIVE, "f12s0001", 3, 30000.0) is False          # not smaller: untouched
    assert pm.paper_resize_lock(c, LIVE, "f12s0001", 5, 50000.0) is False          # never grows
    assert pm._active_shadow_lock(c, LIVE, "f12s0001") == (30000.0, 3)
    assert pm.paper_resize_lock(c, LIVE, "f12s0001", 1, 10000.0) is True
    assert pm._active_shadow_lock(c, LIVE, "f12s0001") == (10000.0, 1)
    pm.paper_release_margin(c, LIVE, "f12s0001", 0.0)
    assert pm.paper_resize_lock(c, LIVE, "f12s0001", 0, 0.0) is False              # a released lock: never


def test_open_position_refuses_to_overwrite_any_row(desk):
    c, ref = desk["c"], "l2rbrow1"
    _open_live(c, ref)
    c.execute("UPDATE paper_live_positions SET state = 'closed', pnl_net = 123.0 WHERE journal_ref = ?", (ref,))
    c.commit()
    _, view = _filled_live_ticket(c, ref, {(24000.0, "CE"): 90.0, (24200.0, "CE"): 30.0}, spread=_bull_call())
    with pytest.raises(lp.LiveEntryRefused, match=r"already recorded \(closed\)"):
        lp.open_position(c, LIVE, {"short_id": ref, "ticker": "NIFTY 50", "spread": _bull_call()}, view, now=AT)
    row = lp.positions(c)[0]
    assert row["state"] == "closed" and row["pnl_net"] == 123.0 and row["entry_mark_ps"] == 70.0


def test_an_impossible_mark_hold_is_memoised_against_its_snapshot(desk, monkeypatch):
    """L2 residual (c): a held_impossible_mark is a PRICE hold — the same
    predicate on the same cached snapshot is not re-fetched to be refused
    again (HELD_ON_PRICE)."""
    c = desk["c"]
    _open_live(c, "l2rc0001", expiry="2026-10-22")
    calls = []
    fn = lambda t, x: calls.append(x) or _call_book(150.0, 60.0)
    monkeypatch.setattr(lp, "_exit", lambda conn, row, prices, res, now, **k: {
        "status": "held_impossible_mark", "journal_ref": row["journal_ref"], "reason": "impossible mark (test)"})
    first = lp.tick(now=AT, conn=c, chain_fn=fn, sleep_fn=lambda s: None, now_epoch_fn=lambda: 5000.0,
                    interval_s=300)
    assert first["exits"][0]["status"] == "held_impossible_mark" and len(calls) == 1
    second = lp.tick(now=AT.replace(minute=1), conn=c, chain_fn=fn, sleep_fn=lambda s: None,
                     now_epoch_fn=lambda: 5060.0, interval_s=300)
    (x,) = second["exits"]
    assert x["status"] == "held_impossible_mark" and x["held_on_cached_chain"] is True and len(calls) == 1
    assert len(_events(c, LIVE, lp.EVENT_EXIT_HELD, "l2rc0001")) == 1


def test_a_closed_row_with_its_lock_active_is_named_as_such(desk):
    """L2 residual (c): the kept-lock text said 'no live position row was
    recorded' for a CLOSED row whose lock had not settled yet."""
    c, ref = desk["c"], "l2rc0002"
    pm.request_entry(c, ref, 17550.0)
    _open_live(c, ref)
    c.execute("UPDATE paper_live_positions SET state = 'closed', pnl_net = 812.5 WHERE journal_ref = ?", (ref,))
    c.commit()
    pm.expire_pending_lock(c, ref, why="test")
    (ev,) = _events(c, LIVE, lp.EVENT_LOCK_KEPT_FILLED, ref)
    assert "no live position row was recorded" not in ev
    assert ev.endswith("its live position row is CLOSED (settled pnl Rs.812.50) but its lock is still active — "
                       "lock kept; the next live tick releases it at that pnl")
    late = lp._repair_late_locks(c, LIVE)                                  # ... and that is what happens
    assert late == [ref] and pm._active_shadow_lock(c, LIVE, ref) is None

"""#182 LONG_CALL / LONG_PUT — Shadow Learner only, Rs.15k premium cap, long-option exit latches.
Hermetic: ':memory:' DBs, canned chains and re-quotes, no network."""
import json
from datetime import date, datetime

import pytest

from src import brain_map, long_options as lo, oms, options_proposer as op, portfolio_manager as pm
from src import shadow_learner as sl, strategy_router as sr
from src.execution import live_pricer as lp
from src.execution import paper_venue as pv
from src.strategy import StrategyConstructor

LEARNER = sl.ACCOUNT


def _bear_put(lot=65, buy_prem=180.0):
    s = StrategyConstructor(vix=13.0, lot_size=lot).construct_bear_put_spread(24000, 23800, buy_prem, 60.0)
    s.update(lots=1, expiry="2026-11-27", entry_spot=24010.0)
    return s


def _chain(quotes: dict) -> dict:
    oc = {}
    for (k, t), (bid, ask, ltp) in quotes.items():
        oc.setdefault(f"{float(k):.6f}", {})[t.lower()] = {"top_bid_price": bid, "top_ask_price": ask,
                                                           "last_price": ltp}
    return {"last_price": 24000.0, "oc": oc}


# ------------------------------------------------------------- building + routing

def test_from_spread_takes_the_buy_leg_of_a_directional_spread():
    put = lo.from_spread(_bear_put())
    assert put["strategy"] == lo.LONG_PUT and put["direction"] == "bearish"
    assert put["legs"] == [dict(_bear_put()["legs"][0], side="BUY", option_type="PE")]
    assert put["legs"][0]["strike"] == 24000 and put["net_debit"] == 180.0
    assert put["max_loss"] == 180.0 * 65 and put["margin"]["total_margin"] == 180.0 * 65
    assert put["expiry"] == "2026-11-27" and put["entry_spot"] == 24010.0
    call = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 100.0, 30.0)
    assert lo.from_spread(dict(call, expiry="2026-11-27"))["strategy"] == lo.LONG_CALL
    condor = StrategyConstructor(vix=13.0, lot_size=65).construct_iron_condor(23500, 24500, 200, 30.0, 10.0, 30.0, 10.0)
    assert lo.from_spread(condor) is None


def test_routing_knows_long_options_and_only_the_learner_may_ticket_one():
    assert sr.premium_of(lo.LONG_CALL) == "debit" and sr.direction_of(lo.LONG_PUT) == "bearish"
    assert sr.STRUCTURE_FOR_VIEW["bullish"] == "bull_call_spread"          # the primary never routes to one
    assert lo.refusal_for_account(lo.LONG_CALL, LEARNER) is None
    for acct in (oms.PRIMARY_ACCOUNT, pm.ACCOUNT_PAPER_2L_LIVE, pm.ACCOUNT_PAPER_2L):
        assert "Shadow Learner archetype only" in lo.refusal_for_account(lo.LONG_PUT, acct)
    c = brain_map.connect(":memory:")
    oms.ensure_schema(c)
    prop = {"ticker": "NIFTY 50", "short_id": "lo0001", "spread": lo.from_spread(_bear_put())}
    with pytest.raises(ValueError, match="Shadow Learner archetype only"):
        sr.issue(c, prop, journal_ref="lo0001", account_id=pm.ACCOUNT_PAPER_2L_LIVE, lots=1)
    assert sr.issue(c, prop, journal_ref="lo0001", account_id=LEARNER, lots=1)["ticket_id"]


def test_bounds_and_the_live_requote_accept_a_single_long_leg():
    put = lo.from_spread(_bear_put())
    assert lp.structure_bounds(put, 182.0) == {"width_ps": 0.0, "max_profit_ps": 23818.0, "max_loss_ps": 182.0}
    entry = {"ticker": "NIFTY 50", "spread": put}
    rq = lp.requote_entry(entry, now=datetime(2026, 11, 6, 11, 0),
                          chain_fn=lambda t, e: _chain({(24000, "PE"): (178.0, 182.0, 180.0)}))
    assert rq["ok"] and rq["entry_mark_ps"] == 182.0 and rq["max_loss_ps"] == 182.0


# ------------------------------------------------------------- the Rs.15k cap

@pytest.mark.parametrize("prem,lot,asked,lots", [
    (100.0, 65, 5, 2),          # Rs.6,500 a lot: two fit under Rs.15,000
    (230.0, 65, 1, 1),          # Rs.14,950: exactly under
    (231.0, 65, 1, 0),          # Rs.15,015: one lot is over the cap
    (0.0, 65, 1, 0),
])
def test_cap_lots(prem, lot, asked, lots):
    assert lo.cap_lots(prem, lot, asked) == lots
    if lots:
        assert lo.premium_paid_rs(prem, lot, lots) <= lo.MAX_PREMIUM_RS


@pytest.fixture
def world(monkeypatch):
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    oms.ensure_schema(c)
    lp.ensure_schema(c)
    pm.get_account(c)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.0)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    monkeypatch.setattr(sl, "record_fire", lambda conn, entry, fire_date=None: {"created": True})
    yield c
    c.close()


def _entry(spread, ref="lo0001"):
    return {"short_id": ref, "date": "2026-11-06", "ticker": "NIFTY 50", "action": "SPREAD",
            "decision": "rejected", "outcome": None, "spread": spread, "signal": "t", "lots": 1}


def _requote(prem):
    return lambda conn, acct, entry, prefetched=None: {
        "ok": True, "legs": [dict(entry["spread"]["legs"][0], premium=prem, fill_basis="live_crossed")],
        "quote_ts": "2026-11-06T11:00:00", "entry_mark_ps": prem,
        **lo.bounds(entry["spread"], prem)}


def _locks(c):
    return c.execute("SELECT journal_ref, margin_rs, lots, released_at FROM paper_margin_locks").fetchall()


def test_over_the_cap_at_proposal_is_refused_before_any_lock(world, monkeypatch):
    monkeypatch.setattr(op, "_live_requote", lambda *a, **k: pytest.fail("no re-quote past the cap"))
    entry = _entry(lo.from_spread(_bear_put(buy_prem=240.0)))           # Rs.15,600 a lot
    v = sl.take(entry, "t", conn=world)
    assert v["status"] == "rejected" and v["reason"].startswith("premium cap: 1 lot costs Rs.15,600.00")
    assert _locks(world) == []


def test_under_the_cap_opens_one_live_long_put(world, monkeypatch):
    monkeypatch.setattr(op, "_live_requote", _requote(182.0))
    monkeypatch.setattr(op, "_live_entry_rules", lambda conn, acct, entry, rq, lots: lots)
    entry = _entry(lo.from_spread(_bear_put(buy_prem=180.0)))
    v = sl.take(entry, "t", conn=world)
    assert v["status"] == "approved" and v["venue_status"] == oms.FILLED, v
    row = world.execute("SELECT strategy, entry_mark_ps, max_loss_ps, width_ps, lots, legs_json "
                        "FROM paper_live_positions WHERE account_id = ?", (LEARNER,)).fetchone()
    assert row[0] == lo.LONG_PUT and row[1] == 182.0 and row[2] == 182.0 and row[3] == 0.0 and row[4] == 1
    assert [l["side"] for l in json.loads(row[5])] == ["BUY"]
    assert v["premium_paid_rs"] == 182.0 * 65 <= lo.MAX_PREMIUM_RS


def test_a_crossed_requote_over_the_cap_is_refused_and_its_lock_released(world, monkeypatch):
    monkeypatch.setattr(op, "_live_requote", _requote(235.0))           # proposal 220 fits, the ask does not
    monkeypatch.setattr(op, "_live_entry_rules", lambda conn, acct, entry, rq, lots: lots)
    entry = _entry(lo.from_spread(_bear_put(buy_prem=220.0)))
    v = sl.take(entry, "t", conn=world)
    assert v["status"] == "rejected" and "premium cap" in v["reason"]
    assert [r[3] is not None for r in _locks(world)] == [True]           # the lock taken at proposal is released
    assert world.execute("SELECT COUNT(*) FROM paper_live_positions").fetchone()[0] == 0


def test_another_account_is_refused_at_the_door(world):
    v = sl.take(_entry(lo.from_spread(_bear_put())), "t", conn=world, account=pm.ACCOUNT_PAPER_2L_LIVE_GEX)
    assert v["status"] == "rejected" and "Shadow Learner archetype only" in v["reason"]


# ------------------------------------------------------------- exits

def test_trail_ladder_only_rises():
    assert lo.lock_for(None) is None and lo.lock_for(49.9) is None
    assert lo.lock_for(50) == 0.0 and lo.lock_for(150) == 50.0 and lo.lock_for(250) == 120.0
    assert lo.lock_for(10, lock_pct=120.0) == 120.0


@pytest.mark.parametrize("gain,lock,today,signal", [
    (-50.0, None, date(2026, 11, 7), "premium_stop"),
    (-49.9, None, date(2026, 11, 7), "hold"),
    (40.0, 50.0, date(2026, 11, 7), "trail_hit"),
    (60.0, 50.0, date(2026, 11, 7), "hold"),
    (10.0, None, date(2026, 11, 17), "theta_time_stop"),     # 11 of 21 days gone, under +20%
    (25.0, None, date(2026, 11, 17), "hold"),                 # the move came: theta waits
    (10.0, None, date(2026, 11, 15), "hold"),                 # 9 of 21 days
])
def test_exit_signal(gain, lock, today, signal):
    assert lo.exit_signal(gain, lock, date(2026, 11, 6), date(2026, 11, 27), today) == signal


def _row(**kw):
    r = {"account_id": LEARNER, "journal_ref": "lo0001", "ticker": "NIFTY 50", "strategy": lo.LONG_PUT,
         "direction": "bearish", "expiry": "2026-11-27", "lots": 1, "lot_size": 65,
         "legs": [{"side": "BUY", "option_type": "PE", "strike": 24000.0, "entry_fill": 100.0}],
         "entry_mark_ps": 100.0, "width_ps": 0.0, "max_profit_ps": 23900.0, "max_loss_ps": 100.0,
         "opened_at": "2026-11-06T10:00:00", "entry_quote_ts": None,
         "ratchet_peak_pct": None, "ratchet_lock_pct": None}
    r.update(kw)
    return r


def _put_chain(bid):
    return _chain({(24000, "PE"): (bid, bid + 1.0, bid + 0.5)})


NOW = datetime(2026, 11, 9, 11, 0)


def test_evaluate_premium_stop_and_hold():
    ev = lp.evaluate(_row(), _put_chain(50.0), NOW.date(), now=NOW)
    assert ev["ok"] and ev["signal"] == "premium_stop" and ev["capture_pct"] == -50.0
    assert ev["profit_ps"] == -50.0
    assert lp.evaluate(_row(), _put_chain(110.0), NOW.date(), now=NOW)["signal"] == "hold"


def test_evaluate_trail_arms_on_two_fetches_then_fires():
    row = _row()
    ev = lp.evaluate(row, _put_chain(160.0), NOW.date(), now=NOW)       # +60% → rung (50, lock 0) seen once
    assert ev["signal"] == "hold" and ev["lock"] is None and ev["unconfirmed"]["lock"] == 0.0
    src1, src2 = object(), object()
    assert lp._confirm_rung(row, ev, src1)["rung"] == "pending"
    ev2 = lp._confirm_rung(row, lp.evaluate(row, _put_chain(155.0), NOW.date(), now=NOW), src2)
    assert ev2["rung"] == "confirmed" and ev2["lock"] == 0.0 and ev2["peak"] == 55.0
    armed = _row(ratchet_peak_pct=ev2["peak"], ratchet_lock_pct=ev2["lock"])
    assert lp.evaluate(armed, _put_chain(99.0), NOW.date(), now=NOW)["signal"] == "trail_hit"
    assert lp.evaluate(armed, _put_chain(120.0), NOW.date(), now=NOW)["signal"] == "hold"
    lp._PENDING_RUNG.clear()


def test_evaluate_theta_and_the_shared_pre_expiry_window():
    late = datetime(2026, 11, 18, 11, 0)
    assert lp.evaluate(_row(), _put_chain(105.0), late.date(), now=late)["signal"] == "theta_time_stop"
    eve = datetime(2026, 11, 25, 11, 0)
    assert lp.evaluate(_row(), _put_chain(200.0), eve.date(), now=eve)["signal"] == "pre_expiry_exit"


def test_a_spread_row_still_takes_the_ratchet_path():
    row = _row(strategy="bear_put_spread", width_ps=200.0, max_profit_ps=100.0, max_loss_ps=100.0,
               legs=[{"side": "BUY", "option_type": "PE", "strike": 24000.0},
                     {"side": "SELL", "option_type": "PE", "strike": 23800.0}])
    chain = _chain({(24000, "PE"): (150.0, 151.0, 150.5), (23800, "PE"): (60.0, 61.0, 60.5)})
    ev = lp.evaluate(row, chain, NOW.date(), now=NOW)
    assert ev["ok"] and ev["signal"] == "hold" and ev["capture_pct"] == pytest.approx(-11.0)


# ------------------------------------------------------------- the proposer hook

def test_hook_builds_a_long_put_from_this_cycles_bear_put_and_journals_only_an_open(monkeypatch):
    monkeypatch.setattr("src.confluence.evidence.stamp_earnings", lambda e: None)
    taken, logged = [], []
    result = {"proposal": {"spread": _bear_put()}, "view": "bearish", "vix": 14.0}
    v = op._learner_long_option_entry("NIFTY 50", result, ready_fn=lambda u, strategy=None: (True, "ready"),
                                      take_fn=lambda e, why: taken.append(e) or {"status": "approved"},
                                      log_fn=logged.append)
    assert v == {"status": "approved"}
    e = taken[0]
    assert e["spread"]["strategy"] == lo.LONG_PUT and e["decision"] == "rejected" and e["learner_only"]
    assert logged == [e]
    v = op._learner_long_option_entry("NIFTY 50", result, ready_fn=lambda u, strategy=None: (True, "ready"),
                                      take_fn=lambda e, why: {"status": "rejected", "reason": "premium cap"},
                                      log_fn=logged.append)
    assert v["status"] == "rejected" and len(logged) == 1
    skipped = op._learner_long_option_entry("NIFTY 50", result, ready_fn=lambda u, strategy=None: (False, "taken"),
                                            take_fn=lambda e, why: pytest.fail("not ready"), log_fn=logged.append)
    assert skipped == {"status": "skipped", "reason": "taken"}
    assert op._learner_long_option_entry("NIFTY 50", {"proposal": None, "reason": "no chain"}) is None

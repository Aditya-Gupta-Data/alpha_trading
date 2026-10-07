"""
PAPER_2L_LIVE — the live-quote arm (decision #120, 2026-09-29).

A fourth paper account whose marks and exits are priced only on live
intra-day chain quotes with the bid/ask crossed, settling ITSELF. Hermetic:
sqlite ':memory:', chains injected, the venue's tier slippage patched, the
market clock injected — no network, no token.
"""
import json
from datetime import date, datetime
from pathlib import Path

import sqlite3

import pytest

from src import brain_map, oms, plan_tracker as pt, portfolio_manager as pm
from src.execution import live_pricer as lp, paper_venue as pv
from src.strategy import StrategyConstructor

ROOT = Path(__file__).resolve().parents[1]
LIVE, TWO_L = pm.ACCOUNT_PAPER_2L_LIVE, pm.ACCOUNT_PAPER_2L
OPEN = datetime(2026, 9, 29, 11, 0)          # a Tuesday, market open


def _bull_call(basis="quoted"):
    # BUY 24000 CE @ ask 100 / SELL 24200 CE @ bid 30 -> debit 70, width 200, lot 65
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_bull_call_spread(24000, 24200, 100.0, 30.0)
    for l in s["legs"]:
        l["fill_basis"] = basis
    s.update(lots=1, expiry="2026-10-28", entry_spot=24000.0)
    return s


def _condor():
    # SELL 23500 PE / BUY 23300 PE, SELL 24500 CE / BUY 24700 CE; credit 40, wings 200
    s = StrategyConstructor(vix=13.0, lot_size=65).construct_iron_condor(
        23500, 24500, 200, 30.0, 10.0, 30.0, 10.0)
    for l in s["legs"]:
        l["fill_basis"] = "quoted"
    s.update(lots=1, expiry="2026-10-28", entry_spot=24000.0)
    return s


def _chain(quotes: dict) -> dict:
    """{(strike, 'CE'|'PE'): (bid, ask, ltp)} -> a Dhan-shaped chain."""
    oc = {}
    for (k, t), (bid, ask, ltp) in quotes.items():
        node = oc.setdefault(f"{float(k):.6f}", {})
        node[t.lower()] = {"top_bid_price": bid, "top_ask_price": ask, "last_price": ltp}
    return {"last_price": 24000.0, "oc": oc}


BULL_ENTRY = _chain({(24000, "CE"): (98.0, 100.0, 99.0), (24200, "CE"): (30.0, 32.0, 31.0)})


def _entry(ref="lv0001", spread=None):
    return {"short_id": ref, "date": "2026-09-29", "ticker": "NIFTY 50", "action": "SPREAD",
            "decision": "approved", "outcome": None, "spread": spread or _bull_call(), "signal": "t",
            "accounts": {TWO_L: {"status": "approved", "lots": 1, "margin_rs": 17550.0},
                         LIVE: {"status": "approved", "lots": 1, "margin_rs": 17550.0}}}


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
    monkeypatch.setattr(pv, "_tier_frac", lambda u, slippage_fn=None: 0.01)      # a visible 1% tier slip
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_now", lambda: OPEN)
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: None)
    lp.reset_cache()
    yield c
    lp.reset_cache()
    c.close()


def _open(c, ref="lv0001", spread=None, chain=BULL_ENTRY, primary_lock=True):
    """Lock margin for 2L + LIVE and open LIVE's position through the real
    entry path (requote -> ticket -> venue -> open_position)."""
    e = _entry(ref, spread)
    if primary_lock:
        pm.request_entry(c, ref, 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, ref, 17550.0, lots=1, primary_lots=1)
    rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: chain)
    assert rq["ok"], rq
    from src import strategy_router as sr
    prop = {"ticker": e["ticker"], "short_id": ref, "signal": "t",
            "spread": dict(e["spread"], legs=rq["legs"])}
    issued = sr.issue(c, prop, journal_ref=ref, source="t", account_id=LIVE, lots=1)
    pv.sweep(c, stamp=False)
    view = oms.ticket_view(c, issued["ticket_id"])
    assert view["status"] == oms.FILLED
    opened = lp.open_position(c, LIVE, e, view, quote_ts=rq["quote_ts"], now=OPEN)
    return e, view, opened


# ------------------------------------------------------------- registry

def test_the_live_arm_rides_both_switches(world, monkeypatch):
    assert LIVE in pm.shadow_account_ids() and LIVE in pm.LIVE_ACCOUNTS
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    assert LIVE not in pm.shadow_account_ids()
    assert pm.get_paper_account(world, LIVE)["starting_capital"] == 200000.0


def test_proposal_time_refusal_when_a_leg_had_no_real_quote(world):
    c = world
    p = {"ticker": "NIFTY 50", "short_id": "lv0009", "spread": _bull_call(basis="ltp"), "lots": 1, "vix": 13.0}
    out = pm.evaluate_shadow_accounts("lv0009", p, conn=c)
    assert out[LIVE]["status"] == "rejected" and "no live bid/ask" in out[LIVE]["reason"]
    assert out[TWO_L]["status"] == "approved"                                   # 2L does not care
    assert pm._active_shadow_lock(c, LIVE, "lv0009") is None


# ------------------------------------------------------------- entry

def test_requote_crosses_the_spread_and_refuses_when_it_cannot(world, monkeypatch):
    e = _entry()
    rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: BULL_ENTRY)
    assert rq["ok"] and [l["premium"] for l in rq["legs"]] == [100.0, 30.0]      # BUY at ask, SELL at bid
    assert all(l["fill_basis"] == "live_crossed" for l in rq["legs"]) and rq["quote_ts"] == "2026-09-29T11:00:00"
    monkeypatch.setattr(lp, "_market_open", lambda now: False)
    assert lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: BULL_ENTRY)["reason"] == "market closed at approval"
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    no_bid = _chain({(24000, "CE"): (98.0, 100.0, 99.0), (24200, "CE"): (None, 32.0, 31.0)})
    assert "no bid for the SELL leg" in lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: no_bid)["reason"]
    assert lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: None)["reason"] == "option chain unavailable"
    stale = _chain({(24000, "CE"): (98.0, 200.0, 99.0), (24200, "CE"): (30.0, 32.0, 31.0)})
    assert ">50% off last" in lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: stale)["reason"]


def test_entry_fills_at_the_crossed_limits_with_zero_tier_slippage(world):
    c = world
    e, view, opened = _open(c)
    fills = {(float(l["strike"]), l["side"]): (l["avg_fill_price"], l["fill_basis"]) for l in view["legs"]}
    assert fills[(24000.0, "BUY")] == (100.0, "live_crossed") and fills[(24200.0, "SELL")] == (30.0, "live_crossed")
    assert opened["entry_mark_ps"] == 70.0 and opened["width_ps"] == 200.0
    assert opened["max_loss_ps"] == 70.0 and opened["max_profit_ps"] == 130.0
    row = lp.open_rows(c, LIVE)[0]
    assert row["state"] == "open" and row["entry_quote_ts"] == "2026-09-29T11:00:00"
    # the 2L account's own ticket, by contrast, still pays the tier slip
    from src import strategy_router as sr
    t2 = sr.issue(c, {"ticker": "NIFTY 50", "short_id": "lv0001", "signal": "t", "spread": e["spread"]},
                  journal_ref="lv0001", source="t", account_id=TWO_L, lots=1)
    pv.sweep(c, stamp=False)
    v2 = oms.ticket_view(c, t2["ticket_id"])
    assert {l["avg_fill_price"] for l in v2["legs"]} == {101.0, 29.7}


def test_credit_structure_bounds_from_the_actual_crossed_credit(world):
    c = world
    condor = _condor()
    chain = _chain({(23500, "PE"): (30.0, 32.0, 31.0), (23300, "PE"): (9.0, 10.0, 9.5),
                    (24500, "CE"): (30.0, 32.0, 31.0), (24700, "CE"): (9.0, 10.0, 9.5)})
    e, view, opened = _open(c, "lv0002", condor, chain)
    assert opened["entry_mark_ps"] == -40.0                        # credit: short at bid 30+30, long at ask 10+10
    assert opened["max_profit_ps"] == 40.0 and opened["max_loss_ps"] == 160.0 and opened["width_ps"] == 200.0


def test_execute_paper_entry_requotes_the_live_arm_and_refuses_when_closed(world, monkeypatch):
    from src import options_proposer as op
    c = world
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    monkeypatch.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: BULL_ENTRY)
    e = _entry("lv0003")
    pm.request_entry(c, "lv0003", 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, "lv0003", 17550.0, lots=1, primary_lots=1)
    rec = op._execute_paper_entry(e)
    assert rec["mode"] == "paper_venue" and rec["accounts"][LIVE]["status"] == oms.FILLED
    assert e["accounts"][LIVE]["entry_quote_ts"] == "2026-09-29T11:00:00"
    assert lp.has_open_position(c, LIVE, "lv0003")
    assert c.execute("SELECT COUNT(*) FROM trade_tickets").fetchone()[0] == 3    # 10L + 2L + LIVE
    # market closed at approval: refused, lock released at zero, no LIVE ticket
    monkeypatch.setattr(lp, "_market_open", lambda now: False)
    e2 = _entry("lv0004")
    pm.request_entry(c, "lv0004", 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, "lv0004", 17550.0, lots=1, primary_lots=1)
    rec = op._execute_paper_entry(e2)
    assert e2["accounts"][LIVE]["status"] == "rejected" and "market closed" in e2["accounts"][LIVE]["reason"]
    assert pm._active_shadow_lock(c, LIVE, "lv0004") is None and pm._active_shadow_lock(c, TWO_L, "lv0004") is not None
    assert c.execute("SELECT COUNT(*) FROM trade_tickets WHERE account_id = ?", (LIVE,)).fetchone()[0] == 1
    ev = c.execute("SELECT detail FROM paper_account_events WHERE account_id = ? AND event_type = 'live_entry_refused'",
                   (LIVE,)).fetchone()[0]
    assert "market closed" in ev and "released at zero" in ev


def _door_answers_nothing(monkeypatch, why="rate limit (DH-904: Too many requests)"):
    """Reach the REAL default door (un-muzzled) with its Dhan call faked to
    return no chain and name `why` — no network, no token."""
    from src import dhan_client as dc
    monkeypatch.setattr(lp, "_is_test_env", lambda: False)
    monkeypatch.setattr(dc, "get_option_chain", lambda t, x: None)
    monkeypatch.setattr(dc, "last_chain_error", lambda: why)


def test_a_no_chain_refusal_names_the_doors_reason_in_the_log_and_the_event(world, monkeypatch, capsys):
    # 2026-10-01: ICICIBANK 1c0d04d0 was refused "option chain unavailable"
    # with nothing to say why. The door's reason now rides the refusal.
    from src import options_proposer as op
    c = world
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    _door_answers_nothing(monkeypatch)
    e = _entry("lv0005")
    pm.request_entry(c, "lv0005", 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, "lv0005", 17550.0, lots=1, primary_lots=1)
    rq = lp.requote_entry(e, now=OPEN)                     # the prefetch: a dict, never a raise
    assert not rq["ok"] and rq["reason"] == "option chain unavailable (rate limit (DH-904: Too many requests))"
    op._execute_paper_entry(e, live_requote=rq)
    assert "REFUSED at approval — option chain unavailable (rate limit (DH-904: Too many requests))" \
        in capsys.readouterr().out
    ev = c.execute("SELECT detail FROM paper_account_events WHERE account_id = ? AND event_type = 'live_entry_refused'",
                   (LIVE,)).fetchone()[0]
    assert ev == "option chain unavailable (rate limit (DH-904: Too many requests)) — lock released at zero"
    assert pm._active_shadow_lock(c, LIVE, "lv0005") is None and pm._active_shadow_lock(c, TWO_L, "lv0005") is not None


def test_a_failed_tick_fetch_is_logged_with_the_doors_reason(world, monkeypatch, capsys):
    c = world
    _open(c)
    lp.reset_cache()
    _door_answers_nothing(monkeypatch, why="transport error: reset by peer")
    clock = {"t": 1000.0}
    t = lp.tick(now=OPEN, conn=c, sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
                now_epoch_fn=lambda: clock["t"])
    assert t["fetched"] == 1 and t["marked"] == 0 and t["abstained"] == 1
    assert "(live account: chain NIFTY 50 2026-10-28 failed: transport error: reset by peer)" \
        in capsys.readouterr().out


def test_each_shadow_lock_prints_approved_once_not_again_at_approval(world, monkeypatch, capsys):
    # 2026-10-01 log: every shadow account printed "approved 1 lot(s)" twice
    # per trade — once when its lock was taken at proposal, again when
    # approval re-confirmed the same lock. One lock, one line.
    from src import options_proposer as op
    c = world
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    p = {"spread": _bull_call(), "lots": 1, "vix": 13.0}
    first = op._judge_shadow_accounts("lv0006", p)                       # proposal: locks taken
    second = op._judge_shadow_accounts("lv0006", p, allow_rotation=True)  # approval: same locks held
    assert {a: v["status"] for a, v in first.items()} == {TWO_L: "approved", LIVE: "approved"}
    assert {a: (v["status"], v["reason"]) for a, v in second.items()} == {
        TWO_L: ("approved", pm.HELD_LOCK_REASON), LIVE: ("approved", pm.HELD_LOCK_REASON)}
    out = capsys.readouterr().out
    for acct in (TWO_L, LIVE):
        assert out.count(f"[{acct}] lv0006: approved") == 1, out


# ------------------------------------------------------------- marks

def test_marks_cross_the_spread_abstain_on_bad_quotes_and_tolerate_a_zero_bid_long(world):
    c = world
    e, view, opened = _open(c)
    row = lp.open_rows(c, LIVE)[0]
    # close now: sell the long at bid 120, buy the short back at ask 35 -> mark 85, profit 15 of 130
    ev = lp.evaluate(row, _chain({(24000, "CE"): (120.0, 122.0, 121.0), (24200, "CE"): (33.0, 35.0, 34.0)}),
                     date(2026, 9, 29))
    assert ev["ok"] and ev["mark_ps"] == 85.0 and ev["profit_ps"] == 15.0
    assert ev["capture_pct"] == pytest.approx(15 / 130 * 100, abs=0.01) and ev["signal"] == "hold"
    # short leg with no ask -> abstain
    ev = lp.evaluate(row, _chain({(24000, "CE"): (120.0, 122.0, 121.0), (24200, "CE"): (33.0, None, 34.0)}), date(2026, 9, 29))
    assert not ev["ok"] and "no ask" in ev["reason"]
    # crossed book -> abstain; quote >50% off last -> abstain
    ev = lp.evaluate(row, _chain({(24000, "CE"): (125.0, 122.0, 121.0), (24200, "CE"): (33.0, 35.0, 34.0)}), date(2026, 9, 29))
    assert not ev["ok"] and "crossed" in ev["reason"]
    ev = lp.evaluate(row, _chain({(24000, "CE"): (0.05, 122.0, 121.0), (24200, "CE"): (33.0, 35.0, 34.0)}), date(2026, 9, 29))
    assert not ev["ok"] and ">50% off" in ev["reason"]
    # long leg with NO bid is worth 0 — the structure still marks (and clamps at -max_loss)
    ev = lp.evaluate(row, _chain({(24000, "CE"): (None, 1.0, None), (24200, "CE"): (0.5, 0.6, 0.55)}), date(2026, 9, 29))
    assert ev["ok"] and ev["mark_ps"] == -0.6 and ev["profit_ps"] == -70.0


def test_neutral_structures_take_profit_at_65_pct_and_the_pre_expiry_rule_is_the_trackers(world):
    c = world
    condor = _condor()
    chain = _chain({(23500, "PE"): (30.0, 32.0, 31.0), (23300, "PE"): (9.0, 10.0, 9.5),
                    (24500, "CE"): (30.0, 32.0, 31.0), (24700, "CE"): (9.0, 10.0, 9.5)})
    _open(c, "lv0002", condor, chain)
    row = lp.open_rows(c, LIVE)[0]
    decayed = _chain({(23500, "PE"): (5.0, 6.0, 5.5), (23300, "PE"): (1.0, 2.0, 1.5),
                      (24500, "CE"): (5.0, 6.0, 5.5), (24700, "CE"): (1.0, 2.0, 1.5)})
    ev = lp.evaluate(row, decayed, date(2026, 9, 29))
    assert ev["mark_ps"] == -10.0 and ev["profit_ps"] == 30.0 and ev["signal"] == "profit_take"   # 30/40 = 75%
    ev = lp.evaluate(row, chain, date(2026, 10, 27))                     # 1 day to expiry on an index
    assert ev["signal"] == "pre_expiry_exit"


# ------------------------------------------------------------- the ratchet exit, end to end

def test_ratchet_arms_on_live_capture_and_exits_only_this_account_at_bid_ask(world):
    c = world
    e, view, opened = _open(c)
    up = _chain({(24000, "CE"): (140.0, 142.0, 141.0), (24200, "CE"): (12.0, 14.0, 13.0)})   # mark 126, +56 = 43% -> arms
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: up, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 1000.0, interval_s=300)
    assert t["marked"] == 1 and t["exits"] == [] and t["fetched"] == 1 and t["rung_pending"] == 1
    row = lp.open_rows(c, LIVE)[0]
    # one snapshot never arms a rung (audit F01): the mark is recorded, the lock waits for a second fetch
    assert row["ratchet_peak_pct"] is None and row["ratchet_lock_pct"] is None
    assert row["last_mark_ps"] == 126.0 and row["quote_ts"] is not None
    # a second, later fetch (one quote interval on) shows the same 43%: the breakeven lock arms
    t = lp.tick(now=OPEN.replace(minute=5), conn=c, chain_fn=lambda tk, x: up, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 1300.0, interval_s=300)
    assert t["fetched"] == 1 and t["rung_confirmed"] == 1 and t["exits"] == []
    row = lp.open_rows(c, LIVE)[0]
    assert row["ratchet_peak_pct"] == pytest.approx(43.08, abs=0.01) and row["ratchet_lock_pct"] == 0.0
    # give-back below the breakeven lock: capture -5% -> ratchet_hit -> exit at bid/ask, LIVE only
    down = _chain({(24000, "CE"): (90.0, 92.0, 91.0), (24200, "CE"): (24.0, 26.0, 25.0)})    # mark 64, -6
    t = lp.tick(now=OPEN.replace(hour=11, minute=10), conn=c, chain_fn=lambda tk, x: down, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 1600.0, interval_s=300)
    assert len(t["exits"]) == 1 and t["exits"][0]["status"] == "settled" and t["exits"][0]["signal"] == "ratchet_hit"
    tix = c.execute("SELECT account_id, lots, note FROM trade_tickets WHERE note LIKE 'EXIT%'").fetchall()
    assert [tuple(r) for r in tix] == [(LIVE, 1, "EXIT ratchet_hit")]
    fills = {(float(l["strike"]), l["side"]): l["avg_fill_price"]
             for l in oms.ticket_view(c, t["exits"][0]["ticket_id"])["legs"]}
    assert fills == {(24000.0, "SELL"): 90.0, (24200.0, "BUY"): 26.0}        # crossed, zero slip
    from src import portfolio as pf
    qty = 65
    frictions = sum(pf.calculate_trade_frictions("OPTION", s, p, qty)
                    for s, p in (("BUY", 100.0), ("SELL", 30.0), ("SELL", 90.0), ("BUY", 26.0)))
    expected = round(-6.0 * qty - frictions, 2)
    closed = lp.positions(c)[0]
    assert closed["state"] == "closed" and closed["resolution"] == "ratchet_hit"
    assert closed["closed_at"] == "2026-09-29T11:10:00" and closed["pnl_net"] == pytest.approx(expected, abs=0.02)
    assert closed["settlement_basis"] == "live_bid_ask" and closed["exit_mark_ps"] == 64.0
    # only LIVE's lock settled; 2L and the primary untouched
    assert pm._active_shadow_lock(c, LIVE, "lv0001") is None and pm._active_shadow_lock(c, TWO_L, "lv0001") is not None
    assert c.execute("SELECT COUNT(*) FROM margin_locks WHERE released_at IS NULL").fetchone()[0] == 1
    assert pm.paper_equity(c, LIVE) == pytest.approx(200000.0 + expected, abs=0.02)
    assert pm.paper_equity(c, TWO_L) == 200000.0
    detail = json.loads(c.execute("SELECT detail FROM paper_account_events WHERE event_type = 'live_exit'").fetchone()[0])
    assert detail["resolution"] == "ratchet_hit" and detail["closed_at"] == "2026-09-29T11:10:00" and detail["lock_released"]


def test_a_cached_predicate_is_reverified_on_a_fresh_chain_before_any_exit(world):
    c = world
    _open(c)
    up = _chain({(24000, "CE"): (140.0, 142.0, 141.0), (24200, "CE"): (12.0, 14.0, 13.0)})
    # the breakeven lock arms only on a second, later fetch (audit F01)
    for epoch in (500.0, 800.0):
        lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: up, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: epoch, interval_s=300)
    assert lp.open_rows(c, LIVE)[0]["ratchet_lock_pct"] == 0.0
    # poison the cache with a give-back; the fresh fetch says "still fine"
    key = ("NIFTY 50", "2026-10-28")
    down = _chain({(24000, "CE"): (90.0, 92.0, 91.0), (24200, "CE"): (24.0, 26.0, 25.0)})
    lp._CHAIN_CACHE[key] = (1000.0, "2026-09-29T11:00:00", down)
    calls = []
    t = lp.tick(now=OPEN.replace(minute=2), conn=c, chain_fn=lambda tk, x: calls.append(1) or up,
                sleep_fn=lambda s: None, now_epoch_fn=lambda: 1100.0, interval_s=300)
    assert calls == [1] and t["exits"] == [] and t["fetched"] == 1           # re-verified, no exit
    assert lp.open_rows(c, LIVE)[0]["state"] == "open"
    # the re-verify fetch FAILS: the cached predicate must NOT act
    lp._CHAIN_CACHE[key] = (1000.0, "2026-09-29T11:00:00", down)
    t = lp.tick(now=OPEN.replace(minute=4), conn=c, chain_fn=lambda tk, x: (_ for _ in ()).throw(RuntimeError("dhan")),
                sleep_fn=lambda s: None, now_epoch_fn=lambda: 1200.0, interval_s=300)
    assert t["exits"] == [] and t.get("unconfirmed") == 1 and t["marked"] == 1
    assert lp.open_rows(c, LIVE)[0]["state"] == "open"
    # past the 15:27 cutoff no fetch is allowed, so a cached predicate holds too
    lp._CHAIN_CACHE[key] = (1000.0, "2026-09-29T15:25:00", down)
    calls.clear()
    t = lp.tick(now=OPEN.replace(hour=15, minute=28), conn=c, chain_fn=lambda tk, x: calls.append(1) or down,
                sleep_fn=lambda s: None, now_epoch_fn=lambda: 1300.0, interval_s=300)
    assert calls == [] and t["exits"] == [] and t.get("unconfirmed") == 1
    assert lp.open_rows(c, LIVE)[0]["state"] == "open"


def test_an_exit_that_would_cost_more_than_the_max_loss_is_held(world):
    c = world
    _open(c)
    row = dict(lp.open_rows(c, LIVE)[0], ratchet_lock_pct=0.0)
    # long bid 0.05, short ask 40 -> mark -39.95 -> loss 109.95 > max loss 70
    res = lp._exit(c, row, {(24000.0, "CE"): 0.05, (24200.0, "CE"): 40.0}, "ratchet_hit", OPEN)
    assert res["status"] == "held_loss_beyond_max"
    assert c.execute("SELECT COUNT(*) FROM trade_tickets WHERE note LIKE 'EXIT%'").fetchone()[0] == 0
    assert lp.open_rows(c, LIVE)[0]["state"] == "open"


# ------------------------------------------------------------- the primary and the lock

def test_the_primary_exit_neither_tickets_nor_settles_the_live_arm(world, monkeypatch):
    c = world
    e, view, opened = _open(c)
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    e["accounts"][LIVE]["ticket_id"] = view["ticket_id"]
    rec = pt._execute_paper_exit(e, {(24000.0, "CE"): 120.0, (24200.0, "CE"): 33.0}, "profit_take", conn=c)
    assert rec["mode"] == "paper_venue" and LIVE not in rec["accounts"]
    exits = c.execute("SELECT account_id FROM trade_tickets WHERE note LIKE 'EXIT%' ORDER BY account_id").fetchall()
    assert [r[0] for r in exits] == ["PAPER_10L"]                          # 2L had no entry ticket in this world
    res = pm.release_entry("lv0001", 5000.0, conn=c)
    assert res["shadow_accounts"][TWO_L]["released"] and res["shadow_accounts"][TWO_L]["pnl_net"] == 5000.0
    assert not res["shadow_accounts"][LIVE]["released"] and "settles on its own" in res["shadow_accounts"][LIVE]["reason"]
    assert pm._active_shadow_lock(c, LIVE, "lv0001") is not None and lp.open_rows(c, LIVE)[0]["state"] == "open"


def test_a_lock_with_no_live_position_is_released_at_zero_and_named(world, monkeypatch):
    """Rejection, never-approved (hypothetical) resolution, unfilled entry:
    every path funnels through release_entry -> the lock never orphans."""
    c = world
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    pm.paper_request_entry(c, LIVE, "lv0007", 17550.0)
    pm.paper_request_entry(c, TWO_L, "lv0007", 17550.0)
    res = pm.release_entry("lv0007", 0.0, conn=c)                          # a human rejection
    assert res["shadow_accounts"][LIVE]["released"] and res["shadow_accounts"][TWO_L]["released"]
    assert pm._active_shadow_lock(c, LIVE, "lv0007") is None and pm.paper_available_cash(c, LIVE) == 200000.0
    ev = c.execute("SELECT event_type FROM paper_account_events WHERE account_id = ? AND journal_ref = 'lv0007'",
                   (LIVE,)).fetchall()
    assert ("live_lock_released_no_position",) in [tuple(r) for r in ev]


def test_a_crashed_exit_is_resumed_from_its_ticket_never_reissued(world):
    c = world
    e, view, opened = _open(c)
    from src import strategy_router as sr
    # a FILLED exit ticket exists but the row is still 'exiting' (crash after the fill)
    c.execute("UPDATE paper_live_positions SET state = 'exiting', exit_started_at = '2026-09-29T10:59:00'")
    c.commit()
    issued = sr.issue_exit(c, lp._entry_like(lp.open_rows(c, LIVE)[0]),
                           {(24000.0, "CE"): 120.0, (24200.0, "CE"): 33.0}, "ratchet_hit",
                           source="plan_tracker.ratchet_hit", account_id=LIVE, lots=1,
                           issued_at="2026-09-29T11:00:00+05:30")             # the fallback lookup path
    pv.sweep(c, stamp=False)
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=lambda s: None, now_epoch_fn=lambda: 1.0)
    assert t["resumed"] == 1
    closed = lp.positions(c)[0]
    assert closed["state"] == "closed" and closed["resolution"] == "ratchet_hit" and closed["exit_mark_ps"] == 87.0
    assert closed["exit_ticket_id"] == issued["ticket_id"]
    assert c.execute("SELECT COUNT(*) FROM trade_tickets WHERE note LIKE 'EXIT%'").fetchone()[0] == 1
    # an UNFILLED one is cancelled and the position reopened
    _open(c, "lv0005")
    c.execute("UPDATE paper_live_positions SET state = 'exiting', exit_started_at = '2026-09-29T10:59:00' "
              "WHERE journal_ref = 'lv0005'")
    c.commit()
    row = [r for r in lp.open_rows(c, LIVE) if r["journal_ref"] == "lv0005"][0]
    issued = sr.issue_exit(c, lp._entry_like(row), {(24000.0, "CE"): 120.0, (24200.0, "CE"): 33.0},
                           "ratchet_hit", source="plan_tracker.ratchet_hit", account_id=LIVE, lots=1,
                           issued_at="2026-09-29T11:00:00+05:30")
    c.execute("UPDATE paper_live_positions SET exit_ticket_id = ? WHERE journal_ref = 'lv0005'", (issued["ticket_id"],))
    c.commit()                                                                # the fast path
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=lambda s: None, now_epoch_fn=lambda: 1.0)
    assert oms.ticket_status(c, issued["ticket_id"]) == oms.CANCELLED
    assert [r for r in lp.open_rows(c, LIVE) if r["journal_ref"] == "lv0005"][0]["state"] == "open"


# ------------------------------------------------------------- expiry backstop

def test_expiry_backstop_mirrors_the_primary(world):
    c = world
    _open(c)
    bars = [("2026-10-27", 24000, 24300, 24250.0), ("2026-10-28", 24000, 24400, 24350.0)]
    out = lp.eod_sweep(c, today=date(2026, 10, 29), bars_fn=lambda t, s: bars, now=OPEN)
    s = out["settled"][0]
    # intrinsic at the 10-28 close 24350: long 350, short 150 -> mark 200 = full width -> profit 130
    assert s["exit_mark_ps"] == 200.0 and s["profit_ps"] == 130.0 and s["basis"] == "last_close_on_or_before_expiry"
    assert lp.positions(c)[0]["resolution"] == "expiry_backstop" and pm._active_shadow_lock(c, LIVE, "lv0001") is None
    # no bars: waits inside the grace window, then settles at max loss with zero frictions
    _open(c, "lv0006")
    out = lp.eod_sweep(c, today=date(2026, 10, 30), bars_fn=lambda t, s: [], now=OPEN)
    assert out["waiting"] == ["lv0006"] and out["settled"] == []
    out = lp.eod_sweep(c, today=date(2026, 10, 31), bars_fn=lambda t, s: [], now=OPEN)
    s = out["settled"][0]
    assert s["basis"] == "no_price_data_max_loss" and s["frictions_rs"] == 0.0 and s["pnl_net"] == -70.0 * 65


# ------------------------------------------------------------- pacing, cutoff, fail-open

def test_chain_fetches_are_paced_capped_and_stop_at_the_cutoff(world):
    c = world
    for i, tk in enumerate(("A", "B", "C", "D", "E")):
        e = _entry(f"lv001{i}")
        e["ticker"] = tk
        pm.paper_request_entry(c, LIVE, e["short_id"], 17550.0)
        rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: BULL_ENTRY)
        from src import strategy_router as sr
        issued = sr.issue(c, {"ticker": tk, "short_id": e["short_id"], "signal": "t",
                              "spread": dict(e["spread"], legs=rq["legs"])},
                          journal_ref=e["short_id"], source="t", account_id=LIVE, lots=1)
        pv.sweep(c, stamp=False)
        lp.open_position(c, LIVE, e, oms.ticket_view(c, issued["ticket_id"]), now=OPEN)
    clock = {"t": 1000.0}
    sleeps, calls = [], []

    def sleep(s):
        sleeps.append(round(s, 2)); clock["t"] += s

    def chain(tk, x):
        calls.append(tk); return BULL_ENTRY
    t = lp.tick(now=OPEN, conn=c, chain_fn=chain, sleep_fn=sleep, now_epoch_fn=lambda: clock["t"], max_fetches=3)
    assert t["fetched"] == 3 and len(calls) == 3 and t["marked"] == 3 and t["abstained"] == 2
    assert sleeps and all(s >= 2.9 for s in sleeps[1:])                    # >= 3 s between chain calls
    t = lp.tick(now=OPEN, conn=c, chain_fn=chain, sleep_fn=sleep, now_epoch_fn=lambda: clock["t"], max_fetches=3)
    assert t["fetched"] == 2 and t["marked"] == 5                         # the rest, within the interval nothing refetches
    lp.reset_cache()
    t = lp.tick(now=OPEN.replace(hour=15, minute=28), conn=c, chain_fn=chain, sleep_fn=sleep,
                now_epoch_fn=lambda: clock["t"])
    assert t["fetched"] == 0 and t["abstained"] == 5                       # past the cutoff: no new fetch


def test_the_tick_never_raises_and_the_live_loop_keeps_running(world, monkeypatch):
    c = world
    _open(c)
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: (_ for _ in ()).throw(RuntimeError("dhan down")),
                sleep_fn=lambda s: None, now_epoch_fn=lambda: 1.0)
    assert t["abstained"] == 1 and t["exits"] == []
    assert lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=lambda s: None,
                   now_epoch_fn=lambda: 1.0, require_market_open=True)["marked"] == 1
    monkeypatch.setattr(lp, "_market_open", lambda now: False)
    assert lp.tick(now=OPEN, conn=c)["skipped"] == "market closed"
    from src import live_bridge as lb
    now = datetime(2026, 9, 29, 11, 0)
    fired = lb.live_cycle(("NIFTY 50",), quote_fn=lambda u: {"price": 24000.0}, entries=[], now_fn=lambda: now,
                          live_account_fn=lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    assert fired == []


# ------------------------------------------------------------- doors

def test_one_exit_door_one_entry_door_and_no_dept3_import_in_the_venue():
    pt_src = (ROOT / "src/plan_tracker.py").read_text()
    assert pt_src.count("strategy_router.issue_exit(") == 1
    lp_src = (ROOT / "src/execution/live_pricer.py").read_text()
    assert "issue_exit" not in lp_src and "oms.issue_ticket" not in lp_src and "strategy_router.issue(" not in lp_src
    assert "_execute_paper_exit(" in lp_src                                  # the tracker's door, not its own
    op = (ROOT / "src/options_proposer.py").read_text()
    assert op.count("strategy_router.issue(") == 1
    venue = (ROOT / "src/execution/paper_venue.py").read_text()
    assert "portfolio_manager" not in venue and 'ZERO_SLIP_ACCOUNTS = ("PAPER_2L_LIVE",)' in venue
    for forbidden in ("dhanhq", "place_order"):
        assert forbidden not in lp_src


# ------------------------------------------------------------- review-round additions

def test_the_row_is_stamped_exiting_with_its_predicate_before_the_door_is_called(world, monkeypatch):
    c = world
    _open(c)
    seen = {}
    real = pt._execute_paper_exit

    def spy(entry, limits, resolution, **kw):
        r = c.execute("SELECT state, exit_started_at, exit_resolution FROM paper_live_positions").fetchone()
        seen["at_call"] = tuple(r)
        return real(entry, limits, resolution, **kw)
    monkeypatch.setattr(pt, "_execute_paper_exit", spy)
    row = dict(lp.open_rows(c, LIVE)[0], ratchet_lock_pct=0.0)
    res = lp._exit(c, row, {(24000.0, "CE"): 90.0, (24200.0, "CE"): 26.0}, "ratchet_hit", OPEN.replace(minute=5))
    assert seen["at_call"] == ("exiting", "2026-09-29T11:05:00", "ratchet_hit") and res["status"] == "settled"


def test_a_door_error_after_issue_leaves_the_row_for_resume_never_reissues(world, monkeypatch):
    c = world
    _open(c)
    row = dict(lp.open_rows(c, LIVE)[0], ratchet_lock_pct=0.0)

    class Venue:                                     # the sweep raises AFTER the ticket was committed
        FILL_BASIS, VENUE = pv.FILL_BASIS, pv.VENUE

        @staticmethod
        def sweep(conn, **kw):
            raise RuntimeError("database is locked")
    res = lp._exit(c, row, {(24000.0, "CE"): 90.0, (24200.0, "CE"): 26.0}, "ratchet_hit", OPEN, venue_mod=Venue)
    assert res["status"] == "exit_error"
    r = lp.open_rows(c, LIVE)[0]
    assert r["state"] == "exiting" and r["exit_resolution"] == "ratchet_hit"
    assert c.execute("SELECT COUNT(*) FROM trade_tickets WHERE note LIKE 'EXIT%'").fetchone()[0] == 1
    # next tick: the real venue fills that PENDING ticket, resume settles it — no second ticket
    pv.sweep(c, stamp=False)
    t = lp.tick(now=OPEN.replace(minute=1), conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 1.0)
    assert t["resumed"] == 1 and lp.positions(c)[0]["state"] == "closed"
    assert lp.positions(c)[0]["resolution"] == "ratchet_hit"
    assert c.execute("SELECT COUNT(*) FROM trade_tickets WHERE note LIKE 'EXIT%'").fetchone()[0] == 1


def test_close_and_release_are_one_transaction(world):
    """Real path, no monkeypatch (audit Chunk 1 D4, decision #122): the lock
    UPDATE itself fails inside SQLite (a trigger aborts it), so the row
    close that ran before it in the same transaction must roll back. Before
    #122 this test replaced the release with a stub and passed while a
    schema executescript committed the row close on its own."""
    c = world
    _open(c)
    row = lp.open_rows(c, LIVE)[0]
    c.execute("CREATE TRIGGER fail_release BEFORE UPDATE OF released_at ON paper_margin_locks "
              "BEGIN SELECT RAISE(ABORT, 'simulated lock write failure'); END")
    c.commit()
    eq_before = pm.paper_equity(c, LIVE)
    with pytest.raises(sqlite3.IntegrityError):
        lp._settle(c, row, 87.0, "ratchet_hit", "live_bid_ask", 0.0, OPEN)
    assert lp.open_rows(c, LIVE)[0]["state"] == "open"                         # rolled back, retried later
    assert pm._active_shadow_lock(c, LIVE, "lv0001") is not None
    assert pm.paper_equity(c, LIVE) == eq_before
    c.execute("DROP TRIGGER fail_release")
    c.commit()
    out = lp._settle(c, row, 87.0, "ratchet_hit", "live_bid_ask", 0.0, OPEN)   # the retry settles both
    assert out["status"] == "settled" and out["lock_released"] is True
    assert pm._active_shadow_lock(c, LIVE, "lv0001") is None
    assert c.execute("SELECT state FROM paper_live_positions WHERE journal_ref = 'lv0001'").fetchone()[0] == "closed"


def test_the_schema_check_never_commits_an_open_transaction(world):
    """The mechanism behind D4: once the tables exist, ensure_*_schema runs
    no statement at all, so a caller's open transaction stays open."""
    c = world
    pm.ensure_accounts_schema(c)
    pm.get_paper_account(c, LIVE)          # first touch seeds (and commits) the account row
    c.execute("INSERT INTO paper_account_events (account_id, ts, event_type) VALUES ('X', 't', 'probe')")
    assert c.in_transaction
    pm.ensure_accounts_schema(c)
    pm.paper_trading_halted(c, LIVE)
    assert c.in_transaction
    c.rollback()
    assert c.execute("SELECT COUNT(*) FROM paper_account_events WHERE event_type = 'probe'").fetchone()[0] == 0


def test_an_unknown_position_state_keeps_the_lock_and_a_closed_row_releases_late(world, monkeypatch, tmp_path):
    c = world
    _open(c)
    monkeypatch.setattr(lp, "has_open_position", lambda *a: (_ for _ in ()).throw(RuntimeError("busy")))
    out = pm.release_shadow_locks(c, "lv0001", 5000.0)
    assert not out[LIVE]["released"] and "lock kept" in out[LIVE]["reason"]
    assert pm._active_shadow_lock(c, LIVE, "lv0001") is not None
    monkeypatch.undo()
    monkeypatch.setattr(lp, "TICK_LOCK_FILE", tmp_path / ".live_pricer_tick.lock")   # undo() reverted conftest's
    # a row that closed with pnl but whose lock survived (crash after the row write): release at that pnl
    c.execute("UPDATE paper_live_positions SET state = 'closed', pnl_net = -1234.5 WHERE journal_ref = 'lv0001'")
    c.commit()
    # ...by the very next live tick, without waiting for the primary
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=lambda s: None, now_epoch_fn=lambda: 1.0)
    assert t.get("late_released") == ["lv0001"] and pm._active_shadow_lock(c, LIVE, "lv0001") is None
    assert pm.paper_equity(c, LIVE) == 200000.0 - 1234.5
    assert c.execute("SELECT COUNT(*) FROM paper_account_events WHERE event_type = 'live_lock_released_late'").fetchone()[0] == 1
    # and the primary-side path does the same when it gets there first
    pm.paper_request_entry(c, LIVE, "lv0099", 100.0)
    c.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, expiry, lots, lot_size, legs_json, "
              "entry_mark_ps, width_ps, max_profit_ps, max_loss_ps, opened_at, state, pnl_net) "
              "VALUES (?, 'lv0099', 'NIFTY 50', '2026-10-28', 1, 65, '[]', 70, 200, 130, 70, '2026-09-29T11:00:00', "
              "'closed', -50.0)", (LIVE,))
    c.commit()
    out = pm.release_shadow_locks(c, "lv0099", 5000.0)
    assert out[LIVE]["released"] and out[LIVE]["pnl_net"] == -50.0


def test_unfilled_entry_ticket_and_unrecorded_position_paths(world, monkeypatch, tmp_path):
    from src import options_proposer as op
    c = world
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    monkeypatch.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: BULL_ENTRY)
    # (1) the venue never fills LIVE's ticket (PENDING) -> cancelled, refused, lock released at zero
    class NoFill:
        FILL_BASIS, VENUE = pv.FILL_BASIS, pv.VENUE

        @staticmethod
        def sweep(conn, **kw):
            return {"skip": "stub"}
    e = _entry("lv0021")
    pm.request_entry(c, "lv0021", 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, "lv0021", 17550.0, lots=1, primary_lots=1)
    op._execute_paper_entry(e, venue_mod=NoFill)
    assert e["accounts"][LIVE]["status"] == "rejected" and "not filled" in e["accounts"][LIVE]["reason"]
    assert pm._active_shadow_lock(c, LIVE, "lv0021") is None and pm._active_shadow_lock(c, TWO_L, "lv0021") is not None
    live_t = c.execute("SELECT ticket_id, status FROM trade_tickets WHERE account_id = ? AND journal_ref = 'lv0021'",
                       (LIVE,)).fetchone()
    assert live_t[1] == oms.CANCELLED
    # (2) FILLED but open_position raises -> lock KEPT, event, repaired from the ticket on the next tick
    monkeypatch.setattr(lp, "open_position", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("insert failed")))
    e2 = _entry("lv0022")
    pm.request_entry(c, "lv0022", 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, "lv0022", 17550.0, lots=1, primary_lots=1)
    op._execute_paper_entry(e2)
    assert e2["accounts"][LIVE]["status"] == "approved" and pm._active_shadow_lock(c, LIVE, "lv0022") is not None
    assert not lp.has_open_position(c, LIVE, "lv0022")
    assert c.execute("SELECT COUNT(*) FROM paper_account_events WHERE event_type = 'live_position_unrecorded'").fetchone()[0] == 1
    monkeypatch.undo()
    monkeypatch.setattr(lp, "_market_open", lambda now: True)
    monkeypatch.setattr(lp, "_stamp_journal", lambda row, payload: None)
    monkeypatch.setattr(lp, "TICK_LOCK_FILE", tmp_path / ".live_pricer_tick.lock")   # undo() reverted conftest's
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=lambda s: None, now_epoch_fn=lambda: 1.0)
    assert t.get("repaired") == ["lv0022"] and lp.has_open_position(c, LIVE, "lv0022")
    row = [r for r in lp.open_rows(c, LIVE) if r["journal_ref"] == "lv0022"][0]
    assert row["entry_mark_ps"] == 70.0 and row["width_ps"] == 200.0 and row["max_profit_ps"] == 130.0


def test_requote_uses_fresh_prices_not_the_proposal_and_refuses_a_degenerate_structure(world, monkeypatch):
    from src import options_proposer as op
    c = world
    monkeypatch.setattr(brain_map, "connect", lambda *a, **k: c)
    monkeypatch.setattr(op, "_PAPER_VENUE_KEEP_CONN", True)
    fresh = _chain({(24000, "CE"): (102.0, 104.0, 103.0), (24200, "CE"): (28.0, 30.0, 29.0)})
    monkeypatch.setattr(op, "_LIVE_CHAIN_FN", lambda t, x: fresh)
    e = _entry("lv0031")
    pm.request_entry(c, "lv0031", 17550.0)
    for acct in (TWO_L, LIVE):
        pm.paper_request_entry(c, acct, "lv0031", 17550.0, lots=1, primary_lots=1)
    rec = op._execute_paper_entry(e)
    live_fills = {(float(l["strike"]), l["side"]): l["avg_fill_price"] for l in
                  oms.ticket_view(c, rec["accounts"][LIVE]["ticket_id"])["legs"]}
    assert live_fills == {(24000.0, "BUY"): 104.0, (24200.0, "SELL"): 28.0}      # fresh, crossed
    assert e["accounts"][LIVE]["entry_mark_ps"] == 76.0
    two_l = {(float(l["strike"]), l["side"]): l["avg_fill_price"] for l in
             oms.ticket_view(c, rec["accounts"][TWO_L]["ticket_id"])["legs"]}
    assert two_l == {(24000.0, "BUY"): 101.0, (24200.0, "SELL"): 29.7}           # the proposal legs + tier slip
    # a crossed debit wider than the structure is refused
    silly = _chain({(24000, "CE"): (240.0, 250.0, 245.0), (24200, "CE"): (30.0, 32.0, 31.0)})
    rq = lp.requote_entry(_entry("x"), now=OPEN, chain_fn=lambda t, x: silly)
    assert not rq["ok"] and "leave no trade" in rq["reason"]


def test_the_confirm_path_exits_on_the_fresh_chain_and_a_zero_bid_long_settles_on_the_mark(world):
    c = world
    _open(c)
    up = _chain({(24000, "CE"): (140.0, 142.0, 141.0), (24200, "CE"): (12.0, 14.0, 13.0)})
    # the breakeven lock arms only on a second, later fetch (audit F01)
    for epoch in (500.0, 800.0):
        lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: up, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: epoch, interval_s=300)
    assert lp.open_rows(c, LIVE)[0]["ratchet_lock_pct"] == 0.0
    key = ("NIFTY 50", "2026-10-28")
    stale_down = _chain({(24000, "CE"): (90.0, 92.0, 91.0), (24200, "CE"): (24.0, 26.0, 25.0)})
    fresh_down = _chain({(24000, "CE"): (88.0, 90.0, 89.0), (24200, "CE"): (25.0, 27.0, 26.0)})
    lp._CHAIN_CACHE[key] = (1000.0, "2026-09-29T11:00:00", stale_down)
    t = lp.tick(now=OPEN.replace(minute=6), conn=c, chain_fn=lambda tk, x: fresh_down, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 1100.0, interval_s=300)
    assert len(t["exits"]) == 1 and t["exits"][0]["status"] == "settled"
    fills = {(float(l["strike"]), l["side"]): l["avg_fill_price"]
             for l in oms.ticket_view(c, t["exits"][0]["ticket_id"])["legs"]}
    assert fills == {(24000.0, "SELL"): 88.0, (24200.0, "BUY"): 27.0}            # the FRESH prices
    assert lp.positions(c)[0]["exit_mark_ps"] == 61.0
    # a debit spread whose long has no bid: any exit costs more than max loss -> HELD (expiry does it)
    _open(c, "lv0041")
    dead = _chain({(24000, "CE"): (None, 1.0, None), (24200, "CE"): (0.5, 0.6, 0.55)})
    lp.reset_cache()
    t = lp.tick(now=datetime(2026, 10, 27, 11, 0), conn=c, chain_fn=lambda tk, x: dead, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 5000.0)
    ex = [x for x in t["exits"] if x["journal_ref"] == "lv0041"][0]
    assert ex["signal"] == "pre_expiry_exit" and ex["status"] == "held_loss_beyond_max"
    # a condor whose long wings have no bid: worth 0, the structure still marks AND exits (profit take),
    # the wings' tickets are floored at 0.05 but the settlement is on the 0.0 mark
    condor = _condor()
    chain = _chain({(23500, "PE"): (30.0, 32.0, 31.0), (23300, "PE"): (9.0, 10.0, 9.5),
                    (24500, "CE"): (30.0, 32.0, 31.0), (24700, "CE"): (9.0, 10.0, 9.5)})
    _open(c, "lv0042", condor, chain)
    decayed = _chain({(23500, "PE"): (4.0, 5.0, 4.5), (23300, "PE"): (None, 0.5, 0.3),
                      (24500, "CE"): (4.0, 5.0, 4.5), (24700, "CE"): (None, 0.5, 0.3)})
    lp.reset_cache()
    t = lp.tick(now=OPEN.replace(minute=20), conn=c, chain_fn=lambda tk, x: decayed, sleep_fn=lambda s: None,
                now_epoch_fn=lambda: 6000.0)
    ex = [x for x in t["exits"] if x["journal_ref"] == "lv0042"][0]
    assert ex["signal"] == "profit_take" and ex["status"] == "settled"          # mark -10 -> +30 of 40 = 75%
    closed = [p for p in lp.positions(c) if p["journal_ref"] == "lv0042"][0]
    assert closed["exit_mark_ps"] == -10.0 and closed["settlement_basis"] == "live_bid_ask"
    fills = {(float(l["strike"]), l["side"]): l["avg_fill_price"] for l in oms.ticket_view(c, ex["ticket_id"])["legs"]}
    assert fills[(23300.0, "SELL")] == 0.05 and fills[(24700.0, "SELL")] == 0.05   # the venue's floor, recorded
    detail = json.loads(c.execute("SELECT detail FROM paper_account_events WHERE journal_ref = 'lv0042' "
                                  "AND event_type = 'live_exit'").fetchone()[0])
    assert detail["limit_floored"] == {"23300PE": 0.05, "24700CE": 0.05} and detail["crossed_mark_ps"] == -10.0


def test_budget_and_cutoff_boundaries_and_reverify_counts_against_the_cap(world):
    c = world
    for i, tk in enumerate(("A", "B", "C", "D")):
        e = _entry(f"lv005{i}")
        e["ticker"] = tk
        pm.paper_request_entry(c, LIVE, e["short_id"], 17550.0)
        rq = lp.requote_entry(e, now=OPEN, chain_fn=lambda t, x: BULL_ENTRY)
        from src import strategy_router as sr
        issued = sr.issue(c, {"ticker": tk, "short_id": e["short_id"], "signal": "t",
                              "spread": dict(e["spread"], legs=rq["legs"])},
                          journal_ref=e["short_id"], source="t", account_id=LIVE, lots=1)
        pv.sweep(c, stamp=False)
        lp.open_position(c, LIVE, e, oms.ticket_view(c, issued["ticket_id"]), now=OPEN)
    clock = {"t": 1000.0}

    def sleep(s):
        clock["t"] += s
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=sleep,
                now_epoch_fn=lambda: clock["t"], max_fetches=10, budget_s=4.0)
    assert t["fetched"] == 2                                                   # the wall-clock budget, not the cap
    lp.reset_cache()
    t = lp.tick(now=OPEN.replace(hour=15, minute=27), conn=c, chain_fn=lambda tk, x: BULL_ENTRY, sleep_fn=sleep,
                now_epoch_fn=lambda: clock["t"])
    assert t["fetched"] == 0                                                   # 15:27 itself is past the cutoff
    # re-verify fetches count against max_fetches: cap 1, one due key fetched, a cached predicate cannot re-verify
    lp.reset_cache()
    down = _chain({(24000, "CE"): (90.0, 92.0, 91.0), (24200, "CE"): (24.0, 26.0, 25.0)})
    for tk in ("A", "B", "C", "D"):
        # fetched at the entries' own minute: a chain from BEFORE a row opened is never used on it (F19)
        lp._CHAIN_CACHE[(tk, "2026-10-28")] = (900.0, "2026-09-29T11:00:00", down)
    c.execute("UPDATE paper_live_positions SET ratchet_lock_pct = 0.0, ratchet_peak_pct = 45.0")
    c.commit()
    calls = []
    t = lp.tick(now=OPEN, conn=c, chain_fn=lambda tk, x: calls.append(tk) or down, sleep_fn=sleep,
                now_epoch_fn=lambda: clock["t"], max_fetches=1, interval_s=300)
    assert t["fetched"] == 1 and len(t["exits"]) == 1 and t.get("unconfirmed") == 3


def test_venue_off_settles_on_the_crossed_quotes_without_a_ticket(world, monkeypatch):
    c = world
    _open(c)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    row = dict(lp.open_rows(c, LIVE)[0], ratchet_lock_pct=0.0)
    res = lp._exit(c, row, {(24000.0, "CE"): 90.0, (24200.0, "CE"): 26.0}, "ratchet_hit", OPEN)
    assert res["status"] == "settled" and res["basis"] == "live_bid_ask_no_venue"
    assert c.execute("SELECT COUNT(*) FROM trade_tickets WHERE note LIKE 'EXIT%'").fetchone()[0] == 0
    assert lp.positions(c)[0]["exit_mark_ps"] == 64.0 and pm._active_shadow_lock(c, LIVE, "lv0001") is None


def test_the_friction_guard_knows_live_crossed_and_the_journal_stamp_is_muzzled(monkeypatch):
    s = _bull_call(basis="live_crossed")
    quotes = {(24000.0, "CE"): 120.0, (24200.0, "CE"): 33.0}
    f_live, slip_live = pt._spread_exit_costs_quoted(s, quotes, exit_slipped=True)
    f_model, slip_model = pt._spread_exit_costs_quoted(_bull_call(basis="model"), quotes, exit_slipped=True)
    assert f_live == f_model and slip_live == 0.0 and slip_model > 0.0
    calls = []
    monkeypatch.setattr("src.journal.update_entry", lambda *a, **k: calls.append(1))
    lp._stamp_journal({"journal_ref": "x", "account_id": LIVE}, {"closed_at": "t", "resolution": "r", "pnl_net": 0})
    assert calls == []                                                          # pytest -> muzzled


def test_run_tracker_sweeps_the_live_arm_even_with_an_empty_journal(world, monkeypatch):
    monkeypatch.setattr(pt.journal, "read_all", lambda: [])
    seen = {}
    monkeypatch.setattr(lp, "eod_sweep_standalone",
                        lambda today=None: seen.setdefault("today", today) or {"settled": []})
    assert pt.run_tracker(email=False) == 0
    assert seen["today"] is not None

"""Decision #161 (Architect 2026-10-28): the long-horizon expiry falls back to the nearest earlier contract
with a live book (Issue 52), and PAPER_2L_LIVE_GEX — a Rs.2L live-quote book, isolated, the only GEX-steered
account — takes the D6 momentum spread itself when the primary's SMA routing builds something else. Offline."""
from datetime import date

import pytest

from src import brain_map, options_proposer as op, portfolio_manager as pm
from tests.test_options_proposer import make_analysis, make_chain

GEX = "PAPER_2L_LIVE_GEX"


# --------------------------------------------------------------- expiry liquidity fallback

def _dead(chain):
    for node in chain["oc"].values():
        for side in ("ce", "pe"):
            if side in node:
                node[side] = dict(node[side], top_bid_price=0, top_ask_price=0, last_price=0)
    return chain


def test_a_book_with_no_bid_or_ask_near_the_money_is_not_live():
    assert op.has_live_book(make_chain())
    assert not op.has_live_book(_dead(make_chain()))
    assert not op.has_live_book({"oc": {}})


def test_a_dead_back_month_falls_back_to_the_nearest_earlier_quoted_expiry(monkeypatch):
    today = date(2026, 10, 28)
    expiries = ["2026-11-23", "2026-12-29", "2027-01-25"]
    asked = []

    def chain_for(u, exp):
        asked.append(exp)
        return make_chain() if exp == "2026-11-23" else _dead(make_chain())
    monkeypatch.setattr(op, "get_expiry_list", lambda u: expiries)
    monkeypatch.setattr(op, "get_option_chain", chain_for)
    r = op.build_proposal("NIFTY FIN SERVICE", analysis=make_analysis(uptrend=True, rsi=55), vix=13.0,
                          book={"cash": 5_000_000.0, "positions": {}}, prices={}, today=today, horizon="long",
                          account_equity=5_000_000.0)
    assert asked == ["2027-01-25", "2026-12-29", "2026-11-23"]
    assert r["proposal"] is not None and r["proposal"]["spread"]["expiry"] == "2026-11-23"
    assert r["proposal"]["expiry_fallback"] == {"from": "2027-01-25", "to": "2026-11-23",
                                                "reason": "no live quotes on the furthest contract (Issue 52)"}


def test_a_short_horizon_or_a_live_back_month_never_falls_back(monkeypatch):
    asked = []
    monkeypatch.setattr(op, "get_expiry_list", lambda u: ["2026-11-23", "2026-12-29"])
    monkeypatch.setattr(op, "get_option_chain", lambda u, e: asked.append(e) or make_chain())
    r = op.build_proposal("NIFTY FIN SERVICE", analysis=make_analysis(uptrend=True, rsi=55), vix=13.0,
                          book={"cash": 5_000_000.0, "positions": {}}, prices={}, today=date(2026, 10, 28),
                          horizon="long", account_equity=5_000_000.0)
    assert asked == ["2026-12-29"] and "expiry_fallback" not in r["proposal"]


# --------------------------------------------------------------- PAPER_2L_LIVE_GEX

@pytest.fixture
def gex_on(monkeypatch):
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_GEX_ENABLED", True)
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    yield c
    c.close()


def test_the_gex_book_is_a_seeded_isolated_live_account(gex_on):
    assert pm.PAPER_ACCOUNTS[GEX] == 200000.0 and GEX in pm.LIVE_ACCOUNTS
    assert GEX in pm.shadow_account_ids() and pm.gex_account_enabled()
    assert pm.paper_account_summary(gex_on, GEX)["equity"] == 200000.0


def test_the_gex_book_switches_off_alone(gex_on, monkeypatch):
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_GEX_ENABLED", False)
    ids = pm.shadow_account_ids()
    assert GEX not in ids and pm.ACCOUNT_PAPER_2L in ids and pm.ACCOUNT_PAPER_2L_LIVE in ids


SHORT_DOWN = {"allowed": False, "zone": "short_gamma", "wanted": "bear_put_spread", "move_pct": -1.2,
              "gex": {"day": "2026-10-27"}, "reason": "x"}


def _run(conn, steer, primary, built, taken=None):
    calls = {"build": [], "take": [], "log": []}

    def build_fn(u, **kw):
        calls["build"].append(kw)
        return built
    out = op._gex_own_entry("NIFTY BANK", {"analysis": {"price": 52_340.0}}, primary,
                            steer_fn=lambda *a: steer, build_fn=build_fn, conn=conn,
                            take_fn=lambda e, why, account: calls["take"].append((account, why))
                            or {"status": "approved", "lots": 1},
                            log_fn=lambda e: calls["log"].append(e))
    return out, calls


def _bear_put():
    return {"proposal": {"action": "SPREAD", "ticker": "NIFTY BANK", "shares": 30, "price": 100.0, "signal": "s", "lots": 1,
                         "view": "bearish", "spread": {"strategy": "bear_put_spread", "entry_spot": 52_340.0,
                                                       "lots": 1, "legs": [], "expiry": "2026-11-23"}}}


def test_the_gex_book_takes_the_d6_spread_when_the_primary_built_a_condor(gex_on):
    primary = {"proposal": {"spread": {"strategy": "iron_condor", "entry_spot": 52_340.0}}}
    out, calls = _run(gex_on, SHORT_DOWN, primary, _bear_put())
    assert out == {"status": "approved", "lots": 1}
    assert calls["build"][0]["view_override"] == "bearish" and calls["build"][0]["account_equity"] == 200000.0
    assert calls["take"][0][0] == GEX and "GEX D6 bear put spread (primary: iron_condor)" in calls["take"][0][1]
    e = calls["log"][0]
    assert e["decision"] == "rejected" and e["portfolio_family"] == "GEX_D6"


def test_the_gex_book_also_acts_when_the_primary_abstains(gex_on):
    out, calls = _run(gex_on, SHORT_DOWN, {"proposal": None, "reason": "no tradeable quotes"}, _bear_put())
    assert out["status"] == "approved" and "(primary: no tradeable quotes)" in calls["take"][0][1]


def test_no_gex_entry_outside_short_gamma_or_when_the_primary_built_the_same_spread(gex_on):
    long_ = dict(SHORT_DOWN, zone="long_gamma", wanted=None)
    assert _run(gex_on, long_, {"proposal": None}, _bear_put())[0] is None
    same = {"proposal": {"spread": {"strategy": "bear_put_spread", "entry_spot": 52_340.0}}}
    assert _run(gex_on, SHORT_DOWN, same, _bear_put())[0] is None             # it copies the primary instead
    straddle = dict(SHORT_DOWN, wanted="long_straddle")
    assert _run(gex_on, straddle, {"proposal": None}, _bear_put())[0] is None


def test_a_gated_or_unbuilt_gex_spread_is_skipped_by_name(gex_on):
    out, calls = _run(gex_on, SHORT_DOWN, {"proposal": None},
                      dict(_bear_put(), advisory_block="earnings proximity gate: results in 2 day(s)"))
    assert out["status"] == "skipped" and "earnings proximity gate" in out["reason"] and not calls["take"]


def test_the_gex_book_is_off_by_default(monkeypatch):
    assert op._gex_own_entry("NIFTY BANK", {}, {}) is None                      # conftest pins it off


def test_the_gex_book_never_fills_the_firm_wide_slot(gex_on):
    from src import exposure_gate
    from src.execution import live_pricer as lp
    lp.ensure_schema(gex_on)
    gex_on.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, strategy, direction, expiry, "
                   "lots, lot_size, legs_json, entry_mark_ps, width_ps, max_profit_ps, max_loss_ps, opened_at, state) "
                   "VALUES (?, 'g1', 'NIFTY BANK', 'bear_put_spread', 'bearish', '2026-11-23', 1, 30, '[]', 1, 2, 1, 1, "
                   "'2026-10-29T10:00:00', 'open')", (GEX,))
    pm.paper_request_entry(gex_on, GEX, "g1", 1000.0)
    pm.paper_request_entry(gex_on, pm.ACCOUNT_PAPER_2L, "p1", 1000.0)                 # a PAPER_2L lock does count
    rows, err = exposure_gate._firm_holdings(conn=gex_on)
    assert err is None and [r["account_id"] for r in rows] == [pm.ACCOUNT_PAPER_2L]


def test_the_shipped_config_runs_the_gex_book():
    import json
    from pathlib import Path
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
    assert cfg["paper_2l_live_gex_enabled"] is True and cfg["gex_steering_accounts"] == [GEX]

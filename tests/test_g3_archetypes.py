"""Milestone M1 "G3" — three distinct strategy archetypes, end to end
(2026-09-23 audit). The builders and the regime router already existed
(decisions #98 floors, 2026-08-05 graded regime read); these tests pin the
WHOLE path per regime: technical read -> archetype -> legs -> reward/risk
floor -> V1.1 fractional sizing -> OMS ticket -> no mid-trade stop (#105)."""
import pytest

from src import options_proposer as op, strategy_router as sr
from src.strategy import reward_risk_ratio, reward_risk_floor
from tests.test_options_proposer import BIG_BOOK, graded, make_chain


def _run(analysis, vix=12.0, book=None):
    return op.build_proposal("NIFTY 50", analysis=analysis, vix=vix,
                             chain=make_chain(), expiry="2026-11-26",
                             book=book or dict(BIG_BOOK), prices={})


def _ticket_legs(p):
    t = sr.build_ticket(p)
    return t, [(l["side"], l["option_type"], l["strike"]) for l in t["legs"]]


def test_bullish_regime_routes_to_a_bull_call_debit_spread():
    a = graded(fast_pct=3.0, slow_pct=6.0)               # spot above both SMAs
    r = _run(a)
    p = r["proposal"]
    assert p is not None and p["spread"]["strategy"] == "bull_call_spread"
    legs = {(l["side"], l["option_type"], l["strike"]) for l in p["spread"]["legs"]}
    assert legs == {("BUY", "CE", 25000.0), ("SELL", "CE", 25200.0)}   # ATM buy, OTM sell
    assert p["spread"]["net_debit"] is not None and p["spread"]["net_credit"] is None
    assert reward_risk_ratio(p["spread"]) >= reward_risk_floor("bull_call_spread") == 1.5
    assert p["regime_read"]["view"] == "bullish" and p["regime_read"]["grade"] in ("bullish", "strong_bullish")
    assert p["regime_read"]["structure"] == "bull_call_spread"


def test_neutral_regime_routes_to_an_iron_condor_credit_spread():
    a = graded(fast_pct=0.4, slow_pct=-0.6)               # pinned inside the flat band
    r = _run(a, vix=13.0)
    p = r["proposal"]
    assert p is not None and p["spread"]["strategy"] == "iron_condor"
    legs = {(l["side"], l["option_type"], l["strike"]) for l in p["spread"]["legs"]}
    assert legs == {("SELL", "PE", 24500.0), ("BUY", "PE", 24300.0),
                    ("SELL", "CE", 25500.0), ("BUY", "CE", 25700.0)}
    assert p["spread"]["net_credit"] is not None
    assert reward_risk_ratio(p["spread"]) >= reward_risk_floor("iron_condor") == 0.35
    assert p["regime_read"]["flat"] and p["regime_read"]["view"] == "neutral"


def test_bearish_regime_still_routes_to_the_bear_put():
    p = _run(graded(fast_pct=-3.0, slow_pct=-6.0))["proposal"]
    assert p["spread"]["strategy"] == "bear_put_spread"
    assert {l["option_type"] for l in p["spread"]["legs"]} == {"PE"}


def test_all_three_archetypes_are_distinct_defined_risk_and_in_the_router():
    seen = {}
    for a, vix in ((graded(fast_pct=3.0, slow_pct=6.0), 12.0),
                   (graded(fast_pct=0.4, slow_pct=-0.6), 13.0),
                   (graded(fast_pct=-3.0, slow_pct=-6.0), 12.0)):
        p = _run(a, vix=vix)["proposal"]
        seen[p["spread"]["strategy"]] = p
    assert set(seen) == {"bull_call_spread", "iron_condor", "bear_put_spread"}   # G3: 3 archetypes
    for name, p in seen.items():
        assert sr.is_defined_risk(name) and sr.ROUTING_TABLE[name]["direction"] == p["regime_read"]["view"]
        assert p["spread"]["max_loss"] > 0 and p["spread"]["max_profit"] > 0


def test_new_archetypes_size_by_the_v11_fractional_rule_and_carry_no_stop():
    import src.config as cfg
    for a, vix in ((graded(fast_pct=3.0, slow_pct=6.0), 12.0),
                   (graded(fast_pct=0.4, slow_pct=-0.6), 13.0)):
        p = _run(a, vix=vix, book={"cash": 1_000_000.0, "holdings": {}})["proposal"]
        s = p["sizing"]
        assert s["risk_pct"] == cfg.ACCOUNT_RISK_PER_TRADE_PCT and s["equity"] == 1_000_000.0
        # adaptive sizing (#81) may shrink the fractional lots, never grow them
        assert 1 <= p["lots"] == s["lots_final"] <= s["lots"]
        assert p["lots"] * p["spread"]["max_loss"] <= max(s["risk_capacity_rs"], p["spread"]["max_loss"])
        assert "stop_loss" not in p["spread"] and "stop" not in p["spread"]      # #105: structure is the stop
        t, legs = _ticket_legs(p)
        assert t.get("kind", "ENTRY") == "ENTRY" and len(legs) == len(p["spread"]["legs"])
        assert all(l["qty_target"] == p["lots"] * p["spread"]["lot_size"] for l in t["legs"])


def test_a_refused_structure_still_reports_the_regime_it_read():
    # a chain too thin to build anything: refusal, but the view is on the answer
    thin = {"last_price": 25000.0, "oc": {"25000.000000": make_chain()["oc"]["25000.000000"]}}
    r = op.build_proposal("NIFTY 50", analysis=graded(fast_pct=3.0, slow_pct=6.0), vix=12.0,
                          chain=thin, expiry="2026-11-26", book=dict(BIG_BOOK), prices={})
    assert r["proposal"] is None and r["view"] == "bullish"
    assert op.regime_read(graded(fast_pct=3.0, slow_pct=6.0), 12.0)["structure"] == "bull_call_spread"

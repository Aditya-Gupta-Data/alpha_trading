"""
Reward-to-risk guardrail (architect directive 2026-09-16, decision #98):
max_profit / max_loss on the BUILT structure must clear 1.5 for directional
spreads and 0.35 for range-bound credit structures, or the trade is refused
with a named REJECTED_POOR_RR reason — before sizing, before margin.

Offline: fake chains, no Dhan. Run:
    python -m pytest tests/test_reward_risk.py -q
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import strategy as st
from src import options_proposer as op
from src import proposal_ledger as PL
from src.strategies import glassbreaking as gb
from src.strategies import insolvency_short as ins
from tests.test_options_proposer import make_analysis, make_chain, BIG_BOOK

EXPIRY = (date.today() + timedelta(days=14)).isoformat()


def _spread(strategy, max_profit, max_loss):
    return {"strategy": strategy, "max_profit": max_profit, "max_loss": max_loss,
            "legs": [{"side": "BUY", "option_type": "CE"}, {"side": "SELL", "option_type": "CE"}]}


# ------------------------------------------------------------ the pure gate

def test_ratio_and_floors():
    assert st.reward_risk_ratio(_spread("bull_call_spread", 300.0, 200.0)) == 1.5
    assert st.reward_risk_ratio(_spread("x", 300.0, 0.0)) is None
    assert st.reward_risk_ratio({"strategy": "x"}) is None
    assert st.reward_risk_floor("bull_call_spread") == 1.5
    assert st.reward_risk_floor("bear_put_spread") == 1.5
    assert st.reward_risk_floor("iron_condor") == 0.35
    assert st.reward_risk_floor("iron_butterfly") == 0.35
    assert st.reward_risk_floor("anything_else") == 1.5          # unknown = strict


def test_directional_1_4_is_rejected_and_1_6_is_approved():
    ok, r, floor, why = st.reward_risk_gate(_spread("bear_put_spread", 1400.0, 1000.0))
    assert ok is False and r == 1.4 and floor == 1.5
    assert why.startswith("REJECTED_POOR_RR") and "R:R below 1.5 threshold" in why
    assert "Rs.1,400 against Rs.1,000" in why and "(R:R 1.40)" in why
    ok, r, *_ = st.reward_risk_gate(_spread("bull_call_spread", 1600.0, 1000.0))
    assert ok is True and r == 1.6
    ok, r, *_ = st.reward_risk_gate(_spread("bull_call_spread", 1500.0, 1000.0))
    assert ok is True and r == 1.5                                # AT LEAST 1.5


def test_the_architects_example_is_rejected():
    ok, r, _, why = st.reward_risk_gate(_spread("bear_put_spread", 3000.0, 9000.0))
    assert not ok and r == pytest.approx(0.3333, abs=1e-4) and "R:R 0.33" in why


def test_neutral_structures_use_their_own_floor():
    assert st.reward_risk_gate(_spread("iron_condor", 340.0, 1000.0))[0] is False
    assert st.reward_risk_gate(_spread("iron_condor", 360.0, 1000.0))[0] is True
    assert st.reward_risk_gate(_spread("iron_butterfly", 400.0, 1000.0))[0] is True
    # a condor at 1.4 passes the neutral floor; the same numbers on a
    # directional spread do not
    assert st.reward_risk_gate(_spread("iron_condor", 1400.0, 1000.0))[0] is True
    assert st.reward_risk_gate(_spread("bear_put_spread", 1400.0, 1000.0))[0] is False


def test_unmeasurable_payoff_fails_closed():
    ok, r, _, why = st.reward_risk_gate(_spread("bull_call_spread", 100.0, 0.0))
    assert not ok and r is None and "not measurable" in why
    ok, *_ = st.reward_risk_gate({"strategy": "bull_call_spread"})
    assert not ok
    assert st.reward_risk_gate(None)[0] is False


def test_every_built_structure_carries_its_ratio():
    sc = st.StrategyConstructor(vix=13.0, lot_size=75)
    bcs = sc.construct_bull_call_spread(25000, 25200, 100.0, 30.0)
    assert bcs["reward_risk"] == pytest.approx(bcs["max_profit"] / bcs["max_loss"], abs=1e-3)
    ic = sc.construct_iron_condor(24800, 25200, 200, 95, 38, 100, 40)
    assert ic["reward_risk"] == pytest.approx(117 / 83, abs=1e-3)


def test_ledger_classifies_the_refusal():
    why = st.reward_risk_gate(_spread("bear_put_spread", 1400.0, 1000.0))[3]
    assert PL.classify({"proposal": None, "reason": why}) == "REJECTED_POOR_RR"


# ------------------------------------------------------------ the live proposer

def _build(view_analysis, base_premium, vix=13.0, spot=25000.0):
    return op.build_proposal("NIFTY 50", analysis=view_analysis, vix=vix, expiry=EXPIRY,
                             chain=make_chain(spot=spot, base_premium=base_premium),
                             book=dict(BIG_BOOK), prices={})


def test_build_proposal_refuses_a_directional_spread_at_1_4_and_builds_at_1_6():
    # make_chain: 4 steps OTM the premium is 0.82 x base, so a bull call's
    # debit is 0.18 x base against a 200-point width
    # base 463 -> debit 83.3 -> R:R (200-83.3)/83.3 = 1.40
    res = _build(make_analysis(uptrend=True, rsi=25), base_premium=463.0)
    assert res["proposal"] is None
    assert res["reason"].startswith("REJECTED_POOR_RR") and "R:R below 1.5" in res["reason"]
    assert res["rejected_spread"]["strategy"] == "bull_call_spread"
    assert res["reward_risk"] == pytest.approx(1.40, abs=0.01) and res["reward_risk_floor"] == 1.5
    assert PL.classify(res) == "REJECTED_POOR_RR"
    # base 427 -> debit 76.9 -> R:R 1.60
    res = _build(make_analysis(uptrend=True, rsi=25), base_premium=427.0)
    assert res["proposal"] is not None, res["reason"]
    assert res["proposal"]["spread"]["reward_risk"] == pytest.approx(1.60, abs=0.01)


def test_build_proposal_applies_the_same_floor_to_bear_puts():
    res = _build(make_analysis(uptrend=False), base_premium=463.0)
    assert res["proposal"] is None and "R:R below 1.5" in res["reason"]
    assert res["rejected_spread"]["strategy"] == "bear_put_spread"
    res = _build(make_analysis(uptrend=False), base_premium=427.0)
    assert res["proposal"] is not None


def test_build_proposal_keeps_iron_condors_on_the_neutral_floor():
    # condor shorts sit 10 steps out (0.55 x base), wings 14 steps (0.37 x base):
    # net credit 0.36 x base against a 200-point wing
    # base 130 -> credit 46.8, loss 153.2 -> R:R 0.31 (below 0.35)
    res = _build(make_analysis(uptrend=True, rsi=55), base_premium=130.0, vix=13.0)
    assert res["proposal"] is None and "R:R below 0.35" in res["reason"]
    assert res["rejected_spread"]["strategy"] == "iron_condor"
    # base 160 -> credit 57.6, loss 142.4 -> R:R 0.40 (passes 0.35, fails 1.5)
    res = _build(make_analysis(uptrend=True, rsi=55), base_premium=160.0, vix=13.0)
    assert res["proposal"] is not None, res["reason"]
    assert res["proposal"]["spread"]["strategy"] == "iron_condor"
    assert 0.35 <= res["proposal"]["spread"]["reward_risk"] < 1.5


def test_the_gate_sits_before_sizing():
    """A refused ratio never reaches the risk budget: the refusal names R:R,
    not the budget, even on a thin book."""
    res = op.build_proposal("NIFTY 50", analysis=make_analysis(uptrend=True, rsi=25), vix=13.0,
                            expiry=EXPIRY, chain=make_chain(base_premium=463.0),
                            book={"cash": 1_000.0, "holdings": {}}, prices={})
    assert res["proposal"] is None and res["reason"].startswith("REJECTED_POOR_RR")


# ------------------------------------------------------------ the shadow paths

FO = {"as_of": "2026-09-16", "banned": [], "symbols": {"RELIANCE": {"tier": "tier1"}}}


def test_glassbreaking_setup_obeys_the_directional_floor():
    sig = {"primitive": "falling_knife", "direction": "bullish", "date": "2026-09-16",
           "spot": 1000.0, "triggers": ["rsi_oversold"]}
    # 1000/1050 for 30/12: debit 18 vs width 50 -> profit 32 -> R:R 1.78 ok
    ok = gb.build_setup(sig, "RELIANCE", 1000.0, 1050.0, 30.0, 12.0, 250, "2026-09-30",
                        pool_rupees=2_000_000.0, fo=FO)
    assert ok["accepted"] and ok["spread"]["reward_risk"] == pytest.approx(32 / 18, abs=1e-3)
    # 1000/1050 for 30/8: debit 22 vs profit 28 -> R:R 1.27 -> refused
    bad = gb.build_setup(sig, "RELIANCE", 1000.0, 1050.0, 30.0, 8.0, 250, "2026-09-30",
                         pool_rupees=2_000_000.0, fo=FO)
    assert not bad["accepted"] and bad["reason"].startswith("REJECTED_POOR_RR")
    assert bad["reward_risk"] == pytest.approx(1.27, abs=0.01)


def test_insolvency_short_setup_obeys_the_directional_floor():
    trig = {"symbol": "RELIANCE", "date": "2026-09-16", "subject": "Defaults on Payment"}
    # bear put 1000/950 for 30/12: debit 18 vs profit 32 -> 1.78 ok
    ok = ins.build_setup(trig, 1000.0, 1000.0, 950.0, 30.0, 12.0, 250, "2026-09-30",
                         pool_rupees=2_000_000.0, fo=FO)
    assert ok["accepted"], ok["reason"]
    bad = ins.build_setup(trig, 1000.0, 1000.0, 950.0, 30.0, 8.0, 250, "2026-09-30",
                          pool_rupees=2_000_000.0, fo=FO)
    assert not bad["accepted"] and bad["reason"].startswith("REJECTED_POOR_RR")

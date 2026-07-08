"""
Phase 6I trade planner tests — the technical-to-options evaluation
matrix. Everything is pure and offline: mocked technical market states
in, structural strategy plans out, byte-for-byte deterministic.

Run from the project folder:
    python tests/test_trade_planner.py      (simple, no extra installs)
    python -m pytest tests/                 (if you have pytest)
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import trade_planner as tp

# Bank Nifty anchor for the strike-math tests: spot 52,340 -> ATM 52,300
# (step 100), default 2% OTM shorts -> 51,200 P / 53,400 C, wings 400.
SPOT = 52_340.0


def make_state(**overrides) -> dict:
    state = {"spot": SPOT, "underlying": "NIFTY BANK",
             "sma_fast_distance_pct": 0.1, "sma_slow_distance_pct": -0.1,
             "vix": 14.5, "support": None, "resistance": None}
    state.update(overrides)
    return state


# --- the routing matrix ---------------------------------------------------

def test_range_bound_plus_high_iv_routes_to_iron_condor():
    plan = tp.map_technical_to_strategy(make_state())  # mixed SMAs, VIX 14.5
    assert plan["strategy"] == "iron_condor"
    assert plan["tradeable"] is True
    assert plan["view"] == "neutral" and plan["iv_regime"] == "high"
    assert len(plan["legs"]) == 4


def test_strong_bullish_plus_low_iv_routes_to_bull_call_spread():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=1.2, sma_slow_distance_pct=3.5, vix=11.0))
    assert plan["strategy"] == "bull_call_spread"
    assert plan["view"] == "strong_bullish" and plan["iv_regime"] == "low"


def test_bearish_plus_high_iv_routes_to_bear_call_spread():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=-0.8, sma_slow_distance_pct=-1.2, vix=15.0))
    assert plan["strategy"] == "bear_call_spread"
    assert plan["view"] == "bearish" and plan["iv_regime"] == "high"


def test_bearish_plus_low_iv_routes_to_bear_put_spread():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=-0.8, sma_slow_distance_pct=-1.2, vix=11.0))
    assert plan["strategy"] == "bear_put_spread"


def test_range_bound_plus_low_iv_is_a_no_trade():
    plan = tp.map_technical_to_strategy(make_state(vix=11.0))
    assert plan["strategy"] == "no_trade" and plan["tradeable"] is False
    assert plan["legs"] == []
    assert "thin" in plan["rationale"]


def test_the_vix_16_regime_gate_is_never_contradicted():
    # extreme IV blocks the condor exactly where strategy.validate_regime
    # would — the planner and the constructor must agree forever
    for vix in (16.1, 22.0, None):
        plan = tp.map_technical_to_strategy(make_state(vix=vix))
        assert plan["strategy"] == "no_trade", f"VIX {vix} must not plan a condor"
    # bearish credit spreads also stand aside in a panic regime
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=-1.0, sma_slow_distance_pct=-1.5, vix=25.0))
    assert plan["strategy"] == "no_trade"


def test_strong_bullish_in_rich_iv_refuses_the_overpriced_debit():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=1.2, sma_slow_distance_pct=3.5, vix=15.5))
    assert plan["strategy"] == "no_trade"


def test_weak_bullish_never_gets_a_directional_structure():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=0.3, sma_slow_distance_pct=0.8, vix=11.0))
    assert plan["strategy"] == "no_trade"
    assert plan["view"] == "bullish"


def test_missing_spot_is_a_safe_no_trade():
    plan = tp.map_technical_to_strategy({"vix": 14.0})
    assert plan["strategy"] == "no_trade" and plan["legs"] == []


# --- the classifiers ------------------------------------------------------

def test_iv_regime_boundaries():
    assert tp.classify_iv(None) == "unknown"
    assert tp.classify_iv(12.99) == "low"
    assert tp.classify_iv(13.0) == "high"     # the band starts at 13
    assert tp.classify_iv(16.0) == "high"     # 16 itself passes the gate
    assert tp.classify_iv(16.01) == "extreme"


def test_trend_classification_boundaries():
    assert tp.classify_trend(None, 2.0) == "unknown"
    assert tp.classify_trend(0.5, 2.0) == "strong_bullish"    # both at edge
    assert tp.classify_trend(0.4, 2.5) == "bullish"           # unconfirmed
    assert tp.classify_trend(0.2, 0.3) == "bullish"
    assert tp.classify_trend(-0.5, -2.0) == "strong_bearish"
    assert tp.classify_trend(-0.2, -0.3) == "bearish"
    assert tp.classify_trend(0.4, -0.2) == "neutral"          # mixed = range
    assert tp.classify_trend(-0.1, 1.0) == "neutral"


def test_explicit_trend_and_iv_override_the_raw_numbers():
    plan = tp.map_technical_to_strategy(make_state(
        trend="neutral", iv_regime="high",
        sma_fast_distance_pct=5.0, sma_slow_distance_pct=5.0, vix=25.0))
    assert plan["strategy"] == "iron_condor"


# --- Bank Nifty strike geometry -------------------------------------------

def test_condor_strikes_snap_to_the_bank_nifty_grid():
    plan = tp.map_technical_to_strategy(make_state())
    assert plan["atm"] == 52_300.0
    assert plan["strike_step"] == 100.0 and plan["lot_size"] == 35
    by_role = {l["role"]: l for l in plan["legs"]}
    # 2% OTM defaults: 52,340x0.98=51,293.2 -> 51,200; x1.02=53,386.8 -> 53,400
    assert by_role["short put (range floor)"]["strike"] == 51_200.0
    assert by_role["short call (range cap)"]["strike"] == 53_400.0
    # wings sit WING_STEPS x step = 400 further out
    assert by_role["put wing"]["strike"] == 50_800.0
    assert by_role["call wing"]["strike"] == 53_800.0
    # offsets from ATM are part of the structural contract
    assert by_role["short put (range floor)"]["offset_from_atm"] == -1_100.0
    assert by_role["call wing"]["offset_from_atm"] == 1_500.0
    # sides and option types
    assert [(l["side"], l["option_type"]) for l in plan["legs"]] == [
        ("SELL", "PE"), ("BUY", "PE"), ("SELL", "CE"), ("BUY", "CE")]


def test_support_and_resistance_override_the_default_otm_placement():
    plan = tp.map_technical_to_strategy(make_state(
        support=51_550.0, resistance=53_120.0))
    by_role = {l["role"]: l for l in plan["legs"]}
    assert by_role["short put (range floor)"]["strike"] == 51_500.0   # under support
    assert by_role["short call (range cap)"]["strike"] == 53_200.0    # over resistance


def test_nonsense_boundaries_fall_back_to_the_otm_defaults():
    plan = tp.map_technical_to_strategy(make_state(
        support=60_000.0, resistance=40_000.0))  # support above, resistance below
    by_role = {l["role"]: l for l in plan["legs"]}
    assert by_role["short put (range floor)"]["strike"] == 51_200.0
    assert by_role["short call (range cap)"]["strike"] == 53_400.0


def test_bull_call_spread_is_atm_plus_wing():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=1.2, sma_slow_distance_pct=3.5, vix=11.0))
    assert [(l["side"], l["option_type"], l["offset_from_atm"])
            for l in plan["legs"]] == [("BUY", "CE", 0.0), ("SELL", "CE", 400.0)]


def test_bear_call_spread_sells_above_resistance():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=-0.8, sma_slow_distance_pct=-1.2, vix=15.0,
        resistance=52_910.0))
    assert [(l["side"], l["strike"]) for l in plan["legs"]] == [
        ("SELL", 53_000.0), ("BUY", 53_400.0)]


def test_bear_put_spread_is_atm_minus_wing():
    plan = tp.map_technical_to_strategy(make_state(
        sma_fast_distance_pct=-0.8, sma_slow_distance_pct=-1.2, vix=11.0))
    assert [(l["side"], l["option_type"], l["offset_from_atm"])
            for l in plan["legs"]] == [("BUY", "PE", 0.0), ("SELL", "PE", -400.0)]


def test_nifty_50_gets_its_own_grid_and_lot():
    plan = tp.map_technical_to_strategy(make_state(
        underlying="NIFTY 50", spot=25_432.0))
    assert plan["strike_step"] == 50.0 and plan["lot_size"] == 75
    assert plan["atm"] == 25_450.0
    for leg in plan["legs"]:
        assert leg["strike"] % 50.0 == 0


# --- purity ---------------------------------------------------------------

def test_planner_is_pure_deterministic_and_never_mutates_its_input():
    state = make_state(support=51_550.0, resistance=53_120.0)
    snapshot = copy.deepcopy(state)
    first = tp.map_technical_to_strategy(state)
    second = tp.map_technical_to_strategy(state)
    assert first == second           # same read, same plan, forever
    assert state == snapshot         # input untouched


def test_planner_module_has_no_side_effect_machinery():
    """Source guard (same spirit as the simulator's): the planner must
    never grow network/DB/journal/notifier tentacles."""
    import ast
    tree = ast.parse(Path(tp.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    for forbidden in ("dhan_client", "journal", "notifier", "sqlite3",
                      "brain_map", "httpx", "requests", "portfolio"):
        assert not any(forbidden in name for name in imported), \
            f"planner must not import {forbidden}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

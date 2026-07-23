"""
tests/test_strategy_registry.py — the Strategy Registry honesty contract
========================================================================

Covers: frozen identity (hash stability + order semantics), spec validation,
the pure evaluator per kind (incl. NULL-honesty), future-blindness via the
timelock helper, the builder's support floor / BH batch / placebo split, and
the declare() query seam's honest-absence behaviour.
"""
import json

import pytest

from src.analysis import strategy_registry as SR
from src.validation.timelock import assert_future_blind


# ------------------------------------------------------------- identity

def test_strategy_id_is_stable_and_metadata_free():
    a = {"name": "x", "kind": "long_sector", "horizon": "shock",
         "params": {"sector": "NIFTY_IT"}, "thesis": "one"}
    b = {"name": "TOTALLY_DIFFERENT", "kind": "long_sector", "horizon": "shock",
         "params": {"sector": "NIFTY_IT"}, "thesis": "two", "source": "placebo"}
    assert SR.strategy_id(a) == SR.strategy_id(b)   # name/thesis/source are metadata


def test_basket_membership_is_order_blind():
    a = {"kind": "basket_rotation", "horizon": "shock",
         "params": {"longs": ["NIFTY_FMCG", "NIFTY_PHARMA"], "shorts": ["NIFTY_METAL"]}}
    b = {"kind": "basket_rotation", "horizon": "shock",
         "params": {"longs": ["NIFTY_PHARMA", "NIFTY_FMCG"], "shorts": ["NIFTY_METAL"]}}
    assert SR.strategy_id(a) == SR.strategy_id(b)


def test_pair_roles_are_positional():
    a = {"kind": "long_short_pair", "horizon": "shock",
         "params": {"long": "NIFTY_IT", "short": "NIFTY_BANK"}}
    b = {"kind": "long_short_pair", "horizon": "shock",
         "params": {"long": "NIFTY_BANK", "short": "NIFTY_IT"}}
    assert SR.strategy_id(a) != SR.strategy_id(b)   # long A/short B != long B/short A


# ------------------------------------------------------------- validation

@pytest.mark.parametrize("bad", [
    {"kind": "no_such_kind", "horizon": "shock", "params": {"sector": "NIFTY_IT"}},
    {"kind": "long_sector", "horizon": "no_such_hz", "params": {"sector": "NIFTY_IT"}},
    {"kind": "long_sector", "horizon": "shock", "params": {"sector": "NOT_A_SECTOR"}},
    {"kind": "long_short_pair", "horizon": "shock",
     "params": {"long": "NIFTY_IT", "short": "NIFTY_IT"}},
    {"kind": "basket_rotation", "horizon": "shock",
     "params": {"longs": ["NIFTY_IT"], "shorts": ["NIFTY_IT"]}},
    {"kind": "basket_rotation", "horizon": "shock", "params": {"longs": []}},
    {"kind": "benchmark_tilt", "horizon": "shock",
     "params": {"sector": "NIFTY_IT", "weight": 1.5}},
])
def test_validate_spec_rejects_malformed(bad):
    with pytest.raises(ValueError):
        SR.validate_spec(bad)


def test_every_seed_and_placebo_validates():
    for s in SR.SEED_STRATEGIES + SR.PLACEBO_STRATEGIES:
        SR.validate_spec(s)                          # raises on the first bad one


# ------------------------------------------------------------- evaluator

def _phases():
    return {"P1_shock": {
        "NIFTY_IT":    {"abs": 0.05, "excess": 0.02},
        "NIFTY_BANK":  {"abs": 0.01, "excess": -0.02},
        "NIFTY_FMCG":  {"abs": 0.03, "excess": 0.00},
        "NIFTY_METAL": {"abs": -0.04, "excess": -0.07},
    }}


def test_evaluate_long_sector_is_excess():
    spec = {"kind": "long_sector", "params": {"sector": "NIFTY_IT"}}
    assert SR.evaluate_leg(spec, _phases(), "P1_shock") == pytest.approx(0.02)


def test_evaluate_pair_is_abs_difference():
    spec = {"kind": "long_short_pair", "params": {"long": "NIFTY_IT", "short": "NIFTY_BANK"}}
    assert SR.evaluate_leg(spec, _phases(), "P1_shock") == pytest.approx(0.05 - 0.01)


def test_evaluate_benchmark_tilt_scales_excess():
    spec = {"kind": "benchmark_tilt", "params": {"sector": "NIFTY_IT", "weight": 0.5}}
    assert SR.evaluate_leg(spec, _phases(), "P1_shock") == pytest.approx(0.01)


def test_evaluate_basket_with_shorts_is_mean_abs_spread():
    spec = {"kind": "basket_rotation",
            "params": {"longs": ["NIFTY_IT", "NIFTY_FMCG"], "shorts": ["NIFTY_METAL"]}}
    # mean(0.05, 0.03) - mean(-0.04) = 0.04 + 0.04 = 0.08
    assert SR.evaluate_leg(spec, _phases(), "P1_shock") == pytest.approx(0.08)


def test_evaluate_long_only_basket_is_mean_excess():
    spec = {"kind": "basket_rotation", "params": {"longs": ["NIFTY_IT", "NIFTY_FMCG"]}}
    # mean(excess 0.02, 0.00) = 0.01
    assert SR.evaluate_leg(spec, _phases(), "P1_shock") == pytest.approx(0.01)


def test_evaluate_is_null_honest_on_missing_leg():
    spec = {"kind": "long_short_pair", "params": {"long": "NIFTY_IT", "short": "NIFTY_REALTY"}}
    assert SR.evaluate_leg(spec, _phases(), "P1_shock") is None      # REALTY absent
    # a partial basket is a different strategy, never fabricated
    basket = {"kind": "basket_rotation",
              "params": {"longs": ["NIFTY_IT", "NIFTY_REALTY"]}}
    assert SR.evaluate_leg(basket, _phases(), "P1_shock") is None
    # an absent phase -> None
    assert SR.evaluate_leg(spec, _phases(), "P3_resolution") is None


# ------------------------------------------------------------- future-blind

@pytest.mark.parametrize("spec", [
    {"kind": "long_sector", "params": {"sector": "NIFTY_IT"}},
    {"kind": "long_short_pair", "params": {"long": "NIFTY_IT", "short": "NIFTY_BANK"}},
    {"kind": "basket_rotation",
     "params": {"longs": ["NIFTY_IT", "NIFTY_FMCG"], "shorts": ["NIFTY_METAL"]}},
])
def test_evaluate_leg_is_future_blind(spec):
    """A later phase's data (dated AFTER the P1 window) must never change the
    P1 leg. Salt the episode with future phases and an extra future sector."""
    base = [_phases()]

    def salt(phase_dicts):
        d = json.loads(json.dumps(phase_dicts[0]))
        d["P2_basing"] = {"NIFTY_IT": {"abs": 9.9, "excess": 9.9},
                          "NIFTY_BANK": {"abs": -9.9, "excess": -9.9},
                          "NIFTY_FMCG": {"abs": 9.9, "excess": 9.9},
                          "NIFTY_METAL": {"abs": 9.9, "excess": 9.9}}
        d["P3_resolution"] = dict(d["P2_basing"])
        return [d]

    assert_future_blind(lambda rows: SR.evaluate_leg(spec, rows[0], "P1_shock"),
                        base, salt(base), label=f"evaluate_leg {spec['kind']}")


# ------------------------------------------------------------- aggregation

def test_aggregate_carries_ev_and_wilson_lower_bound():
    legs = [("e1", 0.05), ("e2", 0.03), ("e3", -0.01), ("e4", 0.02), ("e5", 0.04)]
    agg = SR._aggregate_returns(legs)
    assert agg["n"] == 5 and agg["wins"] == 4
    assert agg["hit_rate"] == 0.8
    assert agg["ev"] == pytest.approx(sum(v for _, v in legs) / 5, abs=1e-6)
    assert 0.0 <= agg["wilson_lb"] <= agg["hit_rate"]      # LB never above headline


def test_verdict_prefers_only_on_separation():
    assert SR._verdict([]) == "ABSTAIN"
    weak = [{"wilson_lb": 0.3, "hit_rate": 0.6, "bh_significant": False}]
    assert SR._verdict(weak) == "SHOW"                     # not significant -> SHOW
    sep = [{"wilson_lb": 0.65, "hit_rate": 0.9, "bh_significant": True},
           {"wilson_lb": 0.4, "hit_rate": 0.6, "bh_significant": False}]
    assert SR._verdict(sep) == "PREFER"                    # top LB beats runner headline
    noSep = [{"wilson_lb": 0.55, "hit_rate": 0.8, "bh_significant": True},
             {"wilson_lb": 0.5, "hit_rate": 0.7, "bh_significant": True}]
    assert SR._verdict(noSep) == "SHOW"                    # top LB below runner headline


# ------------------------------------------------------------- builder (real data)

def test_build_on_real_templates_is_well_formed():
    doc = SR.build_strategies(dry_run=True)
    assert doc["strategies"] and doc["placebo_report"]["total"] > 0
    for aid, phases in doc["table"].items():
        for ph, cell in phases.items():
            assert cell["verdict"] in ("PREFER", "SHOW", "ABSTAIN")
            lbs = [r["wilson_lb"] for r in cell["strategies"]]
            assert lbs == sorted(lbs, reverse=True)            # ranked on the honest LB
            for r in cell["strategies"]:
                assert r["n"] >= SR.MIN_EPISODE_LEGS           # support floor honored
                assert r["source"] != "placebo"               # placebos never in the real list


# ------------------------------------------------------------- query seam

def test_top_strategies_honest_absence(tmp_path):
    missing = tmp_path / "nope.json"
    out = SR.top_strategies("A1", "P1_shock", registry_path=missing)
    assert out["status"] == "unavailable" and out["strategies"] == []


def test_top_strategies_reads_written_artifact(tmp_path):
    art = tmp_path / "macro_strategies.json"
    SR.build_strategies(out_path=art)
    out = SR.top_strategies("A1", "P1_shock", k=3, registry_path=art)
    assert out["status"] == "ok"
    assert out["verdict"] in ("PREFER", "SHOW", "ABSTAIN")
    assert len(out["strategies"]) <= 3
    # a cell that does not exist is an honest no_cell, never a crash
    assert SR.top_strategies("A4", "P1_shock", registry_path=art)["status"] in (
        "no_cell", "ok")


# ------------------------------------------- declare() integration (live path)

def test_declare_attaches_strategy_slice(tmp_path):
    """A declared horizon carries the ranked recipes; an undeclared one gets
    None (mirroring playbook_slice)."""
    from src.analysis.macro_regime import declare
    art = tmp_path / "macro_strategies.json"
    SR.build_strategies(out_path=art)
    doc = declare(dry_run=True, strategies_path=art)
    for hz, v in doc["horizons"].items():
        if v["declared"]:
            assert v["strategy_slice"] is not None
            assert v["strategy_slice"]["status"] in ("ok", "no_cell")
        else:
            assert v["strategy_slice"] is None


def test_declare_fails_open_when_registry_missing(tmp_path):
    """A missing registry artifact NEVER blocks the declaration — the scoring
    clock must tick. The slice is an honest 'unavailable', not a crash."""
    from src.analysis.macro_regime import declare
    doc = declare(dry_run=True, strategies_path=tmp_path / "absent.json")
    assert "horizons" in doc                         # completed, no raise
    for v in doc["horizons"].values():
        if v["declared"]:
            assert v["strategy_slice"]["status"] in ("unavailable", "no_cell")

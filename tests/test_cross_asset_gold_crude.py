from src import brain_map
from src.strategies import cross_asset_gold_crude as cx
from src.validation import registry as rg


def _state(episode="rbi_hike_2010_11", phase="S2_peak_effect", declared=True):
    return {"horizons": {
        "shock": {"declared": False, "phase": None,
                  "best": {"archetype": "A1", "best_episode": "brexit_2016", "similarity": 0.69}},
        "slow_burn": {"declared": declared, "phase": phase,
                      "best": {"archetype": "S1", "best_episode": episode, "similarity": 0.74}},
    }}


def test_register_parks_at_trial_and_is_idempotent():
    conn = brain_map.connect(":memory:")
    first = cx.register_trial(conn)
    assert first["created"] and first["status"] == "TRIAL"
    again = cx.register_trial(conn)
    assert not again["created"] and again["status"] == "TRIAL"
    assert not rg.citable(conn, first["pattern_id"])
    trail = [r["to_status"] for r in rg.audit_trail(conn, first["pattern_id"])]
    assert trail == ["CANDIDATE", "TRIAL"]


def test_register_never_moves_a_later_status():
    conn = brain_map.connect(":memory:")
    pid = cx.register_trial(conn)["pattern_id"]
    rg.transition(conn, pid, "DEAD", "failed forward trial")
    assert cx.register_trial(conn)["status"] == "DEAD"


def test_inflation_regime_fires_shadow_stance_only():
    out = cx.evaluate(_state())
    assert out["fires"] and out["bucket"] == "CROSS_ASSET"
    assert out["authority"] == "NONE" and out["status"] == "TRIAL"
    assert out["stance"] == {"GOLD": "long", "CRUDEOIL": "long"}
    assert out["regime"]["horizon"] == "slow_burn"


def test_unwind_phase_flattens_crude():
    assert cx.evaluate(_state(phase="S3_unwind"))["stance"] == {"GOLD": "long", "CRUDEOIL": "flat"}


def test_abstains_with_named_reasons():
    assert cx.evaluate(None)["reason"] == "regime_state_unavailable"
    assert cx.evaluate(_state(episode="covid_crash"))["reason"] == "no_inflation_regime_declared"
    assert cx.evaluate(_state(declared=False))["fires"] is False

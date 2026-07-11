"""
Tests for the pattern×strategy evidence view (Phase 5, P5-4). Offline.

Run either of these from the project folder:
    python tests/test_strategy_evidence.py
    python -m pytest tests/test_strategy_evidence.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.discovery import strategy_evidence as se

_SEQ = [0]


def _add(conn, tag, archetype, result, corpus="real", r=None):
    """One resolved outcome carrying `tag`, in the real or sim corpus."""
    _SEQ[0] += 1
    i = _SEQ[0]
    ref = (f"sim:{i:06d}" if corpus == "sim"
           else f"2026-01-01|T{i}|BUY|{i}")
    if r is None:
        r = 1.0 if result == "win" else (-1.0 if result == "loss" else 0.0)
    oid = brain_map.record_outcome(conn, journal_ref=ref, date="2026-01-01",
                                   ticker=f"T{i}", archetype=archetype,
                                   r_multiple=r, result=result)
    eid = brain_map.record_event(conn, "2026-01-01", f"T{i}", "signal", tag,
                                 source=("sim" if corpus == "sim" else "journal"))
    brain_map.link_event_outcome(conn, eid, oid)


def _many(conn, tag, archetype, wins, losses, corpus="real"):
    for _ in range(wins):
        _add(conn, tag, archetype, "win", corpus)
    for _ in range(losses):
        _add(conn, tag, archetype, "loss", corpus)


# ----------------------------------------------------- breakdown

def test_breakdown_splits_corpus_and_applies_render_floor():
    conn = brain_map.connect(":memory:")
    _many(conn, "a", "iron_condor", wins=5, losses=1)          # 6 real
    _many(conn, "a", "iron_condor", wins=4, losses=0, corpus="sim")  # 4 sim
    _many(conn, "a", "bull_call", wins=2, losses=1)            # 3 real < floor

    bd = se.strategy_breakdown(conn, ["a"], min_real=5)
    by = {s["strategy"]: s for s in bd["strategies"]}
    assert by["iron_condor"]["renderable"] is True
    assert by["iron_condor"]["real"]["n"] == 6 and by["iron_condor"]["real"]["wins"] == 5
    assert by["iron_condor"]["sim"]["n"] == 4          # sim kept apart
    assert by["bull_call"]["renderable"] is False       # below the floor
    assert bd["total_real"] == 9 and bd["total_sim"] == 4


# ----------------------------------------------------- verdicts

def test_prefer_when_lower_bound_beats_the_rivals_headline():
    conn = brain_map.connect(":memory:")
    _many(conn, "b", "iron_condor", wins=13, losses=1)         # LB high
    _many(conn, "b", "bull_call", wins=3, losses=5)            # renderable, weak
    v = se.preferred_structure(se.strategy_breakdown(conn, ["b"], min_real=5))
    assert v["verdict"] == "PREFER" and v["structure"] == "iron_condor"


def test_abstain_when_top_structures_overlap():
    conn = brain_map.connect(":memory:")
    _many(conn, "c", "iron_condor", wins=12, losses=3)         # 80%, LB ~58%
    _many(conn, "c", "bull_call", wins=6, losses=1)            # 86% headline
    v = se.preferred_structure(se.strategy_breakdown(conn, ["c"], min_real=5))
    # iron_condor ranks first on the honest number, but bull_call's headline
    # overlaps its lower bound -> no honest preference.
    assert v["verdict"] == "ABSTAIN" and "noise" in v["reason"]


def test_abstain_when_nothing_clears_the_floor():
    conn = brain_map.connect(":memory:")
    _many(conn, "d", "iron_condor", wins=2, losses=1)          # 3 real total
    v = se.preferred_structure(se.strategy_breakdown(conn, ["d"], min_real=5))
    assert v["verdict"] == "ABSTAIN" and "real" in v["reason"]


def test_sim_evidence_can_never_earn_a_preference():
    conn = brain_map.connect(":memory:")
    _many(conn, "e", "iron_condor", wins=10, losses=0, corpus="sim")  # sim-only
    _many(conn, "e", "bull_call", wins=4, losses=1)            # 5 real
    bd = se.strategy_breakdown(conn, ["e"], min_real=5)
    by = {s["strategy"]: s for s in bd["strategies"]}
    assert by["iron_condor"]["renderable"] is False            # 10 sim wins ≠ evidence
    v = se.preferred_structure(bd)
    assert v["verdict"] == "PREFER" and v["structure"] == "bull_call"


def test_honest_number_ranks_above_the_headline():
    conn = brain_map.connect(":memory:")
    _many(conn, "f", "iron_condor", wins=12, losses=3)         # 80%, LB ~58%
    _many(conn, "f", "bull_call", wins=6, losses=1)            # 86%, LB ~47%
    bd = se.strategy_breakdown(conn, ["f"], min_real=5)
    # Ranked by Wilson LB, not the headline rate.
    assert bd["strategies"][0]["strategy"] == "iron_condor"
    assert (bd["strategies"][0]["real"]["wilson_lb"]
            > bd["strategies"][1]["real"]["wilson_lb"])


# ----------------------------------------------------- card

def test_card_states_one_verdict_and_carries_wilson_bounds():
    conn = brain_map.connect(":memory:")
    _many(conn, "g", "iron_condor", wins=13, losses=1)
    _many(conn, "g", "bull_call", wins=3, losses=5)
    out = se.view(conn, ["g"], min_real=5)
    card = out["card"]
    assert "LB" in card and "iron_condor" in card
    # Exactly one directional verdict line (composition law: one verdict).
    assert card.count("➡️") == 1
    assert "PREFER iron_condor" in card


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

"""
Tests for the pattern registry & lifecycle (Phase 4, P4-2). Offline.

Run either of these from the project folder:
    python tests/test_pattern_registry.py
    python -m pytest tests/test_pattern_registry.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.validation import registry as rg
from src.validation import stat_gates as sg


DEFN = {"kind": "itemset", "tags": ["golden_cross", "fii_buying"],
        "regime_vix": "mid"}


def test_register_is_idempotent_on_the_frozen_definition():
    conn = brain_map.connect(":memory:")
    first = rg.register(conn, "itemset", DEFN, mining_run="run-1",
                        support_n=18, fdr_q=0.04)
    assert first["created"] is True and first["status"] == "CANDIDATE"
    # Same predicate, different key order -> SAME hypothesis, no new row.
    reordered = {"regime_vix": "mid", "kind": "itemset",
                 "tags": ["golden_cross", "fii_buying"]}
    again = rg.register(conn, "itemset", reordered, mining_run="run-2")
    assert again["created"] is False
    assert again["pattern_id"] == first["pattern_id"]
    # A changed predicate is a NEW hypothesis.
    other = rg.register(conn, "itemset", dict(DEFN, regime_vix="high"))
    assert other["pattern_id"] != first["pattern_id"]


def test_lifecycle_enforces_legal_transitions_with_audit():
    conn = brain_map.connect(":memory:")
    pid = rg.register(conn, "itemset", DEFN)["pattern_id"]
    # Illegal: CANDIDATE cannot jump straight to VALIDATED.
    v = rg.transition(conn, pid, "VALIDATED", "trying to skip the trial")
    assert v["ok"] is False and "illegal" in v["reason"]
    # The honest road.
    assert rg.transition(conn, pid, "TRIAL", "walk-forward begins")["ok"]
    assert rg.transition(conn, pid, "VALIDATED", "cleared gates")["ok"]
    assert rg.get(conn, pid)["promoted_at"] is not None
    assert rg.transition(conn, pid, "LIVE_ADVISORY", "surfacing")["ok"]
    trail = rg.audit_trail(conn, pid)
    assert [t["to_status"] for t in trail] == [
        "CANDIDATE", "TRIAL", "VALIDATED", "LIVE_ADVISORY"]
    # Unregistered id fails soft.
    assert rg.transition(conn, "ghost", "TRIAL", "x")["ok"] is False


def test_second_quarantine_is_dead_and_dead_is_terminal():
    conn = brain_map.connect(":memory:")
    pid = rg.register(conn, "sequence", dict(DEFN, kind="sequence"))["pattern_id"]
    rg.transition(conn, pid, "TRIAL", "t")
    rg.transition(conn, pid, "VALIDATED", "v")
    assert rg.transition(conn, pid, "QUARANTINED", "CUSUM breach")["ok"]
    # Re-trial and re-validate — allowed once.
    rg.transition(conn, pid, "TRIAL", "re-trial")
    rg.transition(conn, pid, "VALIDATED", "re-validated")
    second = rg.transition(conn, pid, "QUARANTINED", "breached again")
    assert second["status"] == "DEAD"                    # forced landing
    assert rg.get(conn, pid)["retired_at"] is not None
    # Terminal: nothing leaves DEAD — and re-discovery stays DEAD.
    assert rg.transition(conn, pid, "TRIAL", "necromancy")["ok"] is False
    re_reg = rg.register(conn, "sequence", dict(DEFN, kind="sequence"))
    assert re_reg["created"] is False and re_reg["status"] == "DEAD"


def test_insufficient_n_is_distinct_from_failure():
    conn = brain_map.connect(":memory:")
    pid = rg.register(conn, "itemset", dict(DEFN, tags=["a"]))["pattern_id"]
    rg.transition(conn, pid, "INSUFFICIENT_N", "3/10 resolutions so far")
    row = rg.get(conn, pid)
    assert row["status"] == "INSUFFICIENT_N"
    # It can come back when evidence accrues.
    assert rg.transition(conn, pid, "TRIAL", "evidence arrived")["ok"]


def test_citable_gate_and_minted_tags_are_miner_excluded():
    conn = brain_map.connect(":memory:")
    pid = rg.register(conn, "itemset", dict(DEFN, tags=["b"]))["pattern_id"]
    assert rg.citable(conn, pid) is False                # CANDIDATE: dark
    rg.transition(conn, pid, "TRIAL", "t")
    assert rg.citable(conn, pid) is False
    rg.transition(conn, pid, "VALIDATED", "v")
    assert rg.citable(conn, pid) is True
    tag = rg.mint_tag(pid)
    assert tag.startswith("auto:")
    assert not sg.is_minable_tag(tag)                    # no self-rediscovery


def test_oos_stats_accumulate_monotonically():
    conn = brain_map.connect(":memory:")
    pid = rg.register(conn, "itemset", dict(DEFN, tags=["c"]))["pattern_id"]
    rg.update_oos_stats(conn, pid, {"n": 6, "wins": 4})
    rg.update_oos_stats(conn, pid, {"n": 11, "wins": 7, "window": "w2"})
    import json
    stats = json.loads(rg.get(conn, pid)["oos_stats"])
    assert stats["n"] == 11 and stats["window"] == "w2"
    assert rg.update_oos_stats(conn, "ghost", {}) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

"""
Tests for placebo patterns / the realized-FDR meter (Phase 4, P4-5).
Offline, deterministic seeds.

Run either of these from the project folder:
    python tests/test_placebo.py
    python -m pytest tests/test_placebo.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.validation import placebo as pb
from src.validation import registry as rg


def test_seed_batch_registers_hidden_placebos():
    conn = brain_map.connect(":memory:")
    rng = random.Random(1)
    ids = pb.seed_batch(conn, "era-1", ["golden_cross", "fii_buying", "rsi_ov"],
                        rng, count=10)
    assert len(ids) == 10
    # They look like ordinary CANDIDATEs to the registry (harness is blind).
    for pid in ids:
        assert rg.get(conn, pid)["status"] == "CANDIDATE"
        assert pb.is_placebo(conn, pid) is True
    # A real pattern is NOT flagged placebo.
    real = rg.register(conn, "itemset", {"tags": ["x"]})["pattern_id"]
    assert pb.is_placebo(conn, real) is False


def test_insufficient_n_is_honest():
    conn = brain_map.connect(":memory:")
    rng = random.Random(2)
    ids = pb.seed_batch(conn, "era-1", ["a", "b", "c"], rng, count=5)
    for pid in ids:
        pb.record_placebo_outcome(conn, pid)
    fdr = pb.realized_fdr(conn)
    assert fdr["state"] == "insufficient placebo n" and fdr["alarm"] is False


def test_healthy_harness_holds_placebos_and_no_alarm():
    conn = brain_map.connect(":memory:")
    rng = random.Random(3)
    # Two eras of placebos; a WELL-behaved harness validates none of them.
    for era in ("era-1", "era-2", "era-3"):
        pb.seed_batch(conn, era, ["a", "b", "c", "d"], rng, count=10)
    summary = {}
    for era in ("era-1", "era-2", "era-3"):
        summary = pb.audit_batch(conn, era)   # nobody was promoted
    fdr = summary["fdr"]
    assert fdr["n"] == 30 and fdr["validated"] == 0
    assert fdr["state"] == "measured" and fdr["alarm"] is False
    assert fdr["rate"] == 0.0


def test_excess_placebo_validation_raises_the_alarm():
    conn = brain_map.connect(":memory:")
    rng = random.Random(4)
    ids = pb.seed_batch(conn, "era-1", ["a", "b", "c"], rng, count=25)
    # Simulate a BROKEN (too-loose) harness: validate most placebos.
    for pid in ids[:18]:
        rg.transition(conn, pid, "TRIAL", "t")
        rg.transition(conn, pid, "VALIDATED", "leaked")
    fdr = pb.audit_batch(conn, "era-1")["fdr"]
    assert fdr["validated"] == 18 and fdr["rate"] > 0.5
    assert fdr["alarm"] is True                # Wilson LB > designed q


def test_placebos_are_excluded_from_miner_tag_vocabulary():
    # The auto/placebo namespaces stat_gates excludes cover the minted tags.
    from src.validation import stat_gates as sg
    assert not sg.is_minable_tag("auto:abc12345")
    assert sg.is_learnable_ref("placebo:x") is False


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

"""
Tests for the weekly harness digest (Phase 4, P4-6). Offline.

Run either of these from the project folder:
    python tests/test_digest.py
    python -m pytest tests/test_digest.py
"""

import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.validation import digest as dg
from src.validation import placebo as pb
from src.validation import registry as rg


def test_empty_harness_reads_as_honest_silence():
    conn = brain_map.connect(":memory:")
    card = dg.build_digest(conn, today=date(2026, 7, 11))
    assert "no patterns yet" in card and "earned" in card


def test_digest_reports_validated_killed_and_placebo_rate():
    conn = brain_map.connect(":memory:")
    # A validated pattern this week.
    good = rg.register(conn, "itemset", {"tags": ["gc", "fii"]},
                       description="golden cross + fii buying")["pattern_id"]
    rg.transition(conn, good, "TRIAL", "t")
    rg.transition(conn, good, "VALIDATED", "cleared")
    rg.update_oos_stats(conn, good, {"real": {"n": 12, "wins": 9}})
    # A killed one this week.
    bad = rg.register(conn, "itemset", {"tags": ["x"]})["pattern_id"]
    rg.transition(conn, bad, "TRIAL", "t")
    rg.transition(conn, bad, "VALIDATED", "v")
    rg.transition(conn, bad, "QUARANTINED", "CUSUM drift")
    # A measured (healthy) placebo rate.
    rng = random.Random(5)
    for era in ("e1", "e2", "e3"):
        pb.seed_batch(conn, era, ["a", "b", "c"], rng, count=10)
        pb.audit_batch(conn, era)

    card = dg.build_digest(conn, today=date(2026, 7, 11))
    assert "Validated this week" in card and "auto:" in card
    assert "9/12 OOS" in card and "LB" in card          # Wilson shown
    assert "Killed this week" in card and "quarantined" in card
    assert "Placebo false-discovery rate: 0%" in card    # healthy harness
    assert "🚨" not in card                               # no alarm


def test_digest_flags_a_loose_harness_alarm():
    conn = brain_map.connect(":memory:")
    rng = random.Random(6)
    ids = pb.seed_batch(conn, "e1", ["a", "b", "c"], rng, count=25)
    for pid in ids[:18]:                                 # broken: leaks nulls
        rg.transition(conn, pid, "TRIAL", "t")
        rg.transition(conn, pid, "VALIDATED", "leaked")
    pb.audit_batch(conn, "e1")
    card = dg.build_digest(conn, today=date(2026, 7, 11))
    assert "🚨" in card and "too loose" in card


def test_run_sends_via_notify_fn():
    conn = brain_map.connect(":memory:")
    sent = []
    out = dg.run(conn=conn, today=date(2026, 7, 11),
                 notify_fn=lambda t: sent.append(t))
    assert out["sent"] is True and len(sent) == 1
    assert out["card"].startswith("🔬")


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

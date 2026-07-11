"""
Tests for the discovery-pass orchestrator (Phase 5, P5-3). Offline.

Run either of these from the project folder:
    python tests/test_run_miners.py
    python -m pytest tests/test_run_miners.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.discovery import run_miners as rm


def test_empty_db_reports_honest_zero_with_the_reason():
    conn = brain_map.connect(":memory:")
    report = rm.run_all(conn, today=__import__("datetime").date(2026, 7, 11))
    assert report["totals"]["survivors"] == 0
    assert report["totals"]["errors"] == 0
    # Both miners × both corpora ran.
    assert len(report["runs"]) == 4
    assert "CORRECT" in report["summary"] and "earned it" in report["summary"]


def test_pass_survives_one_miner_throwing():
    conn = brain_map.connect(":memory:")

    class Boom:
        @staticmethod
        def run(**kw):
            raise RuntimeError("miner exploded")

    original = rm.MINERS
    rm.MINERS = (("cooccurrence", rm.cm), ("boom", Boom))
    try:
        report = rm.run_all(conn,
                            today=__import__("datetime").date(2026, 7, 11))
    finally:
        rm.MINERS = original
    # The good miner still ran both corpora; the boom miner logged 2 errors.
    assert report["totals"]["errors"] == 2
    assert any("error" in r for r in report["runs"])
    assert any(r.get("miner") == "cooccurrence" for r in report["runs"])


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

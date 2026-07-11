"""
Tests for the validation drift monitor (Phase 4, P4-4). Offline.

Run either of these from the project folder:
    python tests/test_monitor.py
    python -m pytest tests/test_monitor.py
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.validation import monitor as mon
from src.validation import registry as rg
from src.validation import trial as tr


def _validated_pattern(conn, vrate_wins=9, vrate_n=11, promoted="2026-01-01"):
    pid = rg.register(conn, "itemset", {"tags": ["gc", "fii"]})["pattern_id"]
    rg.transition(conn, pid, "TRIAL", "t")
    rg.transition(conn, pid, "VALIDATED", "v")
    rg.update_oos_stats(conn, pid, {"real": {"n": vrate_n, "wins": vrate_wins}})
    conn.execute("UPDATE candidate_patterns SET promoted_at = ? "
                 "WHERE pattern_id = ?", (promoted, pid))
    conn.commit()
    return pid


def _live(conn, pid, results, start_day=2):
    for i, r in enumerate(results):
        day = f"2026-02-{start_day + i:02d}"
        ref = tr.record_shadow_fire(conn, pid, day, "X")["ref"]
        tr.resolve_shadow(conn, ref, "win" if r else "loss",
                          1.5 if r else -1.0, day)


def test_cusum_breaches_on_a_losing_streak_not_on_noise():
    healthy = [1, 0, 1, 1, 0, 1, 1, 0, 1, 1]      # ~70%, matches validation
    assert mon.cusum_breach(healthy, 0.7)["breached"] is False
    bleeding = [1, 1] + [0] * 12                   # 2/14 — fell off a cliff
    assert mon.cusum_breach(bleeding, 0.7)["breached"] is True
    assert mon.cusum_breach([], 0.7)["breached"] is False


def test_drifting_pattern_auto_quarantines_with_a_card():
    conn = brain_map.connect(":memory:")
    pid = _validated_pattern(conn)
    _live(conn, pid, [0] * 10)                    # ten straight losses live
    res = mon.check_pattern(conn, pid, today=date(2026, 2, 20))
    assert res["action"] == "quarantined"
    assert rg.get(conn, pid)["status"] == "QUARANTINED"
    assert "QUARANTINED" in res["card"] and "auto:" in res["card"]


def test_healthy_live_pattern_is_held():
    conn = brain_map.connect(":memory:")
    pid = _validated_pattern(conn)
    _live(conn, pid, [1, 0, 1, 1, 1, 0, 1, 1])    # still ~75%
    res = mon.check_pattern(conn, pid, today=date(2026, 2, 20))
    assert res["action"] == "held" and res["card"] is None
    assert rg.get(conn, pid)["status"] == "VALIDATED"


def test_second_quarantine_is_death():
    conn = brain_map.connect(":memory:")
    pid = _validated_pattern(conn)
    rg.transition(conn, pid, "QUARANTINED", "first strike")
    rg.transition(conn, pid, "TRIAL", "re-trial")
    rg.transition(conn, pid, "VALIDATED", "re-validated")
    conn.execute("UPDATE candidate_patterns SET promoted_at='2026-01-01' "
                 "WHERE pattern_id=?", (pid,)); conn.commit()
    _live(conn, pid, [0] * 10)
    res = mon.check_pattern(conn, pid, today=date(2026, 2, 20))
    assert res["action"] == "dead"
    assert rg.get(conn, pid)["status"] == "DEAD"
    assert "DIED" in res["card"]


def test_lease_expiry_demotes_to_candidate_for_retrial():
    conn = brain_map.connect(":memory:")
    pid = _validated_pattern(conn, promoted="2026-01-01")
    # Healthy but old: 8 wins keeps it un-drifted; >90 days triggers lease.
    _live(conn, pid, [1, 1, 1, 1, 1, 1, 1, 1])
    res = mon.check_pattern(conn, pid, today=date(2026, 5, 1))   # ~120 days
    assert res["action"] == "expired_to_candidate"
    assert rg.get(conn, pid)["status"] == "CANDIDATE"


def test_run_sweep_summary_and_single_card_per_event():
    conn = brain_map.connect(":memory:")
    good = _validated_pattern(conn)
    _live(conn, good, [1, 1, 1, 0, 1, 1])
    bad = rg.register(conn, "itemset", {"tags": ["x"]})["pattern_id"]
    rg.transition(conn, bad, "TRIAL", "t"); rg.transition(conn, bad, "VALIDATED", "v")
    rg.update_oos_stats(conn, bad, {"real": {"n": 10, "wins": 8}})
    conn.execute("UPDATE candidate_patterns SET promoted_at='2026-01-01' WHERE pattern_id=?", (bad,)); conn.commit()
    _live(conn, bad, [0] * 10)
    cards = []
    summary = mon.run_sweep(conn, today=date(2026, 2, 20),
                            notify_fn=lambda t: cards.append(t))
    assert summary["checked"] == 2
    assert summary["quarantined"] == 1 and summary["held"] == 1
    assert len(cards) == 1


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

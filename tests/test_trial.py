"""
Tests for walk-forward trials + shadow tracking (Phase 4, P4-3). Offline.

Run either of these from the project folder:
    python tests/test_trial.py
    python -m pytest tests/test_trial.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.validation import registry as rg
from src.validation import trial as tr


DEFN = {"kind": "itemset", "tags": ["golden_cross", "fii_buying"]}


def _register(conn):
    return rg.register(conn, "itemset", DEFN)["pattern_id"]


def test_split_windows_embargo_and_membership():
    days = [f"2026-01-{d:02d}" for d in range(1, 21)]      # 20 days
    w = tr.split_windows(days, discovery_frac=0.6, embargo_days=5)
    assert w["discovery_end"] == "2026-01-13"             # 0.6*20 -> index 12
    assert w["validation_start"] == "2026-01-18"          # +5 embargo
    assert tr.in_validation("2026-01-19", w) is True
    assert tr.in_validation("2026-01-15", w) is False     # inside embargo
    assert tr.in_validation("2026-01-10", w) is False     # discovery
    # Too few days -> no bounds, nothing counts as validation.
    assert tr.in_validation("2026-01-01", tr.split_windows(["2026-01-01"])) is False


def test_shadow_fire_is_idempotent_and_never_touches_journal():
    conn = brain_map.connect(":memory:")
    pid = _register(conn)
    a = tr.record_shadow_fire(conn, pid, "2026-02-02", "NIFTY 50", "bearish")
    b = tr.record_shadow_fire(conn, pid, "2026-02-02", "NIFTY 50", "bearish")
    assert a["created"] is True and b["created"] is False   # same day+ticker
    assert a["ref"].startswith("shadow:")
    n = conn.execute("SELECT COUNT(*) AS n FROM shadow_trades").fetchone()["n"]
    assert n == 1
    # No journal table/rows were created by shadow tracking.
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "shadow_trades" in tables


def test_resolve_shadow_first_write_wins():
    conn = brain_map.connect(":memory:")
    pid = _register(conn)
    ref = tr.record_shadow_fire(conn, pid, "2026-02-02", "X")["ref"]
    assert tr.resolve_shadow(conn, ref, "win", 1.5, "2026-02-09") is True
    assert tr.resolve_shadow(conn, ref, "loss", -1.0, "2026-02-09") is False
    row = conn.execute("SELECT result FROM shadow_trades "
                       "WHERE journal_ref = ?", (ref,)).fetchone()
    assert row["result"] == "win"
    assert tr.resolve_shadow(conn, "shadow:ghost", "win", 1.0, "x") is False


def test_shadow_evidence_counts_validation_window_only():
    conn = brain_map.connect(":memory:")
    pid = _register(conn)
    w = {"validation_start": "2026-03-01"}
    # One in-discovery firing (before the window) and two out-of-sample.
    for day, res in (("2026-02-20", "win"), ("2026-03-05", "win"),
                     ("2026-03-06", "loss")):
        ref = tr.record_shadow_fire(conn, pid, day, "X")["ref"]
        tr.resolve_shadow(conn, ref, res, 1.0 if res == "win" else -1.0, day)
    ev = tr.shadow_evidence(conn, pid, w)
    assert ev["n"] == 2 and ev["wins"] == 1               # pre-window excluded
    assert tr.shadow_evidence(conn, pid)["n"] == 3        # unrestricted


def test_evaluate_trial_promotes_only_with_real_oos_superiority():
    conn = brain_map.connect(":memory:")
    pid = _register(conn)
    w = {"validation_start": "2026-03-01"}
    # 9 real OOS wins in 11 -> Wilson LB well above a 0.4 breakeven null.
    for i in range(11):
        day = f"2026-03-{i + 2:02d}"
        ref = tr.record_shadow_fire(conn, pid, day, "X")["ref"]
        res = "win" if i < 9 else "loss"
        tr.resolve_shadow(conn, ref, res, 1.5 if res == "win" else -1.0, day)
    v = tr.evaluate_trial(conn, pid, w, avg_win_r=1.5, avg_loss_r=1.0)
    assert v["promote"] is True and v["final_status"] == "VALIDATED"
    # oos_stats persisted on the registry row.
    import json
    stats = json.loads(rg.get(conn, pid)["oos_stats"])
    assert stats["real"]["n"] == 11 and stats["evaluated"] is True


def test_evaluate_trial_sim_only_stays_insufficient():
    conn = brain_map.connect(":memory:")
    pid = _register(conn)
    w = {"validation_start": "2026-03-01"}
    # Loads of SIM evidence, zero real shadow resolutions -> never promotes.
    v = tr.evaluate_trial(conn, pid, w, sim_evidence={"n": 80, "wins": 65},
                          avg_win_r=1.5, avg_loss_r=1.0)
    assert v["promote"] is False
    assert v["final_status"] == "INSUFFICIENT_N"
    assert "sim-only" in v["reason"] or "no real" in v["reason"]


def test_learning_corpus_filter_blocks_self_poisoning():
    refs = ["ab12cd34", "sim:x", "shadow:y", "trial:z", "placebo:q", "ef56gh78"]
    assert tr.learning_corpus_filter(refs) == ["ab12cd34", "ef56gh78"]
    assert tr.learning_corpus_filter([]) == []


def test_shadow_tracking_never_writes_journal_or_portfolio():
    """Runtime spy (the decision-#36 test pattern): the whole shadow
    lifecycle must not call journal.log / journal.rewrite_all or write
    portfolio.json."""
    from src import journal
    conn = brain_map.connect(":memory:")
    pid = _register(conn)
    calls = []
    orig_log = journal.log
    journal.log = lambda *a, **k: calls.append("log")
    try:
        ref = tr.record_shadow_fire(conn, pid, "2026-03-05", "X")["ref"]
        tr.resolve_shadow(conn, ref, "win", 1.5, "2026-03-12")
        tr.evaluate_trial(conn, pid, {"validation_start": "2026-03-01"})
    finally:
        journal.log = orig_log
    assert calls == []                                    # journal untouched


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

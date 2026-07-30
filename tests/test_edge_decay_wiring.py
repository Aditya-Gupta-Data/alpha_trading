"""Task K — knowledge-graph EDGE decay wired into the 20:00 sleep_phase pass
(owner-approved 2026-07-30; closes the Phase-1 audit's UNWIRED finding).

Hermetic: in-memory sqlite only. The muzzle tests prove the suite cannot
decay the REAL brain_map.db even when a caller forgets to sandbox it —
the failure mode that put 4 fixture rows into production on 2026-07-27.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src import brain_map, decay_engine, sleep_phase


def _graph_conn():
    """A sandbox DB carrying just the graph_edges shape decay_engine needs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE graph_edges (
            subject TEXT, predicate TEXT, object TEXT,
            confidence_score REAL
        )""")
    conn.commit()
    return conn


def _add_edge(conn, subject="TCS", confidence=0.9, age_days=10.0):
    decay_engine.migrate_schema(conn)
    stamp = (datetime.now(timezone.utc)
             - timedelta(days=age_days)).isoformat()
    conn.execute(
        "INSERT INTO graph_edges (subject, predicate, object, "
        "confidence_score, valid_from, decay_lambda) VALUES (?,?,?,?,?,?)",
        (subject, "caused", "drawdown", confidence, stamp, 0.05))
    conn.commit()


# ----------------------------------------------------------- the decay works

def test_edge_decay_actually_reduces_confidence():
    conn = _graph_conn()
    _add_edge(conn, confidence=0.9, age_days=10.0)

    stats = sleep_phase.run_edge_decay(conn)

    assert stats["swept"] == 1 and stats["expired"] == 0
    new = conn.execute("SELECT confidence_score FROM graph_edges").fetchone()[0]
    # w = 0.9 * exp(-0.05 * 10) = 0.5458...
    assert new == pytest.approx(0.9 * 2.718281828 ** -0.5, rel=1e-3)
    assert new < 0.9


def test_edge_below_threshold_is_expired_not_deleted():
    conn = _graph_conn()
    _add_edge(conn, confidence=0.15, age_days=30.0)   # 0.15*e^-1.5 = 0.033

    stats = sleep_phase.run_edge_decay(conn)

    assert stats["expired"] == 1
    row = conn.execute(
        "SELECT confidence_score, invalid_at FROM graph_edges").fetchone()
    assert row["invalid_at"] is not None      # stamped...
    assert row["confidence_score"] < decay_engine.DECAY_THRESHOLD
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_edges").fetchone()[0] == 1   # ...not deleted


def test_second_sweep_same_day_does_not_double_decay():
    """Decay is a function of elapsed time, not of call count."""
    conn = _graph_conn()
    _add_edge(conn, confidence=0.9, age_days=10.0)

    sleep_phase.run_edge_decay(conn)
    after_first = conn.execute(
        "SELECT confidence_score FROM graph_edges").fetchone()[0]
    sleep_phase.run_edge_decay(conn)
    after_second = conn.execute(
        "SELECT confidence_score FROM graph_edges").fetchone()[0]

    assert after_second == pytest.approx(after_first, rel=1e-4)


# ------------------------------------------------------------- the muzzle

def test_muzzle_refuses_a_connection_holding_the_real_brain_map(tmp_path,
                                                                monkeypatch):
    """The exact 2026-07-27 failure mode: a caller forgets to sandbox and
    the task is handed the production DB. It must decay NOTHING."""
    fake_real = tmp_path / "brain_map.db"
    conn = sqlite3.connect(fake_real)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE graph_edges (subject TEXT, predicate TEXT, "
                 "object TEXT, confidence_score REAL)")
    conn.commit()
    _add_edge(conn, confidence=0.9, age_days=10.0)
    # Point the sensor's notion of "the real DB" at this file.
    monkeypatch.setattr(brain_map, "DEFAULT_DB_PATH", fake_real)

    stats = sleep_phase.run_edge_decay(conn)

    assert stats == {"skipped": "muzzled_under_pytest"}
    untouched = conn.execute(
        "SELECT confidence_score FROM graph_edges").fetchone()[0]
    assert untouched == 0.9          # not decayed by a single step


def test_muzzle_fails_SAFE_when_the_path_cannot_be_read(monkeypatch):
    """A muzzle that fails open is not a muzzle. If the sensor cannot tell
    what database it is holding, it must assume the real one."""
    class _Opaque:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("cannot inspect")

    assert sleep_phase._targets_the_real_brain_map(_Opaque()) is True
    assert sleep_phase.run_edge_decay(_Opaque()) == {
        "skipped": "muzzled_under_pytest"}


def test_sandboxed_memory_db_is_not_muzzled():
    """The muzzle must not be so broad it blocks legitimate test decay —
    otherwise the tests above would pass while proving nothing."""
    conn = _graph_conn()
    assert sleep_phase._targets_the_real_brain_map(conn) is False


def test_full_pass_never_reaches_the_real_brain_map(monkeypatch):
    """Bomb test: run_sleep_phase with an in-memory DB must not open the
    production database through ANY task, Task K included."""
    def _bomb(*a, **k):
        raise AssertionError("real brain_map.connect() reached from a test")

    real_connect = brain_map.connect

    def _guarded(db_path=None):
        if db_path in (None, str(brain_map.DEFAULT_DB_PATH)):
            _bomb()
        return real_connect(db_path)

    monkeypatch.setattr(brain_map, "connect", _guarded)
    monkeypatch.setattr(sleep_phase.brain_map, "connect", _guarded)

    class _NoLLM:
        base_url = "http://127.0.0.1:1"

        def is_reachable(self):
            return False

    results = sleep_phase.run_sleep_phase(db_path=":memory:",
                                          extractor=_NoLLM())
    assert "edge_decay" in results


# ---------------------------------------------------------------- fail-open

def test_task_k_failure_is_logged_and_the_pass_still_returns(monkeypatch,
                                                              capsys):
    """Constraint 1: a crashing decay engine must not take the pass down."""
    def _boom(conn, *a, **k):
        raise RuntimeError("graph_edges is on fire")

    monkeypatch.setattr(decay_engine, "apply_decay_sweep", _boom)

    class _NoLLM:
        base_url = "http://127.0.0.1:1"

        def is_reachable(self):
            return False

    results = sleep_phase.run_sleep_phase(db_path=":memory:",
                                          extractor=_NoLLM())

    assert results["edge_decay"] is None            # named as failed
    assert "K. edge decay failed" in capsys.readouterr().out
    # ...and every other task still reported, i.e. the pass completed.
    for key in ("ingestion", "consolidation", "decay", "causal",
                "shadow_sweep", "h4_shadow"):
        assert key in results


def test_task_k_runs_inside_the_real_pass_and_is_reported():
    """Wiring proof: the task is actually reached by run_sleep_phase and
    lands in the results dict under its own key."""
    class _NoLLM:
        base_url = "http://127.0.0.1:1"

        def is_reachable(self):
            return False

    results = sleep_phase.run_sleep_phase(db_path=":memory:",
                                          extractor=_NoLLM())
    assert "edge_decay" in results
    assert results["edge_decay"] is not None        # ran, did not crash

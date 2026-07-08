"""
Tests for Phase 6E: Temporal Financial Signal Decay
(src/decay_engine.py + graph_engine.py temporal columns)

All tests run against throwaway ':memory:' SQLite connections — no network
calls, no real data/brain_map.db ever touched (HANDOVER's "never reset live
data" rule). Time is controlled by pre-seeding valid_from timestamps to
specific past dates rather than mocking datetime, so the tests are
deterministic without patching stdlib.

Run:
    python tests/test_decay.py
    pytest tests/test_decay.py -v
"""

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.decay_engine import (
    DECAY_THRESHOLD,
    DEFAULT_LAMBDA,
    apply_decay_sweep,
    migrate_schema,
)
from src.graph_engine import GraphEngine, add_edge, ensure_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_conn():
    """In-memory brain_map connection with graph_edges schema fully set up."""
    conn = brain_map.connect(":memory:")
    ensure_schema(conn)
    return conn


def _ts(days_ago: float) -> str:
    """ISO-8601 UTC timestamp that is `days_ago` days in the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


def _set_valid_from(conn, rowid: int, days_ago: float) -> None:
    """Back-date a row's valid_from to simulate age."""
    conn.execute(
        "UPDATE graph_edges SET valid_from = ? WHERE rowid = ?",
        (_ts(days_ago), rowid),
    )
    conn.commit()


def _get_row(conn, src, rel, tgt):
    """Fetch a single graph_edges row as a dict-like row."""
    return conn.execute(
        "SELECT rowid, confidence_score, valid_from, invalid_at, decay_lambda "
        "FROM graph_edges WHERE source_node=? AND relation=? AND target_node=?",
        (src, rel, tgt),
    ).fetchone()


# ---------------------------------------------------------------------------
# 1. Schema migration
# ---------------------------------------------------------------------------

def test_ensure_schema_adds_temporal_columns():
    """ensure_schema creates valid_from, invalid_at, decay_lambda columns."""
    conn = brain_map.connect(":memory:")
    ensure_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(graph_edges)")}
    assert "valid_from" in cols
    assert "invalid_at" in cols
    assert "decay_lambda" in cols


def test_migrate_schema_is_idempotent():
    """Calling migrate_schema twice on same connection never raises."""
    conn = _fresh_conn()
    migrate_schema(conn)
    migrate_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(graph_edges)")}
    assert "valid_from" in cols


def test_migrate_schema_on_empty_db_is_safe():
    """migrate_schema on a DB with no graph_edges table returns silently."""
    conn = brain_map.connect(":memory:")
    migrate_schema(conn)  # graph_edges does not exist yet — must not raise


# ---------------------------------------------------------------------------
# 2. add_edge stamps valid_from and decay_lambda
# ---------------------------------------------------------------------------

def test_add_edge_stamps_valid_from():
    """A freshly inserted edge has valid_from set to roughly now."""
    conn = _fresh_conn()
    add_edge(conn, "A", "leads", "B", confidence_score=0.9)
    row = _get_row(conn, "A", "leads", "B")
    assert row["valid_from"] is not None
    dt = datetime.fromisoformat(row["valid_from"])
    age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    assert age_seconds < 5  # written less than 5 s ago


def test_add_edge_stamps_decay_lambda():
    """A freshly inserted edge gets the default decay_lambda."""
    conn = _fresh_conn()
    add_edge(conn, "A", "leads", "B", confidence_score=0.8)
    row = _get_row(conn, "A", "leads", "B")
    assert row["decay_lambda"] == DEFAULT_LAMBDA


def test_add_edge_reinforce_resets_valid_from_and_clears_invalid_at():
    """Calling add_edge on an expired edge reactivates it: invalid_at → NULL,
    valid_from resets to now."""
    conn = _fresh_conn()
    add_edge(conn, "A", "leads", "B", confidence_score=0.9)
    row = _get_row(conn, "A", "leads", "B")
    rowid = row["rowid"]
    # Simulate expiry by stamping invalid_at manually.
    conn.execute(
        "UPDATE graph_edges SET invalid_at = ? WHERE rowid = ?",
        (_ts(0), rowid),
    )
    conn.commit()
    # Re-observe the same pattern.
    add_edge(conn, "A", "leads", "B", confidence_score=0.9)
    row = _get_row(conn, "A", "leads", "B")
    assert row["invalid_at"] is None, "reinforce must clear invalid_at"
    dt = datetime.fromisoformat(row["valid_from"])
    age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    assert age_seconds < 5, "reinforce must reset valid_from to now"


# ---------------------------------------------------------------------------
# 3. Basic decay formula
# ---------------------------------------------------------------------------

def test_decay_formula_one_day():
    """After 1 day of age, weight = w0 * exp(-lambda * 1)."""
    conn = _fresh_conn()
    w0 = 0.8
    add_edge(conn, "X", "r", "Y", confidence_score=w0)
    row = _get_row(conn, "X", "r", "Y")
    _set_valid_from(conn, row["rowid"], days_ago=1.0)

    apply_decay_sweep(conn, default_lambda=DEFAULT_LAMBDA)

    row = _get_row(conn, "X", "r", "Y")
    expected = w0 * math.exp(-DEFAULT_LAMBDA * 1.0)
    assert abs(row["confidence_score"] - expected) < 1e-6


def test_decay_formula_two_days():
    """After 2 days, weight = w0 * exp(-lambda * 2)."""
    conn = _fresh_conn()
    w0 = 1.0
    add_edge(conn, "X", "r", "Y", confidence_score=w0)
    row = _get_row(conn, "X", "r", "Y")
    _set_valid_from(conn, row["rowid"], days_ago=2.0)

    apply_decay_sweep(conn)

    row = _get_row(conn, "X", "r", "Y")
    expected = w0 * math.exp(-DEFAULT_LAMBDA * 2.0)
    assert abs(row["confidence_score"] - expected) < 1e-6


def test_decay_custom_lambda():
    """Custom decay_lambda overrides the default for that edge."""
    conn = _fresh_conn()
    fast_lambda = 0.3
    add_edge(conn, "X", "r", "Y", confidence_score=1.0)
    row = _get_row(conn, "X", "r", "Y")
    # Override lambda on the row directly.
    conn.execute(
        "UPDATE graph_edges SET decay_lambda = ? WHERE rowid = ?",
        (fast_lambda, row["rowid"]),
    )
    conn.commit()
    _set_valid_from(conn, row["rowid"], days_ago=1.0)

    apply_decay_sweep(conn)

    row = _get_row(conn, "X", "r", "Y")
    expected = 1.0 * math.exp(-fast_lambda * 1.0)
    assert abs(row["confidence_score"] - expected) < 1e-6


def test_decay_resets_valid_from_to_now():
    """After the sweep, valid_from is updated to roughly now (clock reset)."""
    conn = _fresh_conn()
    add_edge(conn, "P", "r", "Q", confidence_score=0.9)
    row = _get_row(conn, "P", "r", "Q")
    _set_valid_from(conn, row["rowid"], days_ago=3.0)

    apply_decay_sweep(conn)

    row = _get_row(conn, "P", "r", "Q")
    dt = datetime.fromisoformat(row["valid_from"])
    age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    assert age_seconds < 5, "valid_from must be reset to now after sweep"


# ---------------------------------------------------------------------------
# 4. Threshold → invalid_at
# ---------------------------------------------------------------------------

def test_decay_below_threshold_stamps_invalid_at():
    """When decayed weight < DECAY_THRESHOLD, invalid_at is set."""
    conn = _fresh_conn()
    # With lambda=0.5 and t=10 days: 1.0 * exp(-0.5*10) = exp(-5) ≈ 0.0067 < 0.1
    add_edge(conn, "D", "r", "E", confidence_score=1.0)
    row = _get_row(conn, "D", "r", "E")
    conn.execute(
        "UPDATE graph_edges SET decay_lambda = 0.5 WHERE rowid = ?",
        (row["rowid"],),
    )
    conn.commit()
    _set_valid_from(conn, row["rowid"], days_ago=10.0)

    apply_decay_sweep(conn)

    row = _get_row(conn, "D", "r", "E")
    assert row["invalid_at"] is not None, "expired edge must have invalid_at set"
    assert row["confidence_score"] < DECAY_THRESHOLD


def test_above_threshold_does_not_stamp_invalid_at():
    """An edge that decays but stays above 0.1 must NOT have invalid_at set."""
    conn = _fresh_conn()
    add_edge(conn, "G", "r", "H", confidence_score=1.0)
    row = _get_row(conn, "G", "r", "H")
    # lambda=0.05, t=1: 1.0 * exp(-0.05) ≈ 0.951 — well above 0.1
    _set_valid_from(conn, row["rowid"], days_ago=1.0)

    apply_decay_sweep(conn)

    row = _get_row(conn, "G", "r", "H")
    assert row["invalid_at"] is None
    assert row["confidence_score"] > DECAY_THRESHOLD


# ---------------------------------------------------------------------------
# 5. Return stats
# ---------------------------------------------------------------------------

def test_sweep_returns_correct_stats():
    """apply_decay_sweep returns swept/decayed/expired counts."""
    conn = _fresh_conn()

    # Edge 1: will decay but stay above threshold.
    add_edge(conn, "A", "r", "B", confidence_score=1.0)
    r1 = _get_row(conn, "A", "r", "B")
    _set_valid_from(conn, r1["rowid"], days_ago=1.0)

    # Edge 2: will expire (high lambda, old).
    add_edge(conn, "C", "r", "D", confidence_score=1.0)
    r2 = _get_row(conn, "C", "r", "D")
    conn.execute("UPDATE graph_edges SET decay_lambda=0.5 WHERE rowid=?", (r2["rowid"],))
    conn.commit()
    _set_valid_from(conn, r2["rowid"], days_ago=10.0)

    result = apply_decay_sweep(conn)

    assert result["swept"] == 2
    assert result["decayed"] == 2
    assert result["expired"] == 1


def test_sweep_returns_zero_stats_on_empty_graph():
    """Empty graph → all stats are 0."""
    conn = _fresh_conn()
    result = apply_decay_sweep(conn)
    assert result == {"swept": 0, "decayed": 0, "expired": 0}


# ---------------------------------------------------------------------------
# 6. GraphEngine filters expired edges
# ---------------------------------------------------------------------------

def test_graph_engine_excludes_expired_edges():
    """GraphEngine only loads edges where invalid_at IS NULL."""
    conn = _fresh_conn()
    add_edge(conn, "A", "leads", "B", confidence_score=0.9)
    add_edge(conn, "A", "leads", "C", confidence_score=0.8)

    # Manually expire the A→C edge.
    row_c = _get_row(conn, "A", "leads", "C")
    conn.execute(
        "UPDATE graph_edges SET invalid_at = ? WHERE rowid = ?",
        (_ts(0), row_c["rowid"]),
    )
    conn.commit()

    eng = GraphEngine(conn=conn)
    ctx = eng.get_relevant_context("A", max_hops=1)
    targets = {e["target"] for e in ctx}

    assert "B" in targets, "active edge must appear"
    assert "C" not in targets, "expired edge must be excluded"


def test_graph_engine_loads_zero_edges_when_all_expired():
    """If every edge is expired, the graph is empty and returns []."""
    conn = _fresh_conn()
    add_edge(conn, "X", "r", "Y", confidence_score=0.9)
    row = _get_row(conn, "X", "r", "Y")
    conn.execute(
        "UPDATE graph_edges SET invalid_at = ? WHERE rowid = ?",
        (_ts(0), row["rowid"]),
    )
    conn.commit()

    eng = GraphEngine(conn=conn)
    assert eng.graph.number_of_edges() == 0
    assert eng.get_relevant_context("X") == []


def test_graph_engine_loads_after_decay_sweep():
    """Graph built after a real decay sweep only sees surviving edges."""
    conn = _fresh_conn()

    # Edge that will survive.
    add_edge(conn, "NIFTY 50", "signals", "bullish", confidence_score=1.0)
    r1 = _get_row(conn, "NIFTY 50", "signals", "bullish")
    _set_valid_from(conn, r1["rowid"], days_ago=1.0)  # small decay

    # Edge that will expire.
    add_edge(conn, "NIFTY 50", "signals", "stale", confidence_score=1.0)
    r2 = _get_row(conn, "NIFTY 50", "signals", "stale")
    conn.execute("UPDATE graph_edges SET decay_lambda=1.0 WHERE rowid=?", (r2["rowid"],))
    conn.commit()
    _set_valid_from(conn, r2["rowid"], days_ago=20.0)  # will expire

    apply_decay_sweep(conn)
    eng = GraphEngine(conn=conn)

    targets = {e["target"] for e in eng.get_relevant_context("NIFTY 50", max_hops=1)}
    assert "bullish" in targets
    assert "stale" not in targets


# ---------------------------------------------------------------------------
# 7. Edge cases: fresh edge, NULL confidence, idempotency
# ---------------------------------------------------------------------------

def test_fresh_edge_no_decay_applied():
    """An edge created right now (valid_from ≈ now) gets 0 decay on sweep."""
    conn = _fresh_conn()
    w0 = 0.75
    add_edge(conn, "F", "r", "G", confidence_score=w0)

    apply_decay_sweep(conn)

    row = _get_row(conn, "F", "r", "G")
    # t ≈ 0 seconds → exp(-lambda * ~0) ≈ 1.0, so score is unchanged.
    assert abs(row["confidence_score"] - w0) < 0.001


def test_edge_with_null_confidence_gets_stamped_no_decay():
    """An edge with NULL confidence_score gets valid_from stamped but
    no weight update (nothing to decay)."""
    conn = _fresh_conn()
    add_edge(conn, "N", "r", "M", confidence_score=None)
    row = _get_row(conn, "N", "r", "M")
    # Clear valid_from to simulate a legacy row without timestamp.
    conn.execute("UPDATE graph_edges SET valid_from = NULL WHERE rowid = ?",
                 (row["rowid"],))
    conn.commit()

    apply_decay_sweep(conn)

    row = _get_row(conn, "N", "r", "M")
    assert row["valid_from"] is not None, "valid_from must be backfilled"
    assert row["confidence_score"] is None, "NULL confidence must remain NULL"
    assert row["invalid_at"] is None


def test_legacy_edge_without_valid_from_gets_backfilled():
    """A pre-migration edge (valid_from IS NULL) gets stamped on first sweep
    and is not expired."""
    conn = _fresh_conn()
    add_edge(conn, "L", "r", "K", confidence_score=0.7)
    row = _get_row(conn, "L", "r", "K")
    conn.execute("UPDATE graph_edges SET valid_from = NULL WHERE rowid = ?",
                 (row["rowid"],))
    conn.commit()

    result = apply_decay_sweep(conn)

    row = _get_row(conn, "L", "r", "K")
    assert row["valid_from"] is not None
    assert row["invalid_at"] is None
    assert result["swept"] == 0  # backfill only, no decay applied


def test_sweep_is_idempotent_within_same_second():
    """Running the sweep twice in quick succession does not double-decay
    (t ≈ 0 on second call)."""
    conn = _fresh_conn()
    w0 = 0.9
    add_edge(conn, "I", "r", "J", confidence_score=w0)
    row = _get_row(conn, "I", "r", "J")
    _set_valid_from(conn, row["rowid"], days_ago=1.0)

    apply_decay_sweep(conn)
    after_first = _get_row(conn, "I", "r", "J")["confidence_score"]

    apply_decay_sweep(conn)
    after_second = _get_row(conn, "I", "r", "J")["confidence_score"]

    # Second sweep is a no-op because valid_from was just reset to now.
    assert abs(after_first - after_second) < 1e-6


# ---------------------------------------------------------------------------
# 8. Progressive multi-sweep compounding
# ---------------------------------------------------------------------------

def test_two_sweeps_compound_correctly():
    """Two daily sweeps should equal one combined two-day decay:
    w0 * exp(-λ) * exp(-λ) == w0 * exp(-2λ)."""
    w0 = 1.0
    lambda_ = DEFAULT_LAMBDA

    # Sweep 1
    conn = _fresh_conn()
    add_edge(conn, "A", "r", "B", confidence_score=w0)
    row = _get_row(conn, "A", "r", "B")
    _set_valid_from(conn, row["rowid"], days_ago=1.0)
    apply_decay_sweep(conn)
    after_sweep1 = _get_row(conn, "A", "r", "B")["confidence_score"]

    # Sweep 2 (age another day)
    row = _get_row(conn, "A", "r", "B")
    _set_valid_from(conn, row["rowid"], days_ago=1.0)
    apply_decay_sweep(conn)
    after_sweep2 = _get_row(conn, "A", "r", "B")["confidence_score"]

    expected = w0 * math.exp(-lambda_ * 2)
    assert abs(after_sweep2 - expected) < 1e-6


if __name__ == "__main__":
    test_ensure_schema_adds_temporal_columns()
    test_migrate_schema_is_idempotent()
    test_migrate_schema_on_empty_db_is_safe()
    test_add_edge_stamps_valid_from()
    test_add_edge_stamps_decay_lambda()
    test_add_edge_reinforce_resets_valid_from_and_clears_invalid_at()
    test_decay_formula_one_day()
    test_decay_formula_two_days()
    test_decay_custom_lambda()
    test_decay_resets_valid_from_to_now()
    test_decay_below_threshold_stamps_invalid_at()
    test_above_threshold_does_not_stamp_invalid_at()
    test_sweep_returns_correct_stats()
    test_sweep_returns_zero_stats_on_empty_graph()
    test_graph_engine_excludes_expired_edges()
    test_graph_engine_loads_zero_edges_when_all_expired()
    test_graph_engine_loads_after_decay_sweep()
    test_fresh_edge_no_decay_applied()
    test_edge_with_null_confidence_gets_stamped_no_decay()
    test_legacy_edge_without_valid_from_gets_backfilled()
    test_sweep_is_idempotent_within_same_second()
    test_two_sweeps_compound_correctly()
    print("All decay tests passed.")

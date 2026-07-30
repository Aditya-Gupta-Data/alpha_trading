"""Structural-affinity decay rate (owner ruling 2026-07-30) + the one-shot
resurrection repair in scripts/resurrect_affinity.py.

Context: the first production run of the newly-wired edge-decay Task K
expired 45 of 45 `concentrates_in` edges while every causal edge survived.
Cause was not a bug in the sweep but a rate mismatch — graph_engine's
default lambda (0.05/day, ~14-day half-life) applied to an ALL-TIME
structural statistic that is only re-observed when a fresh deal lands.
These tests pin the slower rate and the repair's honesty rules.

Hermetic: in-memory brain_map, synthetic affinity rows, no network.
"""

import importlib.util
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, decay_engine, graph_engine
from src.knowledge_graph import entity_affinity as ea

_SPEC = importlib.util.spec_from_file_location(
    "resurrect_affinity",
    Path(__file__).resolve().parent.parent / "scripts" / "resurrect_affinity.py")
resurrect = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(resurrect)

TODAY = "2026-07-30"


def _conn():
    conn = brain_map.connect(":memory:")
    ea.ensure_schema(conn)
    graph_engine.ensure_schema(conn)
    return conn


def _affinity(conn, client, grp, deals=20, total_extra=1, last_seen="2026-07-01"):
    """Seed the accumulation table so _client_concentration is real."""
    conn.execute(
        "INSERT INTO entity_affinity (client, grp, buy_qty, sell_qty, "
        "buy_value_rs, sell_value_rs, deal_count, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (client, grp, 100, 0, 1000.0, 0.0, deals, "2020-01-01", last_seen))
    if total_extra:
        conn.execute(
            "INSERT INTO entity_affinity (client, grp, buy_qty, sell_qty, "
            "buy_value_rs, sell_value_rs, deal_count, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (client, ea.UNGROUPED, 1, 0, 1.0, 0.0, total_extra,
             "2020-01-01", last_seen))
    conn.commit()


def _expired_edge(conn, client, grp, decayed_to=1e-98):
    """An edge in the exact post-sweep state: confidence crushed,
    invalid_at stamped, valid_from overwritten with the sweep time."""
    swept_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO graph_edges (source_node, relation, target_node, "
        "confidence_score, context, valid_from, invalid_at, decay_lambda, "
        "source) VALUES (?,?,?,?,?,?,?,?,?)",
        (client, "concentrates_in", grp, decayed_to, "20 deals; 95% concentration",
         swept_at, swept_at, 0.05, "affinity_projected"))
    conn.commit()


# --------------------------------------------------- the rate itself

def test_projected_affinity_edge_carries_the_slow_lambda():
    conn = _conn()
    graph_engine.add_edge(conn, "DODONA HOLDINGS", "concentrates_in", "TATA",
                          confidence_score=0.95, valid_from="2026-07-01",
                          decay_lambda=ea.CONCENTRATION_DECAY_LAMBDA,
                          source="affinity_projected")
    row = conn.execute(
        "SELECT decay_lambda FROM graph_edges").fetchone()
    assert row["decay_lambda"] == pytest.approx(0.002)


def test_causal_edges_keep_the_fast_default():
    """The ruling is scoped to structural affinity — reasoning edges must
    be untouched, or we would have slowed the whole graph by accident."""
    conn = _conn()
    graph_engine.add_edge(conn, "GAP DOWN", "RESULTS_IN", "STOP HIT",
                          confidence_score=1.0)
    row = conn.execute("SELECT decay_lambda FROM graph_edges").fetchone()
    assert row["decay_lambda"] == pytest.approx(0.05)


def test_an_affinity_edge_survives_the_sweep_that_used_to_kill_it():
    """THE REGRESSION. A 60-day-old affinity died instantly at lambda=0.05
    (0.95*e^-3 = 0.047 < 0.1). At 0.002 it lives (0.95*e^-0.12 = 0.84)."""
    conn = _conn()
    anchor = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    graph_engine.add_edge(conn, "CRESTA FUND", "concentrates_in", "JINDAL",
                          confidence_score=0.95, valid_from=anchor,
                          decay_lambda=ea.CONCENTRATION_DECAY_LAMBDA,
                          source="affinity_projected")

    stats = decay_engine.apply_decay_sweep(conn)

    row = conn.execute(
        "SELECT confidence_score, invalid_at FROM graph_edges").fetchone()
    assert stats["expired"] == 0
    assert row["invalid_at"] is None
    assert row["confidence_score"] > decay_engine.DECAY_THRESHOLD


# ------------------------------------------------- the repair's honesty

def test_dry_run_writes_nothing():
    conn = _conn()
    _affinity(conn, "DODONA HOLDINGS", "TATA", deals=20, last_seen="2026-07-01")
    _expired_edge(conn, "DODONA HOLDINGS", "TATA")

    p = resurrect.plan(conn, TODAY)          # plan() is read-only

    assert len(p["restore"]) == 1
    row = conn.execute(
        "SELECT confidence_score, invalid_at FROM graph_edges").fetchone()
    assert row["invalid_at"] is not None                 # still expired
    assert row["confidence_score"] == pytest.approx(1e-98)   # still crushed


def test_apply_restores_confidence_anchor_and_lambda_not_just_the_flag():
    """The whole point: clearing invalid_at alone would leave an ACTIVE
    edge with ~zero confidence that re-expires on the next sweep."""
    conn = _conn()
    _affinity(conn, "DODONA HOLDINGS", "TATA", deals=20, last_seen="2026-07-01")
    _expired_edge(conn, "DODONA HOLDINGS", "TATA")

    resurrect.apply(conn, resurrect.plan(conn, TODAY)["restore"])

    row = conn.execute("SELECT * FROM graph_edges").fetchone()
    assert row["invalid_at"] is None                         # revived
    assert row["confidence_score"] == pytest.approx(0.952, abs=0.01)  # 20/21
    assert row["decay_lambda"] == pytest.approx(0.002)       # slow clock
    assert row["valid_from"].startswith("2026-07-01")        # TRUE anchor

    # ...and it must still be alive after the next sweep.
    decay_engine.apply_decay_sweep(conn)
    after = conn.execute(
        "SELECT confidence_score, invalid_at FROM graph_edges").fetchone()
    assert after["invalid_at"] is None
    assert after["confidence_score"] > decay_engine.DECAY_THRESHOLD


def test_an_affinity_that_is_no_longer_true_is_NOT_resurrected():
    """The edge asserts a fact. If the entity no longer clears the
    concentration bar, reviving it would be revival by fiat."""
    conn = _conn()
    # 2 deals in TATA out of 100 -> way under MIN_CONCENTRATION.
    _affinity(conn, "SPRAY TRADER", "TATA", deals=2, total_extra=98,
              last_seen="2026-07-01")
    _expired_edge(conn, "SPRAY TRADER", "TATA")

    p = resurrect.plan(conn, TODAY)

    assert p["restore"] == []
    assert len(p["skip_not_a_fact"]) == 1
    resurrect.apply(conn, p["restore"])
    assert conn.execute(
        "SELECT invalid_at FROM graph_edges").fetchone()["invalid_at"] is not None


def test_a_genuinely_ancient_affinity_stays_dead():
    """Honest consequence, asserted rather than hidden: >~3y since the last
    deal is still below threshold at 0.002, and must NOT be revived."""
    conn = _conn()
    _affinity(conn, "ASIAN BROKING", "TATA", deals=20, last_seen="2014-01-01")
    _expired_edge(conn, "ASIAN BROKING", "TATA")

    p = resurrect.plan(conn, TODAY)

    assert p["restore"] == []
    assert len(p["skip_too_old"]) == 1
    assert p["skip_too_old"][0]["age_days"] > 4000


def test_plan_counts_every_edge_exactly_once():
    conn = _conn()
    _affinity(conn, "GOOD FUND", "TATA", deals=20, last_seen="2026-07-01")
    _expired_edge(conn, "GOOD FUND", "TATA")
    _affinity(conn, "OLD FUND", "ADANI", deals=20, last_seen="2014-01-01")
    _expired_edge(conn, "OLD FUND", "ADANI")
    _affinity(conn, "SPRAY", "JINDAL", deals=2, total_extra=98,
              last_seen="2026-07-01")
    _expired_edge(conn, "SPRAY", "JINDAL")

    p = resurrect.plan(conn, TODAY)

    assert p["total"] == 3
    assert (len(p["restore"]) + len(p["skip_too_old"])
            + len(p["skip_not_a_fact"])) == 3

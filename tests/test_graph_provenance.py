"""
Provenance firewall tests (holy-grail plan §5.6): WHO wrote a graph edge
decides what it may DO. vol_bridge's net_signal alters position sizing
(decision #38), so only outcome-derived causal edges may feed it — an
entity-affinity projection whose fund/group name happens to carry a
polarity word must NEVER move risk. Fully offline.

Run either of these from the project folder:
    python tests/test_graph_provenance.py
    python -m pytest tests/test_graph_provenance.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, graph_engine, vol_bridge
from src.knowledge_graph import entity_affinity as ea


def test_add_edge_stamps_provenance_and_defaults_outcome_derived():
    conn = brain_map.connect(":memory:")
    graph_engine.add_edge(conn, "vix_spike", "caused", "loss",
                          confidence_score=0.9)
    graph_engine.add_edge(conn, "SOME FUND", "concentrates_in", "ADANI",
                          confidence_score=0.8, source="affinity_projected")
    rows = {r["relation"]: r["source"] for r in conn.execute(
        "SELECT relation, source FROM graph_edges")}
    assert rows["caused"] == "outcome_derived"
    assert rows["concentrates_in"] == "affinity_projected"


def test_affinity_edges_with_polarity_names_never_move_net_signal():
    conn = brain_map.connect(":memory:")
    # An affinity edge deliberately crafted so BOTH node names carry
    # polarity vocabulary — the worst case the firewall must absorb.
    graph_engine.add_edge(conn, "CRASH RECOVERY FUND", "concentrates_in",
                          "BULL CAP GROUP", confidence_score=1.0,
                          source="affinity_projected")
    edges = vol_bridge._load_active_edges(conn)
    assert edges == []                       # firewall: nothing to sum
    # One real outcome-derived edge passes and is the ONLY signal input.
    graph_engine.add_edge(conn, "rate_hike", "preceded", "volatility_spike",
                          confidence_score=0.7)
    edges = vol_bridge._load_active_edges(conn)
    assert len(edges) == 1 and edges[0]["relation"] == "preceded"


def test_pre_provenance_db_falls_back_to_relation_exclusion():
    """A brain_map.db written by pre-firewall code (no `source` column):
    vol_bridge must still exclude concentrates_in edges."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE graph_edges (
            source_node TEXT, relation TEXT, target_node TEXT,
            confidence_score REAL, context TEXT,
            valid_from TEXT, invalid_at TEXT, decay_lambda REAL,
            UNIQUE (source_node, relation, target_node))
    """)
    conn.execute("INSERT INTO graph_edges (source_node, relation, "
                 "target_node, confidence_score) VALUES "
                 "('BEAR FUND', 'concentrates_in', 'TATA', 1.0)")
    conn.execute("INSERT INTO graph_edges (source_node, relation, "
                 "target_node, confidence_score) VALUES "
                 "('crude_spike', 'caused', 'loss', 0.6)")
    conn.commit()
    edges = vol_bridge._load_active_edges(conn)
    assert [e["relation"] for e in edges] == ["caused"]


def test_migration_backfills_provenance_deterministically():
    """ensure_schema on a pre-provenance table labels concentrates_in as
    affinity_projected and everything else outcome_derived — no 'unknown'
    limbo that would zero the vol bridge."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE graph_edges (
            source_node TEXT NOT NULL, relation TEXT NOT NULL,
            target_node TEXT NOT NULL, confidence_score REAL, context TEXT)
    """)
    conn.execute("INSERT INTO graph_edges VALUES "
                 "('F', 'concentrates_in', 'ADANI', 0.9, NULL)")
    conn.execute("INSERT INTO graph_edges VALUES "
                 "('vix_spike', 'caused', 'loss', 1.0, NULL)")
    conn.commit()
    graph_engine.ensure_schema(conn)
    rows = {r["relation"]: r["source"] for r in conn.execute(
        "SELECT relation, source FROM graph_edges")}
    assert rows["concentrates_in"] == "affinity_projected"
    assert rows["caused"] == "outcome_derived"


def test_affinity_run_end_to_end_writes_firewalled_edges():
    conn = brain_map.connect(":memory:")
    groups = {"ticker_to_group": {"ADANIENT.NS": "ADANI"},
              "groups": {"ADANI": ["ADANIENT.NS"]}, "client_aliases": {}}
    hist = [{"ticker": "ADANIENT.NS", "client": "MISTY SEAS FUND",
             "side": "sell", "qty": 1000, "value_rs": 100000.0,
             "deal_type": "bulk", "as_of": f"2026-07-{d:02d}"}
            for d in (6, 7, 8)]
    ea.accumulate_entity_affinity(conn, hist, groups, today=date(2026, 7, 10))
    row = conn.execute("SELECT source FROM graph_edges "
                       "WHERE relation = 'concentrates_in'").fetchone()
    assert row["source"] == "affinity_projected"
    assert vol_bridge._load_active_edges(conn) == []


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

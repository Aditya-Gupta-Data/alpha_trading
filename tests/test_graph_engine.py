"""
Tests for the Phase 6C Knowledge Graph reasoning layer (src/graph_engine.py).

Offline — every test builds a throwaway ':memory:' brain_map DB, writes a
few edges, and asserts the GraphEngine's in-memory traversal. No network, no
real data/brain_map.db is touched (HANDOVER's "never reset live data" rule).

Run:
    python tests/test_graph_engine.py
    pytest tests/test_graph_engine.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.graph_engine import GraphEngine, add_edge, ensure_schema


def _engine_with_edges(edges):
    """Build a GraphEngine over a fresh in-memory DB seeded with `edges`
    (tuples of source, relation, target, confidence). The connection stays
    open so the engine can load from it before we drop it."""
    conn = brain_map.connect(":memory:")
    ensure_schema(conn)
    for src, rel, tgt, conf in edges:
        add_edge(conn, src, rel, tgt, conf)
    return GraphEngine(conn=conn)


# A small graph:
#   NIFTY 50 --(0.9)--> IT_STRENGTH --(0.8)--> TCS         (2 hops)
#   NIFTY 50 --(0.6)--> RATE_CUT                            (1 hop)
#   TCS      --(0.7)--> EARNINGS_BEAT                       (3 hops from NIFTY)
def _sample_engine():
    return _engine_with_edges([
        ("NIFTY 50", "signals", "IT_STRENGTH", 0.9),
        ("IT_STRENGTH", "led_to", "TCS", 0.8),
        ("NIFTY 50", "co_occurred_with", "RATE_CUT", 0.6),
        ("TCS", "produced", "EARNINGS_BEAT", 0.7),
    ])


def test_two_hop_traversal_finds_linked_nodes():
    eng = _sample_engine()
    ctx = eng.get_relevant_context("NIFTY 50", max_hops=2)
    targets = {e["target"] for e in ctx}
    # 1-hop: IT_STRENGTH, RATE_CUT.  2-hop: TCS.
    assert targets == {"IT_STRENGTH", "RATE_CUT", "TCS"}


def test_hop_limit_excludes_deeper_nodes():
    eng = _sample_engine()
    ctx = eng.get_relevant_context("NIFTY 50", max_hops=2)
    targets = {e["target"] for e in ctx}
    # EARNINGS_BEAT sits 3 hops out — must NOT appear at max_hops=2.
    assert "EARNINGS_BEAT" not in targets


def test_one_hop_only():
    eng = _sample_engine()
    ctx = eng.get_relevant_context("NIFTY 50", max_hops=1)
    targets = {e["target"] for e in ctx}
    assert targets == {"IT_STRENGTH", "RATE_CUT"}


def test_hops_depth_is_reported():
    eng = _sample_engine()
    by_target = {e["target"]: e["hops"] for e in
                 eng.get_relevant_context("NIFTY 50", max_hops=2)}
    assert by_target["IT_STRENGTH"] == 1
    assert by_target["RATE_CUT"] == 1
    assert by_target["TCS"] == 2


def test_results_sorted_by_confidence_desc():
    eng = _sample_engine()
    ctx = eng.get_relevant_context("NIFTY 50", max_hops=2)
    scores = [e["confidence_score"] for e in ctx]
    assert scores == sorted(scores, reverse=True)
    assert ctx[0]["target"] == "IT_STRENGTH"  # 0.9, the strongest link


def test_unknown_node_returns_empty():
    eng = _sample_engine()
    assert eng.get_relevant_context("UNLISTED_XYZ") == []


def test_empty_graph_degrades_safely():
    eng = _engine_with_edges([])
    assert eng.graph.number_of_edges() == 0
    assert eng.get_relevant_context("NIFTY 50") == []


def test_none_confidence_sorts_last_not_crash():
    eng = _engine_with_edges([
        ("A", "strong", "B", 0.9),
        ("A", "unknown", "C", None),
        ("A", "weak", "D", 0.3),
    ])
    ctx = eng.get_relevant_context("A", max_hops=1)
    order = [e["target"] for e in ctx]
    assert order == ["B", "D", "C"]  # None weight ranked last


def test_duplicate_edge_keeps_strongest():
    eng = _engine_with_edges([
        ("A", "weak_read", "B", 0.2),
        ("A", "strong_read", "B", 0.95),
    ])
    ctx = eng.get_relevant_context("A", max_hops=1)
    assert len(ctx) == 1
    assert ctx[0]["confidence_score"] == 0.95
    assert ctx[0]["relation"] == "strong_read"


def test_summarize_context_renders_block():
    eng = _sample_engine()
    text = eng.summarize_context("NIFTY 50", max_hops=2)
    assert "IT_STRENGTH" in text and "confidence 0.90" in text
    # Empty for a node with nothing linked.
    assert eng.summarize_context("UNLISTED_XYZ") == ""


if __name__ == "__main__":
    test_two_hop_traversal_finds_linked_nodes()
    test_hop_limit_excludes_deeper_nodes()
    test_one_hop_only()
    test_hops_depth_is_reported()
    test_results_sorted_by_confidence_desc()
    test_unknown_node_returns_empty()
    test_empty_graph_degrades_safely()
    test_none_confidence_sorts_last_not_crash()
    test_duplicate_edge_keeps_strongest()
    test_summarize_context_renders_block()
    print("All graph engine tests passed.")

"""
Tests for Phase 1 GraphRAG (ARCHITECTURE_ROADMAP.md §2):
  - src/knowledge_graph/graphrag.py
  - src/knowledge_graph/spreading_activation.py
  - src/knowledge_graph/graphrag_extract.py

Hermetic: uses in-memory SQLite (`:memory:`) and `tmp_path` for any ledger files.
No network, no live LLM calls, no writes to real `data/` or `logs/`.
"""

from __future__ import annotations

import json
import math
import networkx as nx
import pytest

from src import brain_map
from src import graph_engine
from src import vol_bridge
from src.knowledge_graph import graphrag
from src.knowledge_graph import graphrag_extract
from src.knowledge_graph import spreading_activation


# ===========================================================================
# 1. Schema, Node Seeding, Reinforcement & Provenance Firewall (graphrag.py)
# ===========================================================================

def test_ensure_schema_idempotent_and_preserves_legacy_edges():
    conn = brain_map.connect(":memory:")
    graph_engine.ensure_schema(conn)
    graph_engine.add_edge(
        conn, "NIFTY 50", "RESULTS_IN", "loss", confidence_score=0.85, context="VIX > 20"
    )

    # Upgrade in place twice (idempotence check)
    graphrag.ensure_schema(conn)
    graphrag.ensure_schema(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(graph_edges)")}
    for expected in ("edge_class", "polarity", "weight", "evidence_count", "extractor", "source"):
        assert expected in cols

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"graph_nodes", "graph_edge_evidence", "graph_activation_log"} <= tables

    # Legacy edge is untouched and still readable by GraphEngine
    row = conn.execute(
        "SELECT source_node, relation, target_node, confidence_score, context, source "
        "FROM graph_edges WHERE source_node = 'NIFTY 50'"
    ).fetchone()
    assert row["confidence_score"] == pytest.approx(0.85)
    assert row["context"] == "VIX > 20"
    assert row["source"] == "outcome_derived"


def test_seed_nodes_and_backfill_from_edges():
    conn = brain_map.connect(":memory:")
    graph_engine.ensure_schema(conn)
    graph_engine.add_edge(conn, "CUSTOM_THEME_A", "INDICATES", "CUSTOM_THEME_B", 0.6)

    stats1 = graphrag.seed_nodes(conn, now="2026-10-28T10:00:00+00:00")
    assert stats1["total_nodes"] > 50
    assert stats1["sectors_seeded"] >= 7
    assert stats1["groups_seeded"] >= 10
    assert stats1["backfilled_from_edges"] == 2

    # Second seed run is idempotent (adds 0 new nodes)
    stats2 = graphrag.seed_nodes(conn, now="2026-10-28T10:05:00+00:00")
    assert stats2["added_nodes"] == 0

    # Option universe stocks and indices have in_universe = 1
    rel = graphrag.resolve_node(conn, "Reliance Industries", declared_type="company")
    assert rel is not None
    assert rel["node_id"] == "RELIANCE"
    assert rel["in_universe"] == 1

    tata = graphrag.resolve_node(conn, "Tata Motors", declared_type="company")
    assert tata is not None
    assert tata["node_id"] == "TATAMOTORS"
    assert tata["in_universe"] == 0  # not one of the 5 VM stock option underlyings

    # Unknown company is rejected (None), whereas unknown non-company is accepted with in_universe=0
    assert graphrag.resolve_node(conn, "Phantom Corp XYZ", declared_type="company") is None
    lith = graphrag.resolve_node(conn, "Lithium", declared_type="commodity")
    assert lith is not None
    assert lith["node_id"] == "commodity:LITHIUM"
    assert lith["in_universe"] == 0


def test_provenance_firewall_blocks_llm_extracted_from_vol_bridge():
    """CRITICAL INVARIANT: `llm_extracted` edges must NEVER feed `vol_bridge` sizing."""
    conn = brain_map.connect(":memory:")
    graphrag.ensure_schema(conn)

    # Insert heavy negative-keyword GraphRAG edges that would trigger "Expansion"
    # risk cuts if vol_bridge read them.
    src_node = {"node_id": "RELIANCE", "node_type": "company", "in_universe": 1}
    tgt_node = {"node_id": "macro:BEARISH_CRASH_LOSS_SHOCK", "node_type": "macro", "in_universe": 0}
    graphrag.upsert_graphrag_edge(
        conn,
        source_node=src_node,
        relation="drives",
        target_node=tgt_node,
        edge_class="causal",
        polarity=-1,
        strength=1.0,
        content_hash="hash_firewall_1",
        quote="bearish crash loss shock",
    )

    active_for_sizing = vol_bridge._load_active_edges(conn)
    assert active_for_sizing == []
    overrides = vol_bridge.compute_regime_overrides(conn=conn, base_risk_pct=2.0)
    assert overrides == {"regime": "Neutral"}
    assert "risk_pct" not in overrides


def test_reinforcement_math_and_evidence_dedupe():
    conn = brain_map.connect(":memory:")
    graphrag.ensure_schema(conn)
    src = {"node_id": "TATAMOTORS", "node_type": "company", "in_universe": 0}
    tgt = {"node_id": "technology:EV", "node_type": "technology", "in_universe": 0}

    # 1st observation: strength = 0.8 -> confidence = 0.5 * 0.8 = 0.4
    r1 = graphrag.upsert_graphrag_edge(
        conn, src, "makes", tgt, "supply", 1, 0.8, "hash_doc_1", "Tata Motors makes EV"
    )
    assert r1["written"] is True
    assert r1["is_new_edge"] is True
    assert r1["confidence_score"] == pytest.approx(0.4)
    assert r1["evidence_count"] == 1

    # Duplicate replay of the same content_hash: no-op
    r_dup = graphrag.upsert_graphrag_edge(
        conn, src, "makes", tgt, "supply", 1, 0.8, "hash_doc_1", "Tata Motors makes EV"
    )
    assert r_dup["written"] is False
    assert r_dup["duplicate_evidence"] is True
    assert r_dup["confidence_score"] == pytest.approx(0.4)
    assert r_dup["evidence_count"] == 1

    # 2nd distinct observation: strength = 0.8 -> c_new = 1 - (1 - 0.4)*(1 - 0.4) = 0.64
    r2 = graphrag.upsert_graphrag_edge(
        conn, src, "makes", tgt, "supply", 1, 0.8, "hash_doc_2", "Tata Motors makes EVs"
    )
    assert r2["written"] is True
    assert r2["is_new_edge"] is False
    assert r2["confidence_score"] == pytest.approx(0.64)
    assert r2["evidence_count"] == 2


# ===========================================================================
# 2. 3D Spreading Activation Math (spreading_activation.py)
# ===========================================================================

def test_three_hop_chain_propagation_math():
    """Verify exact hop decay (0.6, 0.36, 0.216) and transmission across a 3-hop chain."""
    g = nx.DiGraph()
    g.add_edge("SEED", "HOP1", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("HOP1", "HOP2", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("HOP2", "HOP3", weight=1.0, confidence_score=1.0, polarity=-1)
    g.add_edge("HOP3", "HOP4", weight=1.0, confidence_score=1.0, polarity=1)

    res = spreading_activation.propagate(g, seeds={"SEED": 1.0}, log_activation=False)
    by_node = res["by_node"]

    # HOP4 is 4 hops away (K=3), so excluded; outdeg=1 has eps=1.0 -> exact 0.6, 0.36, -0.216
    assert "HOP4" not in by_node
    assert by_node["HOP1"]["energy"] == pytest.approx(0.6, rel=1e-5)
    assert by_node["HOP1"]["dominant_hop"] == 1
    assert by_node["HOP2"]["energy"] == pytest.approx(0.36, rel=1e-5)
    assert by_node["HOP2"]["dominant_hop"] == 2
    assert by_node["HOP3"]["energy"] == pytest.approx(-0.216, rel=1e-5)
    assert by_node["HOP3"]["dominant_hop"] == 3
    assert by_node["HOP3"]["via"] == ["SEED", "HOP1", "HOP2", "HOP3"]


def test_cycle_does_not_reinfect_nodes_on_path():
    """A -> B -> A and A -> B -> C -> A must not re-inject energy back into A or B."""
    g = nx.DiGraph()
    g.add_edge("A", "B", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("B", "A", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("B", "C", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("C", "B", weight=1.0, confidence_score=1.0, polarity=1)

    res = spreading_activation.propagate(g, seeds={"A": 1.0}, log_activation=False)
    by_node = res["by_node"]

    # A is the seed and never re-injected; B is only reached at hop 1 (not hop 3 via B->C->B)
    assert "A" not in by_node
    assert list(by_node["B"]["hop_energies"].keys()) == [1]
    assert list(by_node["C"]["hop_energies"].keys()) == [2]


def test_hub_fanout_normalization_dampens_hub():
    """A hub with 10 outgoing edges transmits less to each neighbor than a 1-edge specialist."""
    g = nx.DiGraph()
    g.add_edge("SPECIALIST", "TARGET_S", weight=1.0, confidence_score=1.0, polarity=1)
    for i in range(10):
        g.add_edge("HUB", f"TARGET_H_{i}", weight=1.0, confidence_score=1.0, polarity=1)

    res = spreading_activation.propagate(
        g, seeds={"SPECIALIST": 1.0, "HUB": 1.0}, log_activation=False
    )
    by_node = res["by_node"]
    assert by_node["TARGET_S"]["energy"] > by_node["TARGET_H_0"]["energy"] * 1.8


def test_sign_cancellation_and_pruning():
    """Conflicting +1 and -1 paths cancel; weak nodes below theta=0.02 are pruned."""
    g = nx.DiGraph()
    # Two symmetric paths of opposite sign from SEED to CANCEL_TARGET
    g.add_edge("SEED", "POS_MID", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("SEED", "NEG_MID", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("POS_MID", "CANCEL_TARGET", weight=1.0, confidence_score=1.0, polarity=1)
    g.add_edge("NEG_MID", "CANCEL_TARGET", weight=1.0, confidence_score=1.0, polarity=-1)
    # One tiny edge that falls below theta=0.02
    g.add_edge("SEED", "TINY", weight=0.02, confidence_score=0.5, polarity=1)

    res = spreading_activation.propagate(g, seeds={"SEED": 1.0}, log_activation=False)
    by_node = res["by_node"]
    assert "CANCEL_TARGET" not in by_node
    assert "TINY" not in by_node


def test_determinism_and_3d_momentum_across_days():
    """Repeated same-sign activation across days builds 3D momentum M(v,t) in graph_activation_log."""
    conn = brain_map.connect(":memory:")
    graphrag.ensure_schema(conn)
    src = {"node_id": "RELIANCE", "node_type": "company", "in_universe": 1}
    tgt = {"node_id": "commodity:CRUDE_OIL", "node_type": "commodity", "in_universe": 0}
    graphrag.upsert_graphrag_edge(
        conn, src, "needs", tgt, "demand", 1, 0.9, "h1", "Reliance needs crude oil"
    )
    eng = graph_engine.GraphEngine(conn=conn)

    # Day 1 activation
    r_day1 = spreading_activation.propagate(
        eng,
        seeds={"RELIANCE": 1.0},
        conn=conn,
        trigger_ref="ev-day1",
        run_at="2026-10-27T09:00:00+00:00",
    )
    e1 = r_day1["by_node"]["commodity:CRUDE_OIL"]["energy"]
    m1 = r_day1["by_node"]["commodity:CRUDE_OIL"]["momentum"]
    assert m1 == pytest.approx(e1)

    # Day 2 activation (1 day later -> M_day2 = 0.85 * M_day1 + E_day2)
    r_day2 = spreading_activation.propagate(
        eng,
        seeds={"RELIANCE": 1.0},
        conn=conn,
        trigger_ref="ev-day2",
        run_at="2026-10-28T09:00:00+00:00",
    )
    e2 = r_day2["by_node"]["commodity:CRUDE_OIL"]["energy"]
    m2 = r_day2["by_node"]["commodity:CRUDE_OIL"]["momentum"]
    assert m2 == pytest.approx(0.85 * m1 + e2, rel=1e-5)
    assert m2 > e2

    log_count = conn.execute("SELECT COUNT(*) AS n FROM graph_activation_log").fetchone()["n"]
    assert log_count == 2


# ===========================================================================
# 3. LLM Extraction Pipeline, Validator & Fail-Open (graphrag_extract.py)
# ===========================================================================

def test_worked_example_extraction_and_walkable_chain(tmp_path):
    """Worked example from ARCHITECTURE_ROADMAP.md §2.3 step 4:
    'Tata Motors ramps EV output; cell makers warn of lithium shortage.'"""
    conn = brain_map.connect(":memory:")
    graphrag.seed_nodes(conn)

    text = "Tata Motors ramps EV output; cell makers warn of lithium shortage."

    def fake_llm(sys_prompt, user_text):
        return {
            "edges": [
                {
                    "subject": "Tata Motors",
                    "subject_type": "company",
                    "predicate": "makes",
                    "object": "EV",
                    "object_type": "technology",
                    "polarity": 1,
                    "strength": 0.8,
                    "quote": "Tata Motors ramps EV output",
                },
                {
                    "subject": "EV",
                    "subject_type": "technology",
                    "predicate": "needs",
                    "object": "Lithium",
                    "object_type": "commodity",
                    "polarity": 1,
                    "strength": 0.8,
                    "quote": "cell makers warn of lithium shortage",
                },
            ]
        }

    out = graphrag_extract.extract_from_item(
        {"title": text, "text": text, "source": "inbox:test.txt"},
        conn=conn,
        extractor=fake_llm,
        call_ledger_path=tmp_path / "calls.jsonl",
        review_ledger_path=tmp_path / "reviews.jsonl",
        env={"ALPHA_NODE_ROLE": "analytics"},
    )
    assert out["status"] == "ok"
    assert out["written"] == 2
    assert out["rejected"] == 0

    # Walk TATAMOTORS -> technology:EV -> commodity:LITHIUM via GraphEngine + spreading_activation
    eng = graph_engine.GraphEngine(conn=conn)
    act = spreading_activation.propagate(eng, seeds={"TATAMOTORS": 1.0}, conn=conn)
    assert "technology:EV" in act["by_node"]
    assert "commodity:LITHIUM" in act["by_node"]
    assert act["by_node"]["commodity:LITHIUM"]["via"] == [
        "TATAMOTORS",
        "technology:EV",
        "commodity:LITHIUM",
    ]


def test_validator_rejects_hallucinations_and_invalid_edges(tmp_path):
    conn = brain_map.connect(":memory:")
    graphrag.seed_nodes(conn)
    text = "Reliance expands solar module manufacturing capacity in Jamnagar."

    def fake_llm(sys_prompt, user_text):
        return {
            "edges": [
                # 1. Non-verbatim quote (hallucinated wording) -> rejected
                {
                    "subject": "Reliance",
                    "subject_type": "company",
                    "predicate": "makes",
                    "object": "Solar Modules",
                    "object_type": "technology",
                    "polarity": 1,
                    "strength": 0.9,
                    "quote": "Reliance builds solar panels in Gujarat",
                },
                # 2. Invalid predicate outside closed vocabulary -> rejected
                {
                    "subject": "Reliance",
                    "subject_type": "company",
                    "predicate": "dreams_about",
                    "object": "Solar Modules",
                    "object_type": "technology",
                    "polarity": 1,
                    "strength": 0.9,
                    "quote": "Reliance expands solar module",
                },
                # 3. Unresolved company endpoint -> rejected
                {
                    "subject": "Imaginary Solar Ltd",
                    "subject_type": "company",
                    "predicate": "competes_with",
                    "object": "Reliance",
                    "object_type": "company",
                    "polarity": -1,
                    "strength": 0.7,
                    "quote": "Reliance expands solar module",
                },
                # 4. Self-loop -> rejected
                {
                    "subject": "Reliance",
                    "subject_type": "company",
                    "predicate": "owns",
                    "object": "RIL",
                    "object_type": "company",
                    "polarity": 1,
                    "strength": 0.8,
                    "quote": "Reliance expands solar module",
                },
                # 5. Valid edge -> accepted and triggers review alert (Reliance in_universe=1, w=0.85 >= 0.7)
                {
                    "subject": "Reliance",
                    "subject_type": "company",
                    "predicate": "makes",
                    "object": "Solar Module",
                    "object_type": "technology",
                    "polarity": 1,
                    "strength": 0.85,
                    "quote": "Reliance expands solar module manufacturing",
                },
            ]
        }

    alerts = []
    out = graphrag_extract.extract_from_item(
        text,
        conn=conn,
        extractor=fake_llm,
        notify_fn=lambda msg: alerts.append(msg),
        review_ledger_path=tmp_path / "reviews.jsonl",
        call_ledger_path=tmp_path / "calls.jsonl",
        env={"ALPHA_NODE_ROLE": "analytics"},
    )
    assert out["written"] == 1
    assert out["rejected"] == 4
    assert out["rejected_reasons"] == {
        "quote_not_verbatim": 1,
        "invalid_predicate": 1,
        "unresolved_endpoint": 1,
        "self_loop": 1,
    }
    assert out["review_alerts"] == 1
    assert len(alerts) == 1
    assert "RELIANCE" in alerts[0]


def test_extractor_fail_open_and_node_role_refusal(tmp_path):
    conn = brain_map.connect(":memory:")
    graphrag.seed_nodes(conn)

    # 1. Refused on VM / non-analytics host
    refused = graphrag_extract.extract_from_item(
        "Reliance wins contract",
        conn=conn,
        env={"ALPHA_NODE_ROLE": "execution"},
        hostname="alpha-vm",
    )
    assert refused["status"].startswith("skipped (")
    assert refused["written"] == 0

    # 2. LLM raises runtime error -> swallowed, fail-open, subsequent stage runs
    later_stage_ran = False

    def exploding_llm(sys_prompt, user_text):
        raise RuntimeError("router timeout")

    res = graphrag_extract.extract_from_item(
        "Reliance wins contract for green hydrogen.",
        conn=conn,
        extractor=exploding_llm,
        call_ledger_path=tmp_path / "calls.jsonl",
        env={"ALPHA_NODE_ROLE": "analytics"},
    )
    later_stage_ran = True
    assert res["status"].startswith("error (RuntimeError")
    assert later_stage_ran is True

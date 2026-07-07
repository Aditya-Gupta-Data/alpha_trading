"""
Tests for the Phase 6D Causal Triple Writer:
  * local_parser.extract_causal_triples / _coerce_triples (predicate
    whitelist, node normalization), and
  * sleep_phase.write_causal_links — reviewed outcomes -> `graph_edges`
    rows at confidence 1.0, idempotent, and NEVER from raw news
    sentiment (decision #34).

Offline — every test uses a throwaway ':memory:' brain_map DB and a fake
extractor (no Ollama, no network). The real data/brain_map.db is never
touched (HANDOVER's "never reset live data" rule).

Run:
    python tests/test_causal_writer.py
    pytest tests/test_causal_writer.py -v
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import local_parser
from src import sleep_phase
from src.graph_engine import GraphEngine

TODAY = date(2026, 7, 7)


class FakeExtractor:
    """Stands in for LocalExtractor: records whether the causal LLM was
    called and returns a canned triple list."""

    def __init__(self, triples):
        self._triples = triples
        self.causal_calls = 0

    def extract_causal_triples(self, summarized_text):
        self.causal_calls += 1
        self.last_text = summarized_text
        return self._triples


def _seed_outcome(conn, journal_ref="t1", ticker="NIFTY 50",
                  archetype="iron_condor", r_multiple=-1.2, result="loss",
                  post_mortem=None):
    return brain_map.record_outcome(
        conn, journal_ref=journal_ref, date=TODAY.isoformat(), ticker=ticker,
        archetype=archetype, r_multiple=r_multiple, result=result,
        post_mortem=post_mortem or {"variance_analysis": "VIX spiked past 20"})


# --------------------------------------------- local_parser coercion

def test_coerce_triples_filters_bad_predicate_and_normalizes():
    raw = {"triples": [
        {"subject": "Iron Condor", "predicate": "RESULTS_IN",
         "object": "Loss", "condition": "VIX > 20"},
        {"subject": "X", "predicate": "MAYBE_CAUSES", "object": "Y"},   # bad predicate
        {"subject": "", "predicate": "PRECEDES", "object": "Z"},         # empty subject
    ]}
    out = local_parser._coerce_triples(raw)
    assert len(out) == 1
    t = out[0]
    assert t["subject"] == "iron_condor" and t["object"] == "loss"
    assert t["predicate"] == "RESULTS_IN" and t["condition"] == "VIX > 20"


def test_coerce_triples_handles_junk():
    assert local_parser._coerce_triples(None) == []
    assert local_parser._coerce_triples({"triples": "nope"}) == []
    assert local_parser._coerce_triples({}) == []


def test_extract_causal_triples_uses_chat_json(monkeypatch=None):
    ex = local_parser.LocalExtractor()
    ex.chat_json = lambda system, user: {"triples": [
        {"subject": "Bull Call Spread", "predicate": "indicates",
         "object": "Trend Follow", "condition": None}]}
    out = ex.extract_causal_triples("some outcome summary")
    assert out == [{"subject": "bull_call_spread", "predicate": "INDICATES",
                    "object": "trend_follow", "condition": None}]


def test_extract_causal_triples_empty_text_no_call():
    ex = local_parser.LocalExtractor()
    ex.chat_json = lambda system, user: (_ for _ in ()).throw(
        AssertionError("chat_json should not be called on empty text"))
    assert ex.extract_causal_triples("   ") == []


# --------------------------------------------- sleep_phase writer

def test_write_causal_links_inserts_edge_at_confidence_1():
    conn = brain_map.connect(":memory:")
    _seed_outcome(conn)
    ex = FakeExtractor([{"subject": "iron_condor", "predicate": "RESULTS_IN",
                         "object": "loss", "condition": "VIX > 20"}])
    stats = sleep_phase.write_causal_links(conn, extractor=ex, today=TODAY)
    assert stats == {"outcomes_considered": 1, "triples_written": 1,
                     "triples_skipped": 0}
    row = conn.execute("SELECT source_node, relation, target_node, "
                       "confidence_score, context FROM graph_edges").fetchone()
    assert row["source_node"] == "iron_condor"
    assert row["relation"] == "RESULTS_IN"
    assert row["target_node"] == "loss"
    assert row["confidence_score"] == 1.0
    assert row["context"] == "VIX > 20"


def test_write_causal_links_is_idempotent():
    conn = brain_map.connect(":memory:")
    _seed_outcome(conn)
    ex = FakeExtractor([{"subject": "iron_condor", "predicate": "RESULTS_IN",
                         "object": "loss", "condition": "VIX > 20"}])
    sleep_phase.write_causal_links(conn, extractor=ex, today=TODAY)
    sleep_phase.write_causal_links(conn, extractor=ex, today=TODAY)  # run twice
    n = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()["n"]
    assert n == 1  # same triple reinforced, not duplicated


def test_decision_34_no_edges_from_news_only():
    """With reviewed OUTCOMES absent, a graph is never built from raw news
    events — the causal LLM is not even called."""
    conn = brain_map.connect(":memory:")
    # A raw news event is present, but no reviewed outcome exists.
    brain_map._get_or_create_event(conn, TODAY.isoformat(), "TCS", "news",
                                   "block_deal", source="news_sentiment")
    ex = FakeExtractor([{"subject": "block_deal", "predicate": "INDICATES",
                         "object": "rally", "condition": None}])
    stats = sleep_phase.write_causal_links(conn, extractor=ex, today=TODAY)
    assert stats["outcomes_considered"] == 0
    assert stats["triples_written"] == 0
    assert ex.causal_calls == 0  # never fed news to the causal extractor
    n = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()["n"]
    assert n == 0


def test_written_edge_is_readable_by_graph_engine():
    conn = brain_map.connect(":memory:")
    _seed_outcome(conn)
    ex = FakeExtractor([{"subject": "iron_condor", "predicate": "RESULTS_IN",
                         "object": "loss", "condition": "VIX > 20"}])
    sleep_phase.write_causal_links(conn, extractor=ex, today=TODAY)
    # The Phase 6C reader (same connection) sees the freshly written edge.
    eng = GraphEngine(conn=conn)
    ctx = eng.get_relevant_context("iron_condor")
    assert len(ctx) == 1
    assert ctx[0]["target"] == "loss" and ctx[0]["context"] == "VIX > 20"
    text = eng.summarize_context("iron_condor")
    assert "loss" in text and "when VIX > 20" in text


def test_outcome_summary_includes_post_mortem():
    conn = brain_map.connect(":memory:")
    _seed_outcome(conn, post_mortem={"future_guardrails": "avoid condors in high VIX"})
    rows = conn.execute("SELECT archetype, ticker, r_multiple, result, "
                        "post_mortem FROM outcomes").fetchall()
    text = sleep_phase._outcome_summary_text(rows)
    assert "iron_condor" in text and "NIFTY 50" in text
    assert "avoid condors in high VIX" in text


if __name__ == "__main__":
    test_coerce_triples_filters_bad_predicate_and_normalizes()
    test_coerce_triples_handles_junk()
    test_extract_causal_triples_uses_chat_json()
    test_extract_causal_triples_empty_text_no_call()
    test_write_causal_links_inserts_edge_at_confidence_1()
    test_write_causal_links_is_idempotent()
    test_decision_34_no_edges_from_news_only()
    test_written_edge_is_readable_by_graph_engine()
    test_outcome_summary_includes_post_mortem()
    print("All causal writer tests passed.")

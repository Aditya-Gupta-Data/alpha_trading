"""Tests for src/knowledge_graph/graphrag_news_shadow.py (decision #174). Hermetic: tmp_path files, fake LLM."""
from __future__ import annotations

from src import brain_map
from src.knowledge_graph import graphrag_news_shadow as shadow

ENV = {"ALPHA_NODE_ROLE": "analytics"}
CFG = {"graphrag_shadow_enabled": True, "graphrag_shadow_daily_cap": 300}
ITEM = {"title": "Reliance expands solar module manufacturing", "text": "", "url": "u1", "source": "rss:t"}
EDGE = {"subject": "Reliance", "subject_type": "company", "predicate": "makes", "object": "Solar Module",
        "object_type": "technology", "polarity": 1, "strength": 0.85,
        "quote": "Reliance expands solar module manufacturing"}


def _paths(tmp_path):
    return dict(db_path=tmp_path / "shadow.db", seen_path=tmp_path / "seen.txt",
                call_ledger_path=tmp_path / "calls.jsonl", rejected_path=tmp_path / "rej.log",
                review_path=tmp_path / "review.jsonl")


def test_writes_edges_to_the_shadow_db_and_marks_seen(tmp_path):
    calls = []
    llm = lambda system, text: calls.append(text) or {"edges": [EDGE]}
    out = shadow.run(items=[ITEM], extractor=llm, cfg=CFG, env=ENV, **_paths(tmp_path))
    assert out["status"] == "ok" and out["extracted"] == 1 and out["written"] == 1
    conn = brain_map.connect(tmp_path / "shadow.db")
    assert conn.execute("SELECT COUNT(*) FROM graph_edge_evidence").fetchone()[0] == 1
    again = shadow.run(items=[ITEM], extractor=llm, cfg=CFG, env=ENV, **_paths(tmp_path))
    assert again["already_seen"] == 1 and len(calls) == 1


def test_no_discord_card_even_for_a_review_weight_edge(tmp_path, monkeypatch):
    fired = []
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda *a, **k: fired.append(a))
    shadow.run(items=[ITEM], extractor=lambda s, t: {"edges": [EDGE]}, cfg=CFG, env=ENV, **_paths(tmp_path))
    assert fired == []


def test_disabled_by_default_and_refused_off_the_analytics_node(tmp_path, monkeypatch):
    llm = lambda s, t: {"edges": [EDGE]}
    assert shadow.run(items=[ITEM], extractor=llm, cfg={}, env=ENV, **_paths(tmp_path))["status"] == "disabled"
    monkeypatch.setattr(shadow.node_role, "llm_jobs_refusal", lambda **k: "not the analytics node")
    out = shadow.run(items=[ITEM], extractor=llm, cfg=CFG, env={}, hostname="vm", **_paths(tmp_path))
    assert out["status"].startswith("skipped (") and not (tmp_path / "shadow.db").exists()


def test_llm_down_is_retried_and_the_cap_stops_the_run(tmp_path):
    items = [dict(ITEM, url=f"u{i}", title=f"{ITEM['title']} {i}") for i in range(3)]
    out = shadow.run(items=items, extractor=lambda s, t: None, cfg=CFG, env=ENV, **_paths(tmp_path))
    assert out["retry_later"] == 3 and not (tmp_path / "seen.txt").exists()
    capped = shadow.run(items=items, extractor=lambda s, t: {"edges": []},
                        cfg=dict(CFG, graphrag_shadow_daily_cap=4), env=ENV, **_paths(tmp_path))
    assert capped["status"].startswith("skipped (daily extraction cap") and capped["extracted"] == 1


def test_live_graph_is_never_touched(tmp_path, monkeypatch):
    opened = []
    real = brain_map.connect
    monkeypatch.setattr(brain_map, "connect", lambda p=None: opened.append(p) or real(p))
    shadow.run(items=[ITEM], extractor=lambda s, t: {"edges": [EDGE]}, cfg=CFG, env=ENV, **_paths(tmp_path))
    assert opened == [tmp_path / "shadow.db"]


def test_report(tmp_path):
    assert shadow.report(tmp_path / "none.db")["status"] == "no shadow graph yet"
    shadow.run(items=[ITEM], extractor=lambda s, t: {"edges": [EDGE]}, cfg=CFG, env=ENV, **_paths(tmp_path))
    r = shadow.report(tmp_path / "shadow.db")
    assert r["llm_edges"] == 1 and r["edges_touching_universe"] == 1 and sum(r["evidence_per_day"].values()) == 1

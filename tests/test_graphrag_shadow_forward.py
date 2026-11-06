"""Tests for src/knowledge_graph/graphrag_shadow_forward.py (decision #185). Hermetic: tmp_path DB, synthetic bars."""
from __future__ import annotations

from datetime import date, timedelta

from src import brain_map
from src.knowledge_graph import graphrag
from src.knowledge_graph import graphrag_shadow_forward as fwd


def _db(tmp_path, edges):
    p = tmp_path / "shadow.db"
    conn = brain_map.connect(p)
    graphrag.seed_nodes(conn)
    for i, (src, rel, tgt, pol, ts) in enumerate(edges):
        s = graphrag.resolve_node(conn, src, "company") or graphrag.resolve_node(conn, src, "commodity")
        t = graphrag.resolve_node(conn, tgt, "company") or graphrag.resolve_node(conn, tgt, "theme")
        graphrag.upsert_graphrag_edge(conn, source_node=s, relation=rel, target_node=t,
                                      edge_class=graphrag.PREDICATE_TO_CLASS[rel], polarity=pol, strength=0.8,
                                      content_hash=f"h{i}", quote="q", source_ref="r", extractor="llm:test",
                                      extracted_at=ts)
    conn.commit()
    conn.close()
    return p


def _bars(n=30, start=date(2026, 11, 2), step=1.0):
    out, px = [], 100.0
    for i in range(n):
        out.append({"session": (start + timedelta(days=i)).isoformat(), "open": px, "close": px + step})
        px += step
    return out


def test_only_company_targets_become_calls_on_the_ist_day(tmp_path):
    p = _db(tmp_path, [
        ("Crude Oil", "hurts", "Reliance Industries", -1, "2026-11-02T20:00:00+00:00"),
        ("Reliance Industries", "makes", "Green Hydrogen", 1, "2026-11-02T05:00:00+00:00"),
    ])
    import sqlite3
    acts = fwd.load_shadow_activations(sqlite3.connect(p))
    assert acts == [{"date": "2026-11-03", "ticker": "RELIANCE", "relation": "hurts", "sign": -1}]


def test_run_grades_relations_and_pooled_and_writes_the_ledger(tmp_path):
    p = _db(tmp_path, [("Crude Oil", "hurts", "Reliance Industries", -1, "2026-11-02T04:00:00+00:00")])
    ledger = tmp_path / "fwd.jsonl"
    rep = fwd.run(db_path=p, bars_loader=lambda syms: {s: _bars() for s in syms}, ledger_path=ledger,
                  n_shuffles=10)
    assert rep["status"] == "ok" and rep["calls"] == 1
    v = rep["verdicts"]["hurts"]
    assert v["n"] == 1 and v["hit_rate"] == 0.0 and v["status"] == "ACCUMULATING"
    assert rep["verdicts"][fwd.ALL_EDGES]["n"] == 1
    assert ledger.read_text().count("\n") == 1
    assert "hurts" in "\n".join(fwd.summary_lines(rep))


def test_no_shadow_db_and_read_only(tmp_path):
    assert fwd.run(db_path=tmp_path / "none.db", ledger_path=None)["status"] == "no shadow graph yet"
    p = _db(tmp_path, [("Crude Oil", "hurts", "Reliance Industries", -1, "2026-11-02T04:00:00+00:00")])
    before = p.read_bytes()
    fwd.run(db_path=p, bars_loader=lambda syms: {}, ledger_path=None, n_shuffles=5)
    assert p.read_bytes() == before


def test_main_runs_every_horizon(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(fwd, "run", lambda horizon_sessions: seen.append(horizon_sessions) or {"status": "x"})
    assert fwd.main([]) == 0 and seen == [1, 3, 5]
    seen.clear()
    fwd.main(["--horizon", "2"])
    assert seen == [2]

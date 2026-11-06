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
        s = (graphrag.resolve_node(conn, src.split(":", 1)[1], src.split(":", 1)[0]) if src.startswith("theme:")
             else graphrag.resolve_node(conn, src, "company") or graphrag.resolve_node(conn, src, "commodity"))
        t = (graphrag.resolve_node(conn, tgt, "company") or graphrag.resolve_node(conn, tgt, "sector")
             if tgt.startswith("sector:") else
             graphrag.resolve_node(conn, tgt, "company") or graphrag.resolve_node(conn, tgt, "theme"))
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
    monkeypatch.setattr(fwd, "load_calls", lambda: ([], []))
    monkeypatch.setattr(fwd, "run", lambda horizon_sessions, calls: seen.append(horizon_sessions) or {"status": "x"})
    assert fwd.main([]) == 0 and seen == [1, 3, 5]
    seen.clear()
    fwd.main(["--horizon", "2"])
    assert seen == [2]


def test_propagated_calls_follow_structural_links_without_look_ahead(tmp_path):
    import sqlite3
    p = _db(tmp_path, [
        ("Crude Oil", "hurts", "sector:AUTO", -1, "2026-11-02T04:00:00+00:00"),
        ("Crude Oil", "hurts", "Reliance Industries", -1, "2026-11-02T04:00:00+00:00"),
    ])
    conn = sqlite3.connect(p)
    prop = fwd.load_propagated_activations(conn)
    assert {"date": "2026-11-02", "ticker": "MARUTI", "relation": fwd.PROPAGATED, "sign": -1} in prop
    assert all(a["relation"] == fwd.PROPAGATED and a["date"] == "2026-11-02" for a in prop)
    assert "RELIANCE" not in {a["ticker"] for a in prop}
    rep = fwd.run(db_path=p, bars_loader=lambda syms: {s: _bars() for s in syms}, ledger_path=None, n_shuffles=5)
    assert rep["propagated_calls"] == len(prop) and rep["verdicts"][fwd.PROPAGATED]["n"] >= 1


def test_a_later_edge_does_not_carry_an_earlier_seed(tmp_path):
    import sqlite3
    p = _db(tmp_path, [
        ("Crude Oil", "hurts", "theme:Freight Costs", -1, "2026-11-02T04:00:00+00:00"),
        ("theme:Freight Costs", "hurts", "Maruti Suzuki", -1, "2026-11-05T04:00:00+00:00"),
        ("Crude Oil", "hurts", "theme:Freight Costs", -1, "2026-11-06T04:00:00+00:00"),
    ])
    prop = fwd.load_propagated_activations(sqlite3.connect(p))
    maruti = {a["date"] for a in prop if a["ticker"] == "MARUTI"}
    assert "2026-11-06" in maruti and "2026-11-02" not in maruti

"""
Tests for the knowledge-graph visualizer (src/graph_viz.py). Offline.

Run either of these from the project folder:
    python tests/test_graph_viz.py
    python -m pytest tests/test_graph_viz.py
"""

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, graph_engine, graph_viz


def _seed(conn):
    graph_engine.add_edge(conn, "golden_cross", "preceded", "win",
                          confidence_score=0.8)
    graph_engine.add_edge(conn, "vix_spike", "caused", "loss",
                          confidence_score=1.0, decay_lambda=0.0)
    graph_engine.add_edge(conn, "MISTY SEAS FUND", "concentrates_in", "ADANI",
                          confidence_score=0.9, source="affinity_projected")
    # One expired edge (kept, ghosted).
    graph_engine.add_edge(conn, "old_pattern", "preceded", "win",
                          confidence_score=0.05)
    conn.execute("UPDATE graph_edges SET invalid_at = '2026-07-01' "
                 "WHERE source_node = 'old_pattern'")
    conn.commit()


def test_build_graph_json_shapes_kinds_and_flags():
    conn = brain_map.connect(":memory:")
    _seed(conn)
    g = graph_viz.build_graph_json(conn)
    assert g["stats"]["nodes"] == 7
    assert g["stats"]["edges_active"] == 3 and g["stats"]["edges_expired"] == 1
    assert g["stats"]["affinity"] == 1 and g["stats"]["loss_permanent"] == 1
    kinds = {n["id"]: n["kind"] for n in g["nodes"]}
    assert kinds["MISTY SEAS FUND"] == "entity"
    assert kinds["ADANI"] == "group"
    assert kinds["loss"] == "negative" and kinds["win"] == "positive"
    loss_edge = next(e for e in g["edges"] if e["from"] == "vix_spike")
    assert loss_edge["loss_permanent"] is True
    exp = next(e for e in g["edges"] if e["from"] == "old_pattern")
    assert exp["expired"] is True


def test_empty_db_yields_empty_graph_never_raises():
    conn = brain_map.connect(":memory:")
    g = graph_viz.build_graph_json(conn)
    assert g["nodes"] == [] and g["edges"] == []
    assert "generated" in g


def test_html_is_self_contained_and_embeds_the_data():
    conn = brain_map.connect(":memory:")
    _seed(conn)
    g = graph_viz.build_graph_json(conn)
    html = graph_viz.render_html(g)
    assert "MISTY SEAS FUND" in html
    # Round-trip: the embedded JSON parses back to the same stats.
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    assert m and json.loads(m.group(1))["stats"] == g["stats"]
    # Self-contained: no external fetches (CSP/offline rule).
    assert "http://" not in html
    assert "https://" not in html
    assert "<canvas" in html and "data-theme" in html


def test_write_viz_cli_roundtrip(tmp_path=None):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "brain.db"
        conn = brain_map.connect(str(db))
        _seed(conn)
        conn.close()
        out = graph_viz.write_viz(db, tmp / "viz.html")
        assert out is not None and out.exists()
        assert "Knowledge Graph" in out.read_text()
        # Missing DB fails soft (sqlite creates empty -> empty graph page).
        out2 = graph_viz.write_viz(tmp / "ghost.db", tmp / "viz2.html")
        assert out2 is not None


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

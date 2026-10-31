"""
GraphRAG news shadow (decision #174): every RSS headline through the GraphRAG extractor, written to a SEPARATE graph.

    python -m src.knowledge_graph.graphrag_news_shadow [--report]

Analytics node only. The live path extracts only from policy/trade events inside `event_engine.emit`; this job
feeds all RSS items, without that gate, into `data/graphrag_shadow.db`. Nothing reads that file: no card, no
spreading activation, no Discord. It exists to measure whether ungated news makes a useful graph.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import brain_map
from src import node_role
from src.knowledge_graph import graphrag
from src.knowledge_graph import graphrag_extract as gx

ROOT = Path(__file__).resolve().parent.parent.parent
SHADOW_DB_PATH = ROOT / "data" / "graphrag_shadow.db"
SEEN_PATH = ROOT / "logs" / "graphrag_shadow_seen.txt"
CALL_LEDGER_PATH = ROOT / "logs" / "graphrag_shadow_calls.jsonl"
REJECTED_PREDICATES_PATH = ROOT / "logs" / "graphrag_shadow_rejected_predicates.log"
REVIEW_LEDGER_PATH = ROOT / "logs" / "graphrag_shadow_review.jsonl"
DEFAULT_DAILY_CAP = 300

_DONE = ("ok", "skipped (duplicate content_hash)", "skipped (empty text)")


def _load_seen(path: Path) -> set:
    try:
        return set(path.read_text().split())
    except OSError:
        return set()


def _add_seen(path: Path, chash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(chash + "\n")


def run(items=None, db_path=None, seen_path=None, call_ledger_path=None, rejected_path=None, review_path=None,
        extractor=None, cfg=None, env=None, hostname=None, fetch_fn=None) -> dict:
    """One pass over the current RSS items; an item is marked seen only once the extractor finished with it."""
    from src.event_engine import detect, settings
    cfg = cfg if cfg is not None else settings()
    out = {"status": "ok", "items": 0, "already_seen": 0, "extracted": 0, "written": 0, "rejected": 0,
           "rejected_reasons": {}, "retry_later": 0}
    if not cfg.get("graphrag_shadow_enabled", False):
        out["status"] = "disabled"
        return out
    refusal = node_role.llm_jobs_refusal(env=env, hostname=hostname)
    if refusal is not None:
        out["status"] = f"skipped ({refusal})"
        return out
    seen_p = Path(seen_path or SEEN_PATH)
    seen = _load_seen(seen_p)
    items = detect.rss_items(fetch_fn=fetch_fn) if items is None else items
    conn = brain_map.connect(db_path or SHADOW_DB_PATH)
    try:
        graphrag.ensure_schema(conn)
        for item in items:
            chash = gx.content_hash_for(gx.compose_input_text(item))
            if chash in seen:
                out["already_seen"] += 1
                continue
            out["items"] += 1
            r = gx.extract_from_item(
                item, conn=conn, extractor=extractor,
                call_ledger_path=Path(call_ledger_path or CALL_LEDGER_PATH),
                rejected_predicates_path=Path(rejected_path or REJECTED_PREDICATES_PATH),
                review_ledger_path=Path(review_path or REVIEW_LEDGER_PATH),
                notify_fn=lambda text: None,
                daily_cap=int(cfg.get("graphrag_shadow_daily_cap", DEFAULT_DAILY_CAP)),
                env=env, hostname=hostname,
            )
            status = r.get("status") or ""
            if status.startswith("skipped (daily extraction cap"):
                out["status"] = status
                break
            if status not in _DONE:
                out["retry_later"] += 1
                continue
            seen.add(chash)
            _add_seen(seen_p, chash)
            out["extracted"] += int(status == "ok")
            out["written"] += int(r.get("written") or 0)
            out["rejected"] += int(r.get("rejected") or 0)
            for k, v in (r.get("rejected_reasons") or {}).items():
                out["rejected_reasons"][k] = out["rejected_reasons"].get(k, 0) + v
    finally:
        conn.close()
    return out


def report(db_path=None) -> dict:
    """What the shadow graph holds: extracted edges per day, distinct nodes, edges touching the option universe."""
    p = Path(db_path or SHADOW_DB_PATH)
    if not p.exists():
        return {"status": "no shadow graph yet"}
    conn = brain_map.connect(p)
    try:
        graphrag.ensure_schema(conn)
        per_day = {r[0]: r[1] for r in conn.execute(
            "SELECT substr(extracted_at, 1, 10), COUNT(*) FROM graph_edge_evidence GROUP BY 1 ORDER BY 1")}
        edges = conn.execute("SELECT source_node, target_node FROM graph_edges WHERE source = 'llm_extracted'").fetchall()
        uni = {r[0] for r in conn.execute("SELECT node_id FROM graph_nodes WHERE in_universe = 1")}
        nodes = {n for e in edges for n in (e[0], e[1])}
        return {"status": "ok", "evidence_per_day": per_day, "llm_edges": len(edges), "distinct_nodes": len(nodes),
                "edges_touching_universe": sum(1 for e in edges if e[0] in uni or e[1] in uni)}
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--report", action="store_true", help="print what the shadow graph holds; no LLM call")
    a = ap.parse_args(argv)
    res = report() if a.report else run()
    print(json.dumps(dict(res, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))), file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

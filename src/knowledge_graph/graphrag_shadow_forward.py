"""
GraphRAG shadow forward test (decision #185): do the news-shadow edges call the next week's price move?

    python -m src.knowledge_graph.graphrag_shadow_forward [--horizon N ...]     (node cron 22:10 Mon-Fri)

Reads `data/graphrag_shadow.db` read-only. Every evidence row whose TARGET is a listed company is one dated call:
the edge's polarity says that company should rise (+1) or fall (-1). Each call is graded with the alt-data court in
`graphrag_eval` — entry at the open of the first session after the news day (IST), exit at the close `horizon`
sessions later, non-overlapping per (relation, ticker), against a shuffled-ticker placebo on the same dates.
Verdicts only, appended to `logs/graphrag_shadow_forward.jsonl`; nothing is promoted and nothing reads the result.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.knowledge_graph import graphrag_eval as ge

ROOT = Path(__file__).resolve().parent.parent.parent
SHADOW_DB_PATH = ROOT / "data" / "graphrag_shadow.db"
LEDGER_PATH = ROOT / "logs" / "graphrag_shadow_forward.jsonl"
BAR_DAYS = 120
ALL_EDGES = "ALL"
HORIZONS = (1, 3, 5)                # 5 is the verdict horizon; 1 and 3 are early reads
IST = timezone(timedelta(hours=5, minutes=30))


def _ist_day(ts: str) -> str | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.astimezone(IST) if dt.tzinfo else dt).date().isoformat()


def load_shadow_activations(conn) -> list[dict]:
    """One call per (IST news day, target company, relation, polarity) from the shadow graph's evidence."""
    rows = conn.execute(
        "SELECT v.extracted_at, e.relation, e.polarity, e.target_node "
        "FROM graph_edge_evidence v JOIN graph_edges e ON e.source_node = v.source_node "
        "AND e.relation = v.relation AND e.target_node = v.target_node "
        "JOIN graph_nodes n ON n.node_id = e.target_node "
        "WHERE e.source = 'llm_extracted' AND n.node_type = 'company'").fetchall()
    seen, out = set(), []
    for ts, rel, pol, target in rows:
        day, sign = _ist_day(ts), int(pol or 0)
        key = (day, target, rel, sign)
        if day and sign and key not in seen:
            seen.add(key)
            out.append({"date": day, "ticker": ge._bare_symbol(target), "relation": rel, "sign": sign})
    return out


def run(db_path=None, bars_loader=None, horizon_sessions: int = ge.DEFAULT_HORIZON_SESSIONS,
        n_shuffles: int = ge.DEFAULT_PLACEBO_DRAWS, seed: int = 0, ledger_path=LEDGER_PATH, now_iso=None) -> dict:
    """Grade every relation plus ALL edges pooled; append one JSON line to the ledger."""
    p = Path(db_path or SHADOW_DB_PATH)
    report = {"run_at": now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "horizon_sessions": horizon_sessions}
    if not p.exists():
        report["status"] = "no shadow graph yet"
        return report
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        acts = load_shadow_activations(conn)
    finally:
        conn.close()
    tickers = sorted({a["ticker"] for a in acts})
    if bars_loader is None:
        from src.ingestion import bhavcopy_clerk
        bars_loader = lambda syms: bhavcopy_clerk.bars_for_many(syms, days=BAR_DAYS)
    bars = bars_loader(tickers) if tickers else {}
    pooled = [dict(a, relation=ALL_EDGES) for a in acts]
    verdicts = ge.evaluate_altdata_edge_classes(acts, bars, horizon_sessions=horizon_sessions,
                                                n_shuffles=n_shuffles, seed=seed)
    verdicts[ALL_EDGES] = ge.evaluate_altdata_edge_classes(pooled, bars, horizon_sessions=horizon_sessions,
                                                           n_shuffles=n_shuffles, seed=seed)[ALL_EDGES] if pooled else None
    report.update(status="ok", calls=len(acts), tickers=len(tickers),
                  calls_by_day=dict(sorted(Counter(a["date"] for a in acts).items())), verdicts=verdicts)
    if ledger_path:
        lp = Path(ledger_path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a") as fh:
            fh.write(json.dumps(report, sort_keys=True, default=str) + "\n")
    return report


def summary_lines(report: dict) -> list[str]:
    if report.get("status") != "ok":
        return [f"shadow forward test: {report.get('status')}"]
    out = [f"shadow forward test, {report['horizon_sessions']}-session horizon: {report['calls']} calls on "
           f"{report['tickers']} companies"]
    for rel, v in sorted((report.get("verdicts") or {}).items(), key=lambda kv: -(kv[1] or {}).get("n", 0)):
        if not v:
            continue
        hit = "—" if v["hit_rate"] is None else f"{v['hit_rate']:.0%}"
        mean = "—" if v["mean_signed_return_pct"] is None else f"{v['mean_signed_return_pct']:+.2f}%"
        p = (v.get("placebo") or {}).get("p_value")
        out.append(f"  {rel:24s} n={v['n']:<4d} hit={hit:>4s} mean={mean:>7s} "
                   f"placebo_p={'—' if p is None else f'{p:.3f}'}  {v['status']}: {v['reason']}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--horizon", type=int, action="append", help="sessions; repeatable (default 1, 3 and 5)")
    a = ap.parse_args(argv)
    for h in a.horizon or HORIZONS:
        print("\n".join(summary_lines(run(horizon_sessions=h))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

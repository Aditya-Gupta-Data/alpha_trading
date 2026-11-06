"""
GraphRAG shadow forward test (decision #185): do the news-shadow edges call the next week's price move?

    python -m src.knowledge_graph.graphrag_shadow_forward [--horizon N ...]     (node cron 22:10 Mon-Fri)

Reads `data/graphrag_shadow.db` read-only. Every evidence row whose TARGET is a listed company is one dated call:
the edge's polarity says that company should rise (+1) or fall (-1). Each call is graded with the alt-data court in
`graphrag_eval` — entry at the open of the first session after the news day (IST), exit at the close `horizon`
sessions later, non-overlapping per (relation, ticker), against a shuffled-ticker placebo on the same dates.
Verdicts only, appended to `logs/graphrag_shadow_forward.jsonl`; nothing is promoted and nothing reads the result.
Class PROPAGATED (#186): companies reached from a non-company target through spreading activation, graded apart.
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
PROPAGATED = "PROPAGATED"
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


def load_propagated_activations(conn) -> list[dict]:
    """Calls for companies REACHED from a non-company target (crude, a sector, a policy) by spreading activation over
    the structural links plus the shadow edges first seen on or before that news day — never later edges."""
    import networkx as nx
    from src.knowledge_graph import graphrag, spreading_activation
    types = dict(conn.execute("SELECT node_id, node_type FROM graph_nodes").fetchall())
    g = nx.DiGraph()
    for s, t, w, c, p in conn.execute(
            "SELECT source_node, target_node, weight, confidence_score, polarity FROM graph_edges "
            "WHERE source = ? AND invalid_at IS NULL", (graphrag.STRUCTURAL_SOURCE,)):
        g.add_edge(s, t, weight=w, confidence_score=c, polarity=p, edge_class="membership")
    first_seen = {}
    for s, rel, t, ts in conn.execute("SELECT source_node, relation, target_node, extracted_at FROM graph_edge_evidence"):
        day = _ist_day(ts)
        if day and day < first_seen.get((s, rel, t), "9999"):
            first_seen[(s, rel, t)] = day
    llm = {(s, rel, t): (w, p) for s, rel, t, w, p in conn.execute(
        "SELECT source_node, relation, target_node, weight, polarity FROM graph_edges WHERE source = 'llm_extracted'")}
    seeds_by_day = {}
    for s, rel, t, ts in conn.execute("SELECT source_node, relation, target_node, extracted_at FROM graph_edge_evidence"):
        w_p = llm.get((s, rel, t))
        if w_p and types.get(t) != "company" and int(w_p[1] or 0) and _ist_day(ts):
            seeds_by_day.setdefault(_ist_day(ts), set()).add((t, int(w_p[1])))
    out, seen = [], set()
    pending = sorted(llm.items(), key=lambda kv: first_seen.get(kv[0], "9999"))
    i = 0
    for day in sorted(seeds_by_day):
        while i < len(pending) and first_seen.get(pending[i][0], "9999") <= day:
            (s, _, t), (w, p) = pending[i]
            g.add_edge(s, t, weight=w, confidence_score=0.5 * float(w or 0), polarity=p)
            i += 1
        for target, sign in sorted(seeds_by_day[day]):
            res = spreading_activation.propagate(g, {target: float(sign)}, conn=None, log_activation=False)
            for a in res.get("activated") or []:
                e = float(a.get("energy") or 0)
                key = (day, a["node_id"], (e > 0) - (e < 0))
                if types.get(a["node_id"]) == "company" and key[2] and key not in seen:
                    seen.add(key)
                    out.append({"date": day, "ticker": ge._bare_symbol(a["node_id"]), "relation": PROPAGATED,
                                "sign": key[2]})
    return out


def load_calls(db_path=None):
    """(direct, propagated) calls from the shadow graph, read-only; None when there is no shadow graph yet."""
    p = Path(db_path or SHADOW_DB_PATH)
    if not p.exists():
        return None
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return load_shadow_activations(conn), load_propagated_activations(conn)
    finally:
        conn.close()


def run(db_path=None, bars_loader=None, horizon_sessions: int = ge.DEFAULT_HORIZON_SESSIONS,
        n_shuffles: int = ge.DEFAULT_PLACEBO_DRAWS, seed: int = 0, ledger_path=LEDGER_PATH, now_iso=None,
        calls=None) -> dict:
    """Grade every relation, ALL direct calls pooled, and PROPAGATED; append one JSON line to the ledger."""
    report = {"run_at": now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "horizon_sessions": horizon_sessions}
    calls = calls if calls is not None else load_calls(db_path)
    if calls is None:
        report["status"] = "no shadow graph yet"
        return report
    acts, prop = calls
    tickers = sorted({a["ticker"] for a in acts + prop})
    if bars_loader is None:
        from src.ingestion import bhavcopy_clerk
        bars_loader = lambda syms: bhavcopy_clerk.bars_for_many(syms, days=BAR_DAYS)
    bars = bars_loader(tickers) if tickers else {}
    grade = lambda a: ge.evaluate_altdata_edge_classes(a, bars, horizon_sessions=horizon_sessions,
                                                       n_shuffles=n_shuffles, seed=seed)
    verdicts = grade(acts)
    verdicts[ALL_EDGES] = grade([dict(a, relation=ALL_EDGES) for a in acts])[ALL_EDGES] if acts else None
    verdicts[PROPAGATED] = grade(prop)[PROPAGATED] if prop else None
    report.update(status="ok", calls=len(acts), propagated_calls=len(prop), tickers=len(tickers),
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
    out = [f"shadow forward test, {report['horizon_sessions']}-session horizon: {report['calls']} direct + "
           f"{report.get('propagated_calls', 0)} propagated calls on {report['tickers']} companies"]
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
    calls = load_calls()
    for h in a.horizon or HORIZONS:
        print("\n".join(summary_lines(run(horizon_sessions=h, calls=calls))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

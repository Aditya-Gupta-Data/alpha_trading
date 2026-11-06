# MANUAL OFFLINE TOOL — GraphRAG filings BACKTEST (branch feature/graphrag-filings-backtest). Not on any cron.
# A BACKTEST, never proof: the LLM was trained on years it reads, so results can flatter. Forward data decides (#185).
# Everything lands in data/backtest/ — never data/brain_map.db, never the shadow graph, never the forward ledger.
#   python3 scripts/backtest_filings.py backfill --year 2025      NSE announcements with text, order/contract/M&A only
#   python3 scripts/backtest_filings.py items --year 2025         items.jsonl + system.txt for the laptop runner
#   (laptop) powershell -File backtest_filings_runner.ps1         Ollama, one raw JSON line per item, resumable
#   python3 scripts/backtest_filings.py ingest --year 2025 --raw FILE   production validator into the backtest graph
#   python3 scripts/backtest_filings.py grade --year 2025         the #185/#186 court on the backtest graph + FILER classes
import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "backtest"
KEEP_CLASSES = {"CATALYST", "EXPANSION"}
IST = timezone(timedelta(hours=5, minutes=30))
BAR_DAYS = 800
WEEK_PAUSE_SECS = 8
# Shareholding / takeover-code paperwork: "Substantial Acquisition of Shares" trips the EXPANSION keyword but names no business link.
_BOILERPLATE = re.compile(r"substantial acquisition of shares|takeovers?\) regulation|regulation (29|31)\s*\(|regulation 30 of|"
                          r"listing obligations and disclosure requirements|news verification|clarification", re.I)
_SKIP_DESC = {"Disclosure under SEBI Takeover Regulations", "News Verification", "Resignation"}


def substantive(desc: str, text: str) -> bool:
    return desc not in _SKIP_DESC and not _BOILERPLATE.search(text or "")


def paths(year: int, out_dir=None) -> dict:
    d = Path(out_dir or OUT_DIR)
    return {"filings": d / f"filings_{year}.jsonl", "done": d / f"filings_{year}_weeks_done.txt",
            "items": d / f"items_{year}.jsonl", "system": d / "system.txt", "db": d / f"graph_{year}.db",
            "ledger": d / f"forward_{year}.jsonl"}


def keep(row: dict) -> dict | None:
    """An order / contract / M&A filing with real text, else None. Boilerplate ('...about Acquisition') is dropped."""
    from src.ingestion import corporate_events as ce
    desc = str(row.get("desc") or "").strip()
    text = re.sub(r"\s+", " ", str(row.get("attchmntText") or "")).strip()
    if not KEEP_CLASSES & set(ce.classify(f"{desc} {text}")):
        return None
    if len(text) < 90 or text.lower().rstrip(". ").endswith(f"about {desc.lower()}") or not substantive(desc, text):
        return None
    try:
        ts = datetime.strptime(str(row.get("an_dt")), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        return None
    return {"id": str(row.get("seq_id") or f"{row.get('symbol')}-{row.get('an_dt')}"), "at": ts.isoformat(),
            "symbol": str(row.get("symbol") or "").upper(), "name": row.get("sm_name") or "", "desc": desc, "text": text}


def backfill(year: int, out_dir=None, fetch_fn=None, pause: float = WEEK_PAUSE_SECS) -> dict:
    """Week by week, resumable; one NSE request per week with a pause between (one NSE job at a time)."""
    from src.ingestion import corporate_events as ce
    p = paths(year, out_dir)
    p["filings"].parent.mkdir(parents=True, exist_ok=True)
    done = set(p["done"].read_text().split()) if p["done"].exists() else set()
    seen = {json.loads(l)["id"] for l in p["filings"].open()} if p["filings"].exists() else set()
    stats = {"weeks": 0, "rows": 0, "kept": 0, "failed_weeks": []}
    start = date(year, 1, 1)
    while start.year == year:
        end = min(start + timedelta(days=6), date(year, 12, 31))
        key = start.isoformat()
        if key not in done:
            rows = (fetch_fn or ce.fetch)(start, end)
            if not rows:
                stats["failed_weeks"].append(key)
            else:
                with p["filings"].open("a") as fh:
                    for r in rows:
                        k = keep(r)
                        if k and k["id"] not in seen:
                            seen.add(k["id"])
                            fh.write(json.dumps(k, ensure_ascii=False) + "\n")
                            stats["kept"] += 1
                with p["done"].open("a") as fh:
                    fh.write(key + "\n")
                stats["rows"] += len(rows)
            stats["weeks"] += 1
            print(f"  {key}: {len(rows)} rows, kept so far {stats['kept']}", flush=True)
            time.sleep(pause)
        start = end + timedelta(days=1)
    return stats


def items(year: int, out_dir=None) -> int:
    """items.jsonl ({id, text} in the production input format) + the production system prompt."""
    from src.knowledge_graph import graphrag_extract as gx
    p = paths(year, out_dir)
    n = 0
    with p["items"].open("w", encoding="utf-8") as fh:
        for line in p["filings"].open(encoding="utf-8"):
            f = json.loads(line)
            if not substantive(f["desc"], f["text"]):
                continue
            fh.write(json.dumps({"id": f["id"], "text": gx.compose_input_text({"title": f["desc"], "text": f["text"]})},
                                ensure_ascii=False) + "\n")
            n += 1
    p["system"].write_text(gx.EXTRACT_SYSTEM_PROMPT, encoding="utf-8")
    return n


def ingest(year: int, raw_path, out_dir=None) -> dict:
    """Validate each raw LLM answer with the production validator; write accepted edges dated at the filing time."""
    from src import brain_map
    from src.knowledge_graph import graphrag, graphrag_extract as gx
    p = paths(year, out_dir)
    filings = {json.loads(l)["id"]: json.loads(l) for l in p["filings"].open(encoding="utf-8")}
    texts = {json.loads(l)["id"]: json.loads(l)["text"] for l in p["items"].open(encoding="utf-8")}
    conn = brain_map.connect(p["db"])
    graphrag.ensure_schema(conn)
    if conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 0:
        graphrag.seed_nodes(conn)
    graphrag.seed_learning_companies(conn)
    stats = {"answers": 0, "json_ok": 0, "edges": 0, "written": 0, "rejected": {}}
    try:
        for line in Path(raw_path).open(encoding="utf-8"):
            r = json.loads(line)
            f, text = filings.get(str(r.get("id"))), texts.get(str(r.get("id")))
            if not f or text is None:
                continue
            stats["answers"] += 1
            m = re.search(r"\{.*\}", r.get("raw") or "", re.S)
            try:
                edges = (json.loads(m.group(0)).get("edges") or []) if m else None
            except ValueError:
                edges = None
            if not isinstance(edges, list):
                continue
            stats["json_ok"] += 1
            for e in edges:
                stats["edges"] += 1
                ok, why, norm = gx.validate_raw_edge(conn, e, text)
                if not ok:
                    stats["rejected"][why] = stats["rejected"].get(why, 0) + 1
                    continue
                res = graphrag.upsert_graphrag_edge(
                    conn, source_node=norm["source_node"], relation=norm["relation"], target_node=norm["target_node"],
                    edge_class=norm["edge_class"], polarity=norm["polarity"], strength=norm["strength"],
                    content_hash=gx.content_hash_for(text), quote=norm["quote"], source_ref=f"nse:{f['id']}",
                    extractor="backtest:laptop", extracted_at=f["at"])
                stats["written"] += int(not res.get("duplicate_evidence"))
        conn.commit()
    finally:
        conn.close()
    return stats


_ORDER = re.compile(r"\border|contract|letter of (award|intent)|\bLoA\b|\bLoI\b|awarded|bags|secures", re.I)
_MNA = re.compile(r"acqui|merger|amalgamat|stake|takeover", re.I)


def filer_calls(year: int, out_dir=None) -> list:
    """FILER classes: the announcing company itself, sign +1, one call per filing. An event study of the
    announcement, not of the graph — FILER_ORDER for orders/contracts, FILER_MNA for acquisitions/mergers."""
    p = paths(year, out_dir)
    out = []
    for line in p["filings"].open(encoding="utf-8"):
        f = json.loads(line)
        if not substantive(f["desc"], f["text"]) or not f.get("symbol"):
            continue
        blob = f"{f['desc']} {f['text']}"
        kind = "FILER_ORDER" if _ORDER.search(blob) else "FILER_MNA" if _MNA.search(blob) else None
        if kind:
            out.append({"date": f["at"][:10], "ticker": f["symbol"], "relation": kind, "sign": 1})
    return out


def grade(year: int, out_dir=None, horizons=(1, 3, 5), bars_loader=None) -> list:
    """The forward-test court (#185/#186) on the backtest graph plus the FILER classes; ledger in data/backtest/."""
    from src.knowledge_graph import graphrag_eval as ge
    from src.knowledge_graph import graphrag_shadow_forward as fwd
    p = paths(year, out_dir)
    if bars_loader is None:
        from src.ingestion import bhavcopy_clerk
        bars_loader = lambda syms: bhavcopy_clerk.bars_for_many(syms, days=BAR_DAYS)
    calls = fwd.load_calls(p["db"]) or ([], [])
    filers = filer_calls(year, out_dir)
    filer_bars = bars_loader(sorted({c["ticker"] for c in filers})) if filers else {}
    reports = []
    for h in horizons:
        rep = fwd.run(db_path=p["db"], bars_loader=bars_loader, horizon_sessions=h, ledger_path=None, calls=calls)
        rep["verdicts"] = dict(rep.get("verdicts") or {}, **ge.evaluate_altdata_edge_classes(
            filers, filer_bars, horizon_sessions=h)) if filers else rep.get("verdicts")
        rep.update(filer_calls=len(filers), label=f"BACKTEST {year} (filings, laptop LLM) — not forward evidence")
        with p["ledger"].open("a") as fh:
            fh.write(json.dumps(rep, sort_keys=True, default=str) + "\n")
        reports.append(rep)
        print("\n".join(fwd.summary_lines(rep)))
    return reports


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "items", "ingest", "grade"])
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--raw", help="ingest: the laptop runner's output file")
    a = ap.parse_args(argv)
    if a.mode == "backfill":
        print(json.dumps(backfill(a.year)))
    elif a.mode == "items":
        print(f"wrote {items(a.year)} items to {paths(a.year)['items']}")
    elif a.mode == "ingest":
        print(json.dumps(ingest(a.year, a.raw)))
    else:
        grade(a.year)


if __name__ == "__main__":
    main()

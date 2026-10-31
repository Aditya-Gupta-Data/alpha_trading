# MANUAL OFFLINE TOOL — local-LLM benchmark, test 7 (2026-10-30). Not on any cron, writes nothing to data/ or logs/.
# Compares GraphRAG edges from the router and from local models on the SAME real news items, validated by the production validator.
#   Mac:      python3 scripts/llm_bench/bench7_edge_compare.py prep --out DIR --n 40        (items.jsonl + system.txt from the RSS feeds)
#   Mini PC:  python3 bench7_edge_compare.py router --out DIR                                (needs ALPHA_LLM_* env; writes router.jsonl)
#   laptop:   PowerShell loop over items.jsonl against Ollama (think=false), one <model>.jsonl each
#   Mac:      python3 scripts/llm_bench/bench7_edge_compare.py validate --out DIR            (validator + side-by-side; reads every *.jsonl dump)
import argparse
import html
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UA = {"User-Agent": "Mozilla/5.0"}


def prep(out, n, feeds_path):
    sys.path.insert(0, str(ROOT))
    from src.knowledge_graph import graphrag_extract as gx
    feeds = json.load(open(feeds_path))["feeds"]
    items, seen = [], set()
    for f in feeds:
        try:
            raw = subprocess.run(["curl", "-sL", "-m", "30", "-A", "Mozilla/5.0", f["url"]], capture_output=True, check=True).stdout
            root = ET.fromstring(raw)
        except Exception as e:
            print(f"feed FAILED {f['name']}: {e}")
            continue
        got = 0
        for it in root.iter("item"):
            title = html.unescape((it.findtext("title") or "").strip())
            summ = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
            summ = re.sub(r"\s+", " ", summ).strip()
            if len(title) < 20 or title in seen:
                continue
            seen.add(title)
            items.append({"title": title, "summary": summ})
            got += 1
        print(f"feed ok {f['name']}: {got}")
    random.Random(7).shuffle(items)
    items = items[:n]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "items.jsonl", "w", encoding="utf-8") as fh:
        for i, it in enumerate(items, 1):
            fh.write(json.dumps({"id": i, "title": it["title"], "text": gx.compose_input_text(it)}, ensure_ascii=False) + "\n")
    (out / "system.txt").write_text(gx.EXTRACT_SYSTEM_PROMPT, encoding="utf-8")
    print(f"wrote {len(items)} items to {out}")


def router(out):
    base = os.environ.get("ALPHA_LLM_BASE_URL", "http://localhost:3001/v1").rstrip("/")
    key = (os.environ.get("ALPHA_LLM_API_KEY") or "").strip().strip('"').strip("'")
    model = os.environ.get("ALPHA_LLM_MODEL", "auto:fast")
    out = Path(out)
    system = (out / "system.txt").read_text(encoding="utf-8")
    done = 0
    with open(out / "router.jsonl", "w", encoding="utf-8") as fh:
        for line in open(out / "items.jsonl", encoding="utf-8"):
            it = json.loads(line)
            body = {"model": model, "temperature": 0, "max_tokens": 900, "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": it["text"]}]}
            raw, secs, served = "", 0.0, "?"
            for attempt in range(4):
                req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                                             {"Content-Type": "application/json", "Authorization": "Bearer " + key})
                t0 = time.time()
                try:
                    d = json.load(urllib.request.urlopen(req, timeout=180))
                    raw, served = d["choices"][0]["message"]["content"] or "", d.get("model", "?")
                    secs = time.time() - t0
                    break
                except Exception:
                    time.sleep(8 * (attempt + 1))
            fh.write(json.dumps({"id": it["id"], "raw": raw, "secs": round(secs, 1), "served": served}) + "\n")
            fh.flush()
            done += 1
            time.sleep(1.5)
    print(f"router done {done}")


def jparse(t):
    m = re.search(r"\{.*\}", t or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def validate(out):
    sys.path.insert(0, str(ROOT))
    from src.knowledge_graph import graphrag, graphrag_extract as gx
    out = Path(out)
    items = {}
    for line in open(out / "items.jsonl", encoding="utf-8"):
        it = json.loads(line)
        items[it["id"]] = it
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    graphrag.seed_nodes(conn)
    res = {}
    for p in sorted(out.glob("*.jsonl")):
        if p.name == "items.jsonl":
            continue
        name = p.stem
        res[name] = {}
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            it = items.get(r["id"])
            if not it:
                continue
            d = jparse(r.get("raw"))
            edges = (d or {}).get("edges") if isinstance(d, dict) else None
            acc, why = [], {}
            for e in edges or []:
                ok, reason, norm = gx.validate_raw_edge(conn, e, it["text"])
                if ok:
                    acc.append(norm)
                else:
                    why[reason] = why.get(reason, 0) + 1
            res[name][r["id"]] = {"json_ok": d is not None, "n": len(edges or []), "acc": acc, "why": why, "secs": r.get("secs")}
    key = lambda e: (e["source_node"]["node_id"], e["relation"], e["target_node"]["node_id"])
    ref = res.get("router", {})
    print(f"{'backend':16s} {'items':>5s} {'json_ok':>7s} {'edges':>5s} {'accepted':>8s} {'acc%':>5s} {'items>=1 acc':>12s} {'mean s':>7s}  rejections")
    for name, rows in res.items():
        n = len(rows)
        tot = sum(r["n"] for r in rows.values())
        acc = sum(len(r["acc"]) for r in rows.values())
        nz = sum(1 for r in rows.values() if r["acc"])
        why = {}
        for r in rows.values():
            for k, v in r["why"].items():
                why[k] = why.get(k, 0) + v
        secs = [r["secs"] for r in rows.values() if r["secs"]]
        print(f"{name:16s} {n:5d} {sum(r['json_ok'] for r in rows.values()):7d} {tot:5d} {acc:8d} {100*acc/max(tot,1):4.0f}% {nz:12d} {sum(secs)/max(len(secs),1):7.1f}  {why}")
    if ref:
        print("\nAgreement with router (accepted edges, same endpoints+relation):")
        for name, rows in res.items():
            if name == "router":
                continue
            same = opp = only_local = only_router = 0
            for i, r in rows.items():
                a = {key(e): e["polarity"] for e in r["acc"]}
                b = {key(e): e["polarity"] for e in ref.get(i, {}).get("acc", [])}
                for k, v in a.items():
                    if k in b:
                        same += v == b[k]
                        opp += v != b[k]
                    else:
                        only_local += 1
                only_router += sum(1 for k in b if k not in a)
            print(f"  {name:14s} same-direction {same}  opposite {opp}  only-this-model {only_local}  only-router {only_router}")
    print("\nPer-item accepted edges (subject -[relation, polarity, strength]-> object):")
    for i in sorted(items):
        print(f"\n#{i} {items[i]['title'][:110]}")
        for name, rows in res.items():
            r = rows.get(i)
            if r is None:
                continue
            edges = "; ".join(f"{e['source_node']['node_id']} -[{e['relation']},{e['polarity']:+d},{e['strength']}]-> {e['target_node']['node_id']}" for e in r["acc"]) or "(none)"
            print(f"   {name:12s} {edges}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["prep", "router", "validate"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--feeds", default=str(ROOT / "config" / "rss_feeds.json"))
    a = ap.parse_args()
    {"prep": lambda: prep(a.out, a.n, a.feeds), "router": lambda: router(a.out), "validate": lambda: validate(a.out)}[a.mode]()


if __name__ == "__main__":
    main()

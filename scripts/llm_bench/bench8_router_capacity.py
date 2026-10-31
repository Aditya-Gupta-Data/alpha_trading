# MANUAL OFFLINE TOOL — local-LLM benchmark, test 8 (2026-10-31). Not on any cron, writes nothing but stdout.
# How much GraphRAG extraction can the FreeLLMAPI router absorb? Stages: sequential no-pause, then 4 and 8 parallel callers, on the real prompt.
#   Mini PC:  set -a; . ~/alpha_trading/.env; set +a; python3 bench8_router_capacity.py --dir ~/bench_router/cmp --repo ~/alpha_trading --per-stage 60
import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def call(base, key, model, system, text):
    body = {"model": model, "temperature": 0, "max_tokens": 900, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
    req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=120))
        return "ok", time.time() - t0, d.get("model", "?"), d["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError as e:
        return f"http{e.code}", time.time() - t0, "?", ""
    except Exception as e:
        return type(e).__name__, time.time() - t0, "?", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--per-stage", type=int, default=60)
    a = ap.parse_args()
    sys.path.insert(0, os.path.expanduser(a.repo))
    from src.knowledge_graph import graphrag, graphrag_extract as gx
    base = os.environ.get("ALPHA_LLM_BASE_URL", "http://localhost:3001/v1").rstrip("/")
    key = (os.environ.get("ALPHA_LLM_API_KEY") or "").strip().strip('"').strip("'")
    model = os.environ.get("ALPHA_LLM_MODEL", "auto:fast")
    d = Path(os.path.expanduser(a.dir))
    system = (d / "system.txt").read_text(encoding="utf-8")
    items = [json.loads(l) for l in open(d / "items.jsonl", encoding="utf-8")]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    graphrag.seed_nodes(conn)
    lock = threading.Lock()
    for workers in (1, 4, 8):
        batch = [items[i % len(items)] for i in range(a.per_stage)]
        res, t0 = [], time.time()

        def one(it):
            st, secs, served, raw = call(base, key, model, system, it["text"])
            acc = tot = 0
            if st == "ok":
                try:
                    with lock:
                        edges = (json.loads(raw[raw.index("{"):raw.rindex("}") + 1]).get("edges") or [])
                        tot = len(edges)
                        acc = sum(1 for e in edges if gx.validate_raw_edge(conn, e, it["text"])[0])
                except Exception:
                    pass
            return st, secs, served, tot, acc

        with ThreadPoolExecutor(workers) as ex:
            res = list(ex.map(one, batch))
        wall = time.time() - t0
        st = Counter(r[0] for r in res)
        oks = [r for r in res if r[0] == "ok"]
        secs = sorted(r[1] for r in oks)
        print(f"\nworkers={workers} calls={len(res)} wall={wall:.0f}s ok={len(oks)} status={dict(st)}")
        if secs:
            print(f"  latency p50={secs[len(secs)//2]:.1f}s p90={secs[int(len(secs)*0.9)]:.1f}s max={secs[-1]:.1f}s; throughput={len(oks)/wall*60:.1f} ok/min")
            print(f"  edges={sum(r[3] for r in oks)} accepted={sum(r[4] for r in oks)}; served_by={dict(Counter(r[2] for r in oks))}")
        if len(oks) < len(res) * 0.5:
            print("  >50% failed — stopping, router is saturated at this level")
            break
        time.sleep(20)


if __name__ == "__main__":
    main()

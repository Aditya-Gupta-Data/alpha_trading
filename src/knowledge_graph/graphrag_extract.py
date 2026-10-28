"""
src/knowledge_graph/graphrag_extract.py — LLM Relationship Extractor (Event Engine -> GraphRAG)
===============================================================================================

Extracts directed supply-chain, demand, competitive, ownership, regulatory, and
causal relationships from event/news text into `data/brain_map.db` (`graph_nodes`,
`graph_edges`, `graph_edge_evidence`).

Guardrails (ARCHITECTURE_ROADMAP.md §2.3):
  1. Analytics node only (`src.node_role.llm_jobs_refusal()`). Never runs on the VM.
  2. One LLM door (`src.local_parser.LocalExtractor`, FreeLLMAPI `auto:fast`, temp 0).
  3. Strict validator (reject, never repair):
       - closed predicate vocabulary (`graphrag.PREDICATE_TO_CLASS`)
       - `quote` MUST be a non-empty verbatim substring of the input text
       - `subject != object` after canonical node resolution (aliases + modifier stripping)
       - generic endpoints ("scheme", "feedback", "sentiment") are rejected
       - both endpoints resolve via alias table or as valid non-company nodes (`in_universe=0`)
       - `polarity in (-1, 1)` — a neutral (0) or missing direction is rejected, `strength` clamped `0..1`
  4. Provenance firewall: all edges written with `source = 'llm_extracted'`,
     `decay_lambda = 0.02`, initial `confidence_score = 0.5 * strength`, and
     reinforcement `1 - (1 - c) * (1 - 0.5 * strength)`.
  5. Real-time review flag: a *new* edge touching an `in_universe` node with
     `weight >= 0.7` fires one de-duped Discord card via `notifier.fire_broadcast`.
  6. Fail-open: any LLM, DB, or network error is swallowed and logged so the
     caller (`event_engine.emit`) continues uninterrupted.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src import brain_map
from src import node_role
from src.knowledge_graph import graphrag

ROOT = Path(__file__).resolve().parent.parent.parent
REVIEW_LEDGER_PATH = ROOT / "logs" / "graphrag_review_alerts.jsonl"
CALL_LEDGER_PATH = ROOT / "logs" / "graphrag_extract_calls.jsonl"

MAX_INPUT_CHARS = 6000
REVIEW_WEIGHT_THRESHOLD = 0.7
DEFAULT_DAILY_EXTRACT_CAP = 100

EXTRACT_SYSTEM_PROMPT = (
    "Extract directed supply-chain / demand / regulatory relationships stated "
    "or directly implied by the text. Return ONLY JSON "
    '{"edges":[{"subject":str,"subject_type":str,"predicate":str,"object":str,'
    '"object_type":str,"polarity":-1|1,"strength":0..1,"quote":str}]}. '
    "polarity is +1 when the subject strengthens/raises/benefits the object and -1 when it "
    "weakens/cuts/hurts it; leave out any relationship whose direction is not clear. "
    "Name institutions in full (Reserve Bank of India, Government of India), not abbreviations. "
    "predicate in "
    + json.dumps(sorted(graphrag.PREDICATE_TO_CLASS.keys()))
    + ". subject_type and object_type in "
    + json.dumps(list(graphrag.NODE_TYPES))
    + ". quote MUST be a verbatim substring of the input. "
    "No speculation; empty list if nothing is stated."
)


def _default_writes_muzzled() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("IS_TEST_ENV"))


def compose_input_text(item: dict | str) -> str:
    """Format an event_engine item dict (or raw string) into the <=3k-char payload."""
    if isinstance(item, str):
        return item.strip()[:MAX_INPUT_CHARS]
    if not isinstance(item, dict):
        return ""
    title = (item.get("title") or "").strip()
    summary = (item.get("summary") or "").strip()
    body = (item.get("text") or "").strip()
    parts = [p for p in (title, summary, body) if p]
    deduped = []
    for p in parts:
        if not deduped or p not in deduped[-1]:
            deduped.append(p)
    return "\n\n".join(deduped)[:MAX_INPUT_CHARS]


def content_hash_for(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def already_extracted(conn, content_hash: str) -> bool:
    """True if `content_hash` already exists in `graph_edge_evidence`."""
    graphrag.ensure_schema(conn)
    row = conn.execute(
        "SELECT 1 FROM graph_edge_evidence WHERE content_hash = ? LIMIT 1",
        (content_hash,),
    ).fetchone()
    return row is not None


def validate_raw_edge(conn, raw_edge: dict, source_text: str) -> tuple[bool, str | None, dict | None]:
    """Validate one raw LLM edge dict against the strict Phase 1 rules.
    Returns `(ok, rejection_reason, normalized_edge_dict)`. Rejects, never repairs."""
    if not isinstance(raw_edge, dict):
        return False, "not_a_dict", None

    predicate = str(raw_edge.get("predicate") or "").strip().lower()
    if predicate not in graphrag.PREDICATE_TO_CLASS:
        return False, "invalid_predicate", None
    edge_class = graphrag.PREDICATE_TO_CLASS[predicate]

    quote = str(raw_edge.get("quote") or "")
    if not quote.strip() or quote not in (source_text or ""):
        return False, "quote_not_verbatim", None

    raw_pol = raw_edge.get("polarity")
    if isinstance(raw_pol, bool) or raw_pol not in (-1, 0, 1):
        try:
            pol_int = int(raw_pol)
            if str(raw_pol).strip() not in ("-1", "0", "1", "+1"):
                return False, "invalid_polarity", None
        except (TypeError, ValueError):
            return False, "invalid_polarity", None
    else:
        pol_int = int(raw_pol)
    if pol_int == 0:
        return False, "neutral_polarity", None

    raw_strength = raw_edge.get("strength")
    if isinstance(raw_strength, bool) or raw_strength is None:
        return False, "invalid_strength", None
    try:
        strength = float(raw_strength)
    except (TypeError, ValueError):
        return False, "invalid_strength", None
    if strength <= 0.0:
        return False, "invalid_strength", None
    strength = max(0.0, min(1.0, strength))

    subj_raw = str(raw_edge.get("subject") or "").strip()
    obj_raw = str(raw_edge.get("object") or "").strip()
    subj_type = str(raw_edge.get("subject_type") or "").strip().lower()
    obj_type = str(raw_edge.get("object_type") or "").strip().lower()

    for name in (subj_raw, obj_raw):
        if graphrag.is_generic_endpoint(name) and graphrag.canonical_entity_for(name) is None:
            return False, "generic_endpoint", None

    subj_node = graphrag.resolve_node(conn, subj_raw, declared_type=subj_type)
    obj_node = graphrag.resolve_node(conn, obj_raw, declared_type=obj_type)
    if subj_node is None or obj_node is None:
        return False, "unresolved_endpoint", None

    if subj_node["node_id"] == obj_node["node_id"]:
        return False, "self_loop", None

    return True, None, {
        "source_node": subj_node,
        "relation": predicate,
        "target_node": obj_node,
        "edge_class": edge_class,
        "polarity": pol_int,
        "strength": round(strength, 4),
        "quote": quote.strip()[:240],
    }


def _ask_llm(extractor, text: str) -> tuple[dict | None, str]:
    """Call `extractor` (either `LocalExtractor` with `chat_json`, `EventLLM`
    with `ask`, or a test callable) and return `(parsed_json, model_tag)`."""
    if extractor is None:
        from src.local_parser import LocalExtractor
        extractor = LocalExtractor()
    model_name = getattr(extractor, "model", "auto:fast")
    model_tag = f"llm:{model_name}"
    if callable(extractor) and not hasattr(extractor, "chat_json") and not hasattr(extractor, "ask"):
        return extractor(EXTRACT_SYSTEM_PROMPT, text), model_tag
    if hasattr(extractor, "chat_json"):
        return extractor.chat_json(EXTRACT_SYSTEM_PROMPT, text), model_tag
    if hasattr(extractor, "ask"):
        return extractor.ask(EXTRACT_SYSTEM_PROMPT, text), model_tag
    return None, model_tag


def _notify_review_edges(
    new_review_edges: list[dict],
    ledger_path: Path | str | None = None,
    notify_fn=None,
) -> int:
    """Fire one de-duped Discord broadcast card for newly extracted high-weight
    (`weight >= 0.7`) edges touching an `in_universe` node."""
    if not new_review_edges:
        return 0
    if ledger_path is None and notify_fn is None and _default_writes_muzzled():
        return 0
    path = Path(ledger_path) if ledger_path is not None else REVIEW_LEDGER_PATH
    try:
        seen: set[str] = set()
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    seen.add(json.loads(line)["key"])
                except (ValueError, KeyError):
                    continue
        fresh = []
        for e in new_review_edges:
            key = f"{e['source_node']}|{e['relation']}|{e['target_node']}"
            if key not in seen:
                fresh.append((key, e))
        if not fresh:
            return 0
        lines = [
            f"🕸️ **GraphRAG review: {len(fresh)} new high-weight edge(s) touching the option universe** "
            "(context-only, zero execution authority):"
        ]
        for _, e in fresh[:8]:
            pol = "+" if e["polarity"] > 0 else ("-" if e["polarity"] < 0 else "0")
            lines.append(
                f"• `{e['source_node']}` —{e['relation']} ({e['edge_class']}, {pol}, w={e['weight']:.2f})→ "
                f"`{e['target_node']}` — *\"{e.get('quote', '')[:120]}\"*"
            )
        if notify_fn is None:
            from src.notifier import fire_broadcast
            notify_fn = lambda text: fire_broadcast({"event": "graphrag_review", "text": text})
        notify_fn("\n".join(lines))
        if not (ledger_path is None and _default_writes_muzzled()):
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                for key, e in fresh:
                    f.write(
                        json.dumps({
                            "key": key,
                            "source_node": e["source_node"],
                            "relation": e["relation"],
                            "target_node": e["target_node"],
                            "weight": e["weight"],
                            "announced_on": date.today().isoformat(),
                        })
                        + "\n"
                    )
        return len(fresh)
    except Exception as exc:
        print(f"  (graphrag_extract: review notify skipped [{exc}])", file=sys.stderr)
        return 0


def _calls_today(call_ledger_path: Path, today_str: str) -> int:
    try:
        if not call_ledger_path.exists():
            return 0
        return sum(1 for ln in call_ledger_path.read_text().splitlines() if f'"{today_str}"' in ln)
    except OSError:
        return 0


def _record_call(call_ledger_path: Path, today_str: str, content_hash: str) -> None:
    if _default_writes_muzzled() and call_ledger_path == CALL_LEDGER_PATH:
        return
    try:
        call_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(call_ledger_path, "a") as f:
            f.write(json.dumps({"day": today_str, "content_hash": content_hash}) + "\n")
    except OSError:
        pass


def extract_from_item(
    item: dict | str,
    conn=None,
    db_path: Path | str | None = None,
    extractor=None,
    source_ref: str | None = None,
    extracted_at: str | None = None,
    notify_fn=None,
    review_ledger_path: Path | str | None = None,
    call_ledger_path: Path | str | None = None,
    daily_cap: int = DEFAULT_DAILY_EXTRACT_CAP,
    dry_run: bool = False,
    env: dict | None = None,
    hostname: str | None = None,
) -> dict:
    """Extract and persist GraphRAG edges from one event/news item.

    When `dry_run=True`, runs LLM extraction + strict validation against a seeded
    in-memory copy (or without committing edge writes / Discord alerts) so the
    1-week Mini PC dry-run (ARCHITECTURE_ROADMAP.md §2.6 step 6) can audit
    accepted vs rejected edges safely.
    Fail-open: never raises."""
    summary = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "written": 0,
        "duplicates": 0,
        "rejected": 0,
        "rejected_reasons": {},
        "review_alerts": 0,
        "edges": [],
    }
    try:
        refusal = node_role.llm_jobs_refusal(env=env, hostname=hostname)
        if refusal is not None:
            summary["status"] = f"skipped ({refusal})"
            return summary

        text = compose_input_text(item)
        if not text:
            summary["status"] = "skipped (empty text)"
            return summary

        chash = content_hash_for(text)
        if source_ref is None and isinstance(item, dict):
            source_ref = item.get("event_id") or item.get("url") or item.get("source")

        owns_conn = conn is None
        if owns_conn:
            conn = brain_map.connect(":memory:" if dry_run else db_path)
        try:
            graphrag.ensure_schema(conn)
            node_count = conn.execute("SELECT COUNT(*) AS n FROM graph_nodes").fetchone()["n"]
            if node_count == 0:
                graphrag.seed_nodes(conn, now=extracted_at)
            elif not dry_run and not graphrag.has_structural_edges(conn):
                graphrag.seed_structural_edges(conn, now=extracted_at)

            if not dry_run and already_extracted(conn, chash):
                summary["status"] = "skipped (duplicate content_hash)"
                return summary

            c_ledger = Path(call_ledger_path) if call_ledger_path is not None else CALL_LEDGER_PATH
            today_str = (str(extracted_at)[:10] if extracted_at else date.today().isoformat())
            if daily_cap is not None and _calls_today(c_ledger, today_str) >= int(daily_cap):
                summary["status"] = f"skipped (daily extraction cap {daily_cap} reached)"
                return summary

            raw_resp, model_tag = _ask_llm(extractor, text)
            if not dry_run:
                _record_call(c_ledger, today_str, chash)
            if not isinstance(raw_resp, dict) or not isinstance(raw_resp.get("edges"), list):
                summary["status"] = "skipped (llm unavailable or invalid json)"
                return summary

            review_candidates = []
            for raw_edge in raw_resp["edges"]:
                ok, reason, norm = validate_raw_edge(conn, raw_edge, text)
                if not ok:
                    summary["rejected"] += 1
                    summary["rejected_reasons"][reason] = summary["rejected_reasons"].get(reason, 0) + 1
                    continue
                if dry_run:
                    summary["written"] += 1
                    summary["edges"].append({
                        "source_node": norm["source_node"]["node_id"],
                        "relation": norm["relation"],
                        "target_node": norm["target_node"]["node_id"],
                        "edge_class": norm["edge_class"],
                        "polarity": norm["polarity"],
                        "weight": norm["strength"],
                        "quote": norm["quote"],
                        "touches_universe": bool(
                            norm["source_node"].get("in_universe") or norm["target_node"].get("in_universe")
                        ),
                    })
                    continue
                res = graphrag.upsert_graphrag_edge(
                    conn,
                    source_node=norm["source_node"],
                    relation=norm["relation"],
                    target_node=norm["target_node"],
                    edge_class=norm["edge_class"],
                    polarity=norm["polarity"],
                    strength=norm["strength"],
                    content_hash=chash,
                    quote=norm["quote"],
                    source_ref=source_ref,
                    extractor=model_tag,
                    extracted_at=extracted_at,
                )
                if res.get("duplicate_evidence"):
                    summary["duplicates"] += 1
                    continue
                summary["written"] += 1
                edge_record = dict(res, quote=norm["quote"])
                summary["edges"].append(edge_record)
                if (
                    res.get("is_new_edge")
                    and res.get("touches_universe")
                    and float(res.get("weight") or 0.0) >= REVIEW_WEIGHT_THRESHOLD
                ):
                    review_candidates.append(edge_record)

            if review_candidates and not dry_run:
                summary["review_alerts"] = _notify_review_edges(
                    review_candidates,
                    ledger_path=review_ledger_path,
                    notify_fn=notify_fn,
                )
            return summary
        finally:
            if owns_conn and conn is not None:
                conn.close()
    except Exception as exc:
        print(f"  (graphrag_extract: fail-open skip [{type(exc).__name__}: {exc}])", file=sys.stderr)
        summary["status"] = f"error ({type(exc).__name__}: {exc})"
        return summary


def run_stored_items(
    days: int = 7,
    dry_run: bool = True,
    card_dir: Path | str | None = None,
    inbox_dir: Path | str | None = None,
    conn=None,
    extractor=None,
    daily_cap: int = DEFAULT_DAILY_EXTRACT_CAP,
    env: dict | None = None,
    hostname: str | None = None,
    rss_items: list | None = None,
) -> dict:
    """Replay the last `days` days of stored event cards + inbox texts (+ optional
    live RSS items) through `extract_from_item`; default `dry_run=True` (Step 6).
    A dry-run shares ONE in-memory graph across items so node reuse is measurable;
    repeated texts and repeated edges are counted once."""
    from datetime import timedelta
    from src.event_engine import CARD_DIR, INBOX_DIR, detect

    cutoff = date.today() - timedelta(days=max(1, int(days)))
    items: list[dict] = []

    cdir = Path(card_dir) if card_dir is not None else CARD_DIR
    if cdir.is_dir():
        for p in sorted(cdir.glob("*.json")):
            try:
                card = json.loads(p.read_text())
                gen = str(card.get("generated_at") or "")[:10]
                if gen and date.fromisoformat(gen) < cutoff:
                    continue
                src = card.get("source") or {}
                body = str(src.get("text") or "").strip()
                if not body:
                    body = " ".join(
                        str(m.get("quote") or "") for m in (card.get("mechanics") or []) if isinstance(m, dict)
                    ).strip()
                if src.get("title") or body:
                    items.append({
                        "event_id": card.get("event_id") or p.stem,
                        "title": src.get("title") or p.stem,
                        "text": body,
                        "url": src.get("url") or "",
                        "source": src.get("from") or f"card:{p.name}",
                        "full_text": bool(src.get("text")),
                    })
            except Exception:
                continue

    for it in detect.inbox_items(inbox_dir or INBOX_DIR):
        items.append(dict(it, full_text=True))
    for it in rss_items or []:
        items.append(dict(it, full_text=True))

    agg = {
        "days": days,
        "dry_run": bool(dry_run),
        "items_checked": 0,
        "items_full_text": 0,
        "items_duplicate_text": 0,
        "written": 0,
        "duplicate_edges_dropped": 0,
        "rejected": 0,
        "rejected_reasons": {},
        "distinct_nodes": 0,
        "nodes_in_2plus_edges": 0,
        "edges_reaching_universe": 0,
        "universe_reached": {},
        "edges": [],
    }
    owns_conn = conn is None and dry_run
    if owns_conn:
        conn = brain_map.connect(":memory:")
        graphrag.seed_nodes(conn)
    seen_text: set[str] = set()
    seen_edge: set[tuple] = set()
    try:
        for it in items:
            chash = content_hash_for(compose_input_text(it))
            if chash in seen_text:
                agg["items_duplicate_text"] += 1
                continue
            seen_text.add(chash)
            agg["items_checked"] += 1
            agg["items_full_text"] += int(bool(it.get("full_text")))
            r = extract_from_item(
                {k: v for k, v in it.items() if k != "full_text"},
                conn=conn,
                extractor=extractor,
                daily_cap=daily_cap,
                dry_run=dry_run,
                env=env,
                hostname=hostname,
            )
            agg["rejected"] += int(r.get("rejected") or 0)
            for k, v in (r.get("rejected_reasons") or {}).items():
                agg["rejected_reasons"][k] = agg["rejected_reasons"].get(k, 0) + v
            for e in r.get("edges") or []:
                key = (e.get("source_node"), e.get("relation"), e.get("target_node"))
                if key in seen_edge:
                    agg["duplicate_edges_dropped"] += 1
                    continue
                seen_edge.add(key)
                agg["edges"].append(e)
        if conn is not None:
            _measure_universe_reach(conn, agg)
    finally:
        if owns_conn:
            conn.close()
    agg["written"] = len(agg["edges"])
    degree: dict[str, int] = {}
    for e in agg["edges"]:
        for n in (e.get("source_node"), e.get("target_node")):
            degree[n] = degree.get(n, 0) + 1
    agg["distinct_nodes"] = len(degree)
    agg["nodes_in_2plus_edges"] = sum(1 for d in degree.values() if d >= 2)
    return agg


def _measure_universe_reach(conn, agg: dict) -> None:
    """Propagate +1 from each accepted edge's source over (structural edges + accepted
    edges) and record which option-universe nodes it reaches within the hop limit."""
    try:
        import networkx as nx
        from src.knowledge_graph import spreading_activation

        g = nx.DiGraph()
        for r in conn.execute(
            "SELECT source_node, target_node, weight, confidence_score, polarity FROM graph_edges "
            "WHERE source = ? AND invalid_at IS NULL", (graphrag.STRUCTURAL_SOURCE,)
        ):
            g.add_edge(r["source_node"], r["target_node"], weight=r["weight"],
                       confidence_score=r["confidence_score"], polarity=r["polarity"],
                       edge_class="membership")
        for e in agg["edges"]:
            g.add_edge(e["source_node"], e["target_node"], weight=e["weight"],
                       confidence_score=0.5 * float(e["weight"]), polarity=e["polarity"])
        for e in agg["edges"]:
            res = spreading_activation.propagate(g, {e["source_node"]: 1.0}, conn=conn, log_activation=False)
            hits = sorted(a["node_id"] for a in res.get("activated") or [] if a.get("in_universe"))
            e["reaches_universe"] = hits
            if hits:
                agg["edges_reaching_universe"] += 1
            for n in hits:
                agg["universe_reached"][n] = agg["universe_reached"].get(n, 0) + 1
    except Exception as exc:
        print(f"  (graphrag_extract: universe reach skipped [{exc}])", file=sys.stderr)


def main(argv=None) -> int:
    import argparse
    import contextlib
    ap = argparse.ArgumentParser(description="Phase 1 GraphRAG relationship extractor (analytics node only)")
    ap.add_argument("--days", type=int, default=7, help="Lookback days for stored event cards/inbox items")
    ap.add_argument("--apply", action="store_true", help="Persist extracted edges to data/brain_map.db (default is dry-run)")
    ap.add_argument("--rss", action="store_true", help="Also replay today's in-universe RSS items (full feed text)")
    a = ap.parse_args(argv)
    # stdout carries only the JSON result; every progress/log line goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        rss = None
        if a.rss:
            from src.event_engine import detect
            rss = [it for it in detect.rss_items() if detect.in_universe(it)]
        res = run_stored_items(days=a.days, dry_run=not a.apply, rss_items=rss)
    print(json.dumps(res, indent=2, default=str))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

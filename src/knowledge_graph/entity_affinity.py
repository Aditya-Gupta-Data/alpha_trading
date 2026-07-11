"""
src/knowledge_graph/entity_affinity.py — smart-money entity ↔ group learning
============================================================================

Phase 8 learning layer (scratchpad build). The bulk/block-deals tracker
(src/ingestion/deals_tracker.py) accumulates a raw per-deal ledger
(data/deals_history.jsonl). This module turns that history into a memory
of WHICH TRADING ENTITIES concentrate their activity in WHICH promoter
groups — an inferred, public-data-only affinity — and reads their recent
NET DIRECTION as an advisory signal.

The thesis (the user's, formalized): some entities (FII/ODI vehicles,
funds) trade almost exclusively within one promoter group's cluster of
companies. That persistent concentration is a *footprint* of a link that
isn't openly labelled but is visible in the public disclosures. Once an
entity is seen as "linked" to a group, its direction matters: a linked
entity UNLOADING the group it normally holds is distribution at highs
(bearish/caution); loading up is accumulation (bullish).

Honesty rails baked in:
  * PUBLIC DATA ONLY. Every input is a SEBI-mandated bulk/block-deal
    disclosure. "Linked" is a statistical inference from public trading
    concentration, never a claim of actual ownership or inside knowledge.
  * SLOW BY NATURE. Deals are sparse (dozens/day). Concentration is only
    meaningful after weeks/months of history — this layer produces weak
    signal early and is ADVISORY ONLY. It proposes no trades, writes to no
    portfolio/journal, and is not wired into forecast scoring. Whether a
    validated "unloading → short" ever becomes a real position is a
    separate, deferred decision.
  * VALIDATE, DON'T ASSUME. The distribution→drawdown hypothesis is
    probabilistic; the advisories here are the raw material a later
    validation pass (Brain Map events↔outcomes) can score before anything
    is trusted.

Storage (all in the shared data/brain_map.db, additive — the codebase
rule): a dedicated `entity_affinity` accumulation table (all-time per
entity-group buy/sell/count) plus a projected, DECAYING affinity edge in
`graph_edges` (source=entity, relation="concentrates_in", target=group)
so GraphEngine/resonance can consume it and old links fade when an entity
stops concentrating. Structural concentration is all-time; the direction
SIGNAL is computed over a recent window so "unloading" means unloading now.

Runs inside the Sleep Phase as a no-LLM task (VM-safe, like decay), and
stand-alone:  python3 -m src.knowledge_graph.entity_affinity
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import brain_map
from src import graph_engine
from src.ingestion import deals_tracker

ROOT = Path(__file__).resolve().parent.parent.parent
GROUPS_PATH = ROOT / "config" / "entity_groups.json"
AFFINITY_PATH = ROOT / "data" / "entity_affinity.json"
ADVISORY_LOG_PATH = ROOT / "logs" / "affinity_advisories.jsonl"

UNGROUPED = "UNGROUPED"

# --------------------------------------------------- system parameters

# An entity counts as "linked" to a group only with enough evidence: at
# least this many disclosed deals in the group AND at least this share of
# ALL its deals landing there. Deliberately strict — a spurious link that
# fires a bearish advisory is worse than a missed one.
MIN_GROUP_DEALS = 3
MIN_CONCENTRATION = 0.60

# The direction signal is recent, not all-time: "unloading" must mean
# unloading lately. Net below this fraction of gross flow is called "mixed"
# (two-way churn, no clear side) rather than forced onto a direction.
RECENCY_WINDOW_DAYS = 45
NET_DEAD_ZONE = 0.20

DIRECTIONS = ("accumulating", "distributing", "mixed", "flat")


# ------------------------------------------------------- canonicalization

# Everything from the first account marker onward is an account id, not the
# entity: "SBI MUTUAL FUND A/C SBI BLUECHIP" -> "SBI MUTUAL FUND".
_ACCOUNT_MARKER = re.compile(r"\bA\s*/?\s*C\b|\bACCOUNT\b|\bA/C\b|-\s*ODI\b",
                             re.IGNORECASE)
# Trailing generic legal/entity tokens that add no identity once the name
# is otherwise normalized (kept conservative — "FUND"/"CAPITAL" are
# identity-bearing and NOT stripped).
_LEGAL_TAIL = re.compile(
    r"\b(LIMITED|LTD|PRIVATE|PVT|LLP|INC|CORP|CORPORATION|CO)\b\.?", re.IGNORECASE)


def canonicalize_client(name, aliases: dict = None) -> str | None:
    """A disclosed client name -> a stable canonical entity key, or None for
    an empty/garbage name. NSE spells one fund many ways (account suffixes,
    punctuation, legal tails); this collapses the common variance so the
    same entity accumulates under one key. An explicit alias (exact match
    after normalization) always wins. Rule-based and conservative — never
    raises, never guesses a merge it can't defend."""
    if name is None:
        return None
    s = str(name).upper().strip()
    if not s:
        return None
    # Cut off account-identifier tails.
    s = _ACCOUNT_MARKER.split(s)[0]
    # Punctuation (keep & and spaces) -> space; drop long digit runs (acct #s).
    s = re.sub(r"[^A-Z0-9& ]", " ", s)
    s = re.sub(r"\b\d{3,}\b", " ", s)
    # Strip trailing legal tokens, then collapse whitespace.
    s = _LEGAL_TAIL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    aliases = aliases or {}
    return aliases.get(s, s)


# ------------------------------------------------------------- config

def load_entity_groups(path=None) -> dict:
    """config/entity_groups.json -> {"ticker_to_group": {TICKER: GROUP},
    "groups": {GROUP: [tickers]}, "client_aliases": {variant: canonical}}.
    A missing/broken file degrades to empty maps — accumulation still runs,
    every ticker just falls into UNGROUPED. Never raises."""
    path = Path(path) if path is not None else GROUPS_PATH
    empty = {"ticker_to_group": {}, "groups": {}, "client_aliases": {}}
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        print(f"  (entity affinity: unreadable groups file {path} — "
              "everything falls into UNGROUPED)")
        return empty
    if not isinstance(raw, dict):
        return empty
    groups = raw.get("groups") or {}
    ticker_to_group = {}
    clean_groups = {}
    if isinstance(groups, dict):
        for grp, tickers in groups.items():
            if not isinstance(tickers, list):
                continue
            key = str(grp).strip().upper()
            members = [str(t).strip().upper() for t in tickers if str(t).strip()]
            if not key or not members:
                continue
            clean_groups[key] = members
            for ticker in members:
                ticker_to_group[ticker] = key
    aliases_raw = raw.get("client_aliases") or {}
    client_aliases = ({str(k).strip().upper(): str(v).strip().upper()
                       for k, v in aliases_raw.items()
                       if str(k).strip() and str(v).strip()}
                      if isinstance(aliases_raw, dict) else {})
    return {"ticker_to_group": ticker_to_group, "groups": clean_groups,
            "client_aliases": client_aliases}


def group_for_ticker(ticker, ticker_to_group: dict) -> str:
    """A ".NS" ticker -> its promoter group, or UNGROUPED. Never raises."""
    if not ticker:
        return UNGROUPED
    return ticker_to_group.get(str(ticker).strip().upper(), UNGROUPED)


# ------------------------------------------------------------- schema

def ensure_schema(conn) -> None:
    """Create the additive entity-affinity tables in brain_map.db if absent.
    `grp` (not `group`, a SQL keyword) holds the promoter group. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_affinity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT NOT NULL,
            grp TEXT NOT NULL,
            buy_qty INTEGER NOT NULL DEFAULT 0,
            sell_qty INTEGER NOT NULL DEFAULT 0,
            buy_value_rs REAL NOT NULL DEFAULT 0,
            sell_value_rs REAL NOT NULL DEFAULT 0,
            deal_count INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            UNIQUE (client, grp)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_affinity_ingested (
            as_of TEXT PRIMARY KEY,
            folded_at TEXT,
            rows INTEGER
        )
    """)
    conn.commit()


# ------------------------------------------------------------- accumulate

def _fold_row(conn, client: str, grp: str, side: str, qty: int,
              value: float, as_of: str) -> None:
    """Upsert one deal's contribution into the all-time entity_affinity row."""
    buy_q = qty if side == "buy" else 0
    sell_q = qty if side == "sell" else 0
    buy_v = value if (side == "buy" and value) else 0.0
    sell_v = value if (side == "sell" and value) else 0.0
    conn.execute("""
        INSERT INTO entity_affinity
            (client, grp, buy_qty, sell_qty, buy_value_rs, sell_value_rs,
             deal_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT (client, grp) DO UPDATE SET
            buy_qty = buy_qty + excluded.buy_qty,
            sell_qty = sell_qty + excluded.sell_qty,
            buy_value_rs = buy_value_rs + excluded.buy_value_rs,
            sell_value_rs = sell_value_rs + excluded.sell_value_rs,
            deal_count = deal_count + 1,
            last_seen = excluded.last_seen
    """, (client, grp, buy_q, sell_q, buy_v, sell_v, as_of, as_of))


def _client_concentration(conn, client: str) -> tuple:
    """(top_named_group, concentration, group_deal_count) for a client, where
    concentration = the group's share of ALL the client's deals (UNGROUPED
    included in the denominator, so a mostly-ungrouped trader shows low
    concentration in any group). Returns (None, 0.0, 0) if it has no named
    activity."""
    rows = conn.execute(
        "SELECT grp, deal_count FROM entity_affinity WHERE client = ?",
        (client,)).fetchall()
    total = sum(r["deal_count"] for r in rows)
    named = [(r["grp"], r["deal_count"]) for r in rows if r["grp"] != UNGROUPED]
    if not total or not named:
        return None, 0.0, 0
    top_group, top_count = max(named, key=lambda gc: gc[1])
    return top_group, round(top_count / total, 3), top_count


def accumulate_entity_affinity(conn, history: list = None, groups: dict = None,
                               today: date = None, project_edges: bool = True) -> dict:
    """Fold any not-yet-ingested days of the raw deal history into the
    all-time entity_affinity table, then PROJECT a decaying affinity edge
    into graph_edges for each (entity, group) pair TOUCHED this run whose
    all-time concentration clears the link thresholds.

    Only touched pairs are re-projected, so an entity that stops trading a
    group is NOT reinforced and its edge decays out via decay_engine —
    recency is a property of the graph edge, permanence of the table.

    Idempotent per day via entity_affinity_ingested. Pure DB + arithmetic,
    no LLM, no network — safe on the VM. Never raises to the caller."""
    ensure_schema(conn)
    today = today or date.today()
    if history is None:
        history = deals_tracker.read_deal_history()
    groups = groups or load_entity_groups()
    ttg = groups["ticker_to_group"]
    aliases = groups["client_aliases"]

    ingested = {r["as_of"] for r in
                conn.execute("SELECT as_of FROM entity_affinity_ingested")}
    # TIMELOCK (as-of contract, holy-grail plan §5.4): rows dated after
    # `today` are INVISIBLE to this fold — an as-of replay must produce
    # byte-identical state whether or not the ledger already holds later
    # days. Without this, a backfill replayed as-of a past date would
    # leak the future into concentration stats.
    horizon = today.isoformat()
    new_rows = [r for r in history
                if isinstance(r, dict) and r.get("as_of")
                and r["as_of"] <= horizon
                and r["as_of"] not in ingested]
    if not new_rows:
        return {"folded": 0, "new_days": 0, "edges": 0}

    touched, folded, days = {}, 0, set()
    for r in new_rows:
        client = canonicalize_client(r.get("client"), aliases)
        side = r.get("side")
        qty = r.get("qty")
        if not client or side not in ("buy", "sell") or not qty:
            continue
        grp = group_for_ticker(r.get("ticker"), ttg)
        value = r.get("value_rs") or 0.0
        _fold_row(conn, client, grp, side, int(qty), float(value), r["as_of"])
        if grp != UNGROUPED:
            # Remember the LATEST deal date per touched pair: the honest
            # decay anchor for the projected edge (backfill seam — a 2023
            # link must age from 2023, not read as born-today).
            prev = touched.get((client, grp))
            if prev is None or r["as_of"] > prev:
                touched[(client, grp)] = r["as_of"]
        folded += 1
        days.add(r["as_of"])

    now = datetime.now(timezone.utc).isoformat()
    for as_of in sorted(days):
        conn.execute("INSERT OR IGNORE INTO entity_affinity_ingested "
                     "(as_of, folded_at, rows) VALUES (?, ?, ?)",
                     (as_of, now, folded))
    conn.commit()

    edges = 0
    if project_edges:
        for (client, grp), last_seen in sorted(touched.items()):
            top_group, concentration, group_deals = _client_concentration(conn, client)
            if (top_group == grp and group_deals >= MIN_GROUP_DEALS
                    and concentration >= MIN_CONCENTRATION):
                graph_engine.add_edge(
                    conn, client, "concentrates_in", grp,
                    confidence_score=concentration,
                    context=f"{group_deals} deals; {int(concentration*100)}% concentration",
                    valid_from=last_seen,
                    source="affinity_projected")
                edges += 1
        conn.commit()

    return {"folded": folded, "new_days": len(days), "edges": edges}


# ------------------------------------------------------- read-model + signal

def _classify_direction(buy: float, sell: float) -> str:
    """Net buy/sell flow -> a direction word, with a dead-zone that calls
    two-way churn "mixed" rather than forcing a side."""
    gross = buy + sell
    if gross <= 0:
        return "flat"
    net = buy - sell
    if abs(net) < NET_DEAD_ZONE * gross:
        return "mixed"
    return "accumulating" if net > 0 else "distributing"


def _recent_flows(history: list, aliases: dict, ttg: dict,
                  cutoff: str, horizon: str = "9999-12-31") -> dict:
    """Per (client, group) recent buy/sell value+qty from deals on/after
    cutoff AND on/before horizon (ISO dates; lexical compare is valid for
    YYYY-MM-DD). The horizon is the timelock upper bound — an as-of
    readmodel must not see deals dated after its own day. Value-based
    where prices exist, qty as the fallback basis."""
    flows = {}
    for r in history:
        if not isinstance(r, dict):
            continue
        day = r.get("as_of") or ""
        if day < cutoff or day > horizon:
            continue
        client = canonicalize_client(r.get("client"), aliases)
        side, qty = r.get("side"), r.get("qty")
        if not client or side not in ("buy", "sell") or not qty:
            continue
        grp = group_for_ticker(r.get("ticker"), ttg)
        if grp == UNGROUPED:
            continue
        f = flows.setdefault((client, grp), {"buy_v": 0.0, "sell_v": 0.0,
                                             "buy_q": 0, "sell_q": 0})
        value = float(r.get("value_rs") or 0.0)
        f["buy_v" if side == "buy" else "sell_v"] += value
        f["buy_q" if side == "buy" else "sell_q"] += int(qty)
    return flows


def build_affinity_readmodel(conn, groups: dict = None, history: list = None,
                             today: date = None,
                             window_days: int = RECENCY_WINDOW_DAYS) -> dict:
    """Per-group view: which entities are structurally linked (all-time
    concentration) and which way they've traded that group RECENTLY (the
    window). `net_bias` rolls the linked entities' recent net flow into one
    group verdict. Pure read — no writes. Never raises."""
    ensure_schema(conn)
    today = today or date.today()
    groups = groups or load_entity_groups()
    if history is None:
        history = deals_tracker.read_deal_history()
    ttg = groups["ticker_to_group"]
    aliases = groups["client_aliases"]
    cutoff = (today - timedelta(days=window_days)).isoformat()
    recent = _recent_flows(history, aliases, ttg, cutoff,
                           horizon=today.isoformat())

    # Every client with any named activity, and its top link.
    clients = [r["client"] for r in
               conn.execute("SELECT DISTINCT client FROM entity_affinity")]
    out_groups = {}
    for client in clients:
        top_group, concentration, group_deals = _client_concentration(conn, client)
        if (top_group is None or group_deals < MIN_GROUP_DEALS
                or concentration < MIN_CONCENTRATION):
            continue
        f = recent.get((client, top_group))
        if f and (f["buy_v"] + f["sell_v"]) > 0:
            direction = _classify_direction(f["buy_v"], f["sell_v"])
            net_value = round(f["buy_v"] - f["sell_v"], 2)
            net_qty = f["buy_q"] - f["sell_q"]
        elif f:  # recent deals but no price data — fall back to qty
            direction = _classify_direction(f["buy_q"], f["sell_q"])
            net_value, net_qty = 0.0, f["buy_q"] - f["sell_q"]
        else:
            direction, net_value, net_qty = "flat", 0.0, 0
        entry = {"client": client, "concentration": concentration,
                 "group_deals": group_deals, "recent_direction": direction,
                 "recent_net_value_rs": net_value, "recent_net_qty": net_qty}
        out_groups.setdefault(top_group, []).append(entry)

    result_groups = {}
    for grp, entities in out_groups.items():
        entities.sort(key=lambda e: e["concentration"], reverse=True)
        buy = sum(max(e["recent_net_value_rs"], 0) for e in entities)
        sell = sum(-min(e["recent_net_value_rs"], 0) for e in entities)
        active = any(e["recent_direction"] in ("accumulating", "distributing")
                     for e in entities)
        direction = _classify_direction(buy, sell) if active else "flat"
        net_bias = {"accumulating": "accumulation",
                    "distributing": "distribution"}.get(direction, "none"
                    if direction == "flat" else "mixed")
        result_groups[grp] = {
            "linked_entities": entities,
            "net_bias": net_bias,
            "tickers": groups["groups"].get(grp, []),
        }
    return {"as_of": today.isoformat(), "window_days": window_days,
            "groups": result_groups}


def evaluate_distribution_signals(readmodel: dict, today: date = None) -> list:
    """Read-model -> advisory payloads for groups whose linked smart-money
    is clearly accumulating or distributing recently. DISTRIBUTION is the
    "linked entity unloading = caution/bearish" case the user asked for;
    ACCUMULATION its mirror. Advisory only — no trade, no score. Groups at
    'mixed'/'none' emit nothing."""
    today = today or date.today()
    verdict_map = {"distribution": "DISTRIBUTION", "accumulation": "ACCUMULATION"}
    advisories = []
    for grp, data in (readmodel.get("groups") or {}).items():
        verdict = verdict_map.get(data.get("net_bias"))
        if not verdict:
            continue
        movers = [e for e in data["linked_entities"]
                  if e["recent_direction"] in ("accumulating", "distributing")]
        if not movers:
            continue
        lean = "bearish / caution" if verdict == "DISTRIBUTION" else "bullish"
        rationale = (
            f"{len(movers)} linked entity(ies) net "
            f"{'selling' if verdict == 'DISTRIBUTION' else 'buying'} the "
            f"{grp} group recently "
            f"(e.g. {movers[0]['client']}, "
            f"{int(movers[0]['concentration']*100)}% concentrated). "
            f"Advisory {lean}; public-disclosure inference, not validated signal.")
        advisories.append({
            "as_of": today.isoformat(),
            "group": grp,
            "verdict": verdict,
            "lean": lean,
            "entities": [e["client"] for e in movers],
            "tickers": data.get("tickers", []),
            "rationale": rationale,
        })
    return advisories


def _default_writes_muzzled() -> bool:
    """True inside a test run (the decision-#43 muzzle rule, applied to
    file artifacts): a test that didn't pass its OWN path must never write
    the real data/logs artifacts — a suite run on the VM would otherwise
    clobber the live read-model with test junk."""
    import os
    return bool(os.environ.get("PYTEST_CURRENT_TEST")
                or os.environ.get("IS_TEST_ENV"))


def log_affinity_advisories(payloads: list, path=None) -> Path | None:
    """Append advisory payloads to logs/affinity_advisories.jsonl (one JSON
    line each) — read by humans/dashboards only, never the execution loop.
    Mirrors resonance.log_advisories. Returns the path (None when muzzled
    under a test without an explicit path)."""
    if path is None and _default_writes_muzzled():
        return None
    path = Path(path) if path is not None else ADVISORY_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for payload in payloads or []:
            f.write(json.dumps(payload) + "\n")
    return path


def write_readmodel(readmodel: dict, path=None) -> None:
    """Persist the per-group affinity read-model to data/entity_affinity.json
    (advisory artifact, like data/bulk_deals.json). Logged, not raised, on
    failure. Muzzled under tests unless the test passes its own path
    (decision-#43 rule — suite runs must never touch live artifacts)."""
    if path is None and _default_writes_muzzled():
        return
    path = Path(path) if path is not None else AFFINITY_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(readmodel, indent=2))
    except OSError as exc:
        print(f"  (entity affinity: could not write {path} [{exc}])")


# ------------------------------------------------------------- orchestrate

def run(conn=None, db_path=None, history_path=None, groups_path=None,
        today: date = None, window_days: int = RECENCY_WINDOW_DAYS,
        emit_advisories: bool = True, readmodel_path=None,
        advisory_path=None) -> dict:
    """Full pass: accumulate new deal-days into the affinity graph, rebuild
    the read-model, and emit advisories. Reuses a caller-supplied `conn`
    (Sleep-Phase shares one and MUST keep it open) or opens its own from
    `db_path`. Returns a summary. Never raises."""
    own = conn is None
    if conn is None:
        conn = brain_map.connect(db_path)
    try:
        today = today or date.today()
        groups = load_entity_groups(groups_path)
        history = deals_tracker.read_deal_history(history_path)
        acc = accumulate_entity_affinity(conn, history, groups, today=today)
        readmodel = build_affinity_readmodel(conn, groups, history, today=today,
                                             window_days=window_days)
        write_readmodel(readmodel, path=readmodel_path)
        advisories = evaluate_distribution_signals(readmodel, today=today)
        if emit_advisories and advisories:
            log_affinity_advisories(advisories, path=advisory_path)
        return {"folded": acc["folded"], "new_days": acc["new_days"],
                "edges": acc["edges"],
                "linked_groups": len(readmodel["groups"]),
                "advisories": len(advisories)}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    # Manual pass: python3 -m src.knowledge_graph.entity_affinity
    summary = run()
    print(json.dumps(summary, indent=2))

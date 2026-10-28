"""
src/knowledge_graph/graphrag.py — Phase 1 Qualitative 3D Knowledge Graph (GraphRAG) schema & node store
=======================================================================================================

Extends the existing SQLite Brain Map (`data/brain_map.db` and `src/graph_engine.py`)
with:
  1. `graph_nodes`          — canonical entity / sector / commodity / technology /
                              theme / policy / macro / person_group catalog, with
                              alias resolution and `in_universe` flagging.
  2. `graph_edges` columns  — additive `edge_class`, `polarity`, `weight`,
                              `evidence_count`, `extractor` via guarded ALTERs.
  3. `graph_edge_evidence`  — per-(edge, content_hash) provenance & verbatim quote.
  4. `graph_activation_log` — append-only record of every 3D spreading-activation run.

Standing constraints (ARCHITECTURE_ROADMAP.md §1–§2):
  * Zero execution authority in Phase 1. Edges written by the GraphRAG extractor
    carry `source = 'llm_extracted'` and are blocked by `vol_bridge`'s provenance
    firewall (`source = 'outcome_derived'` only).
  * Strictly additive and idempotent: existing `graph_edges` rows are never
    rewritten or invalidated by schema setup or node seeding.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from src import brain_map
from src import graph_engine

ROOT = Path(__file__).resolve().parent.parent.parent
SECTOR_UNIVERSE_PATH = ROOT / "config" / "sector_universe.json"
ENTITY_GROUPS_PATH = ROOT / "config" / "entity_groups.json"

NODE_TYPES: tuple[str, ...] = (
    "company",
    "sector",
    "commodity",
    "technology",
    "theme",
    "policy",
    "macro",
    "person_group",
)

NON_COMPANY_NODE_TYPES: frozenset[str] = frozenset(t for t in NODE_TYPES if t != "company")

EDGE_CLASSES: tuple[str, ...] = (
    "supply",
    "demand",
    "substitute",
    "competes",
    "partnership",
    "regulates",
    "owns",
    "macro_sensitivity",
    "causal",
    "membership",
)

# Closed predicate vocabulary -> edge_class (ARCHITECTURE_ROADMAP.md §2.3 + §2.7 owner ruling)
PREDICATE_TO_CLASS: dict[str, str] = {
    "makes": "supply",
    "sells": "supply",
    "supplies_to": "supply",
    "exports_to": "supply",
    "needs": "demand",
    "consumes": "demand",
    "sources_from": "demand",
    "imports_from": "demand",
    "substitutes": "substitute",
    "competes_with": "competes",
    "partner_with": "partnership",
    "partners_with": "partnership",
    "joint_venture_with": "partnership",
    "regulates": "regulates",
    "subsidises": "regulates",
    "taxes": "regulates",
    "tariffs": "regulates",
    "bans": "regulates",
    "owns": "owns",
    "subsidiary_of": "owns",
    "invests_in": "owns",
    "sensitive_to": "macro_sensitivity",
    "drives": "causal",
    "hurts": "causal",
    "benefits_from": "causal",
}

LLM_EXTRACTED_SOURCE = "llm_extracted"
DEFAULT_LLM_DECAY_LAMBDA = 0.02

# Built-in company display/common aliases beyond event_engine.detect.STOCK_ALIASES
# so multi-word Indian corporate names in news text resolve deterministically to
# their canonical NSE ticker node_id.
COMMON_COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "RELIANCE": ("reliance", "ril", "reliance industries"),
    "HDFCBANK": ("hdfc bank", "hdfcbank"),
    "ICICIBANK": ("icici bank", "icicibank"),
    "INFY": ("infosys", "infy"),
    "TCS": ("tcs", "tata consultancy", "tata consultancy services"),
    "TATAMOTORS": ("tata motors", "tatamotors", "tmpv", "tmcv"),
    "TATASTEEL": ("tata steel", "tatasteel"),
    "TATAPOWER": ("tata power", "tatapower"),
    "TATACHEM": ("tata chemicals", "tatachem"),
    "TATACONSUM": ("tata consumer", "tata consumer products", "tataconsum"),
    "MARUTI": ("maruti", "maruti suzuki"),
    "M&M": ("mahindra & mahindra", "mahindra and mahindra", "m&m"),
    "BAJAJ-AUTO": ("bajaj auto", "bajaj-auto"),
    "BAJFINANCE": ("bajaj finance", "bajfinance"),
    "BAJAJFINSV": ("bajaj finserv", "bajajfinsv"),
    "SBIN": ("state bank of india", "sbi", "sbin"),
    "KOTAKBANK": ("kotak mahindra bank", "kotak bank", "kotakbank"),
    "AXISBANK": ("axis bank", "axisbank"),
    "INDUSINDBK": ("indusind bank", "indusindbk"),
    "LT": ("larsen & toubro", "larsen and toubro", "l&t"),
    "HINDUNILVR": ("hindustan unilever", "hul", "hindunilvr"),
    "BHARTIARTL": ("bharti airtel", "airtel", "bhartiartl"),
    "SUNPHARMA": ("sun pharma", "sun pharmaceutical", "sunpharma"),
    "DRREDDY": ("dr reddy", "dr reddys", "dr. reddy's", "drreddy"),
    "HCLTECH": ("hcl tech", "hcl technologies", "hcltech"),
    "WIPRO": ("wipro",),
    "TECHM": ("tech mahindra", "techm"),
    "ULTRACEMCO": ("ultratech cement", "ultratech", "ultracemco"),
    "JSWSTEEL": ("jsw steel", "jswsteel"),
    "HINDALCO": ("hindalco", "hindalco industries"),
    "COALINDIA": ("coal india", "coalindia"),
    "ONGC": ("ongc", "oil and natural gas corporation"),
    "NTPC": ("ntpc",),
    "POWERGRID": ("power grid", "powergrid"),
    "ADANIENT": ("adani enterprises", "adanient"),
    "ADANIPORTS": ("adani ports", "adaniports"),
    "ADANIGREEN": ("adani green", "adanigreen"),
    "ADANIPOWER": ("adani power", "adanipower"),
}

_GRAPHRAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id       TEXT PRIMARY KEY,
    node_type     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    aliases_json  TEXT,
    in_universe   INTEGER NOT NULL DEFAULT 0,
    sector        TEXT,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes (node_type);

CREATE TABLE IF NOT EXISTS graph_edge_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node   TEXT NOT NULL,
    relation      TEXT NOT NULL,
    target_node   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    source_ref    TEXT,
    quote         TEXT,
    extracted_at  TEXT NOT NULL,
    UNIQUE (source_node, relation, target_node, content_hash)
);

CREATE TABLE IF NOT EXISTS graph_activation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT NOT NULL,
    trigger_ref  TEXT,
    seeds_json   TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    result_json  TEXT NOT NULL
);
"""


def _now_iso(now: str | None = None) -> str:
    return str(now) if now else datetime.now(timezone.utc).isoformat()


def ensure_schema(conn) -> None:
    """Create GraphRAG tables (`graph_nodes`, `graph_edge_evidence`,
    `graph_activation_log`) and add GraphRAG columns to `graph_edges` via
    guarded ALTERs. Idempotent and safe on an already-populated DB."""
    graph_engine.ensure_schema(conn)
    conn.executescript(_GRAPHRAG_SCHEMA)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(graph_edges)")}
    if "edge_class" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN edge_class TEXT")
    if "polarity" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN polarity INTEGER")
    if "weight" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN weight REAL")
    if "evidence_count" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN evidence_count INTEGER DEFAULT 1")
    if "extractor" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN extractor TEXT")
    conn.commit()


# Canonical institutions/macro entities: every alias collapses onto ONE node so
# "RBI", "Reserve Bank of India" and "the central bank" stop fragmenting the graph.
CANONICAL_ENTITIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "policy:RESERVE_BANK_OF_INDIA": ("policy", "RESERVE BANK OF INDIA", (
        "rbi", "reserve bank of india", "reserve bank", "india s central bank", "indian central bank",
        "rbi governor", "rbi mpc", "monetary policy committee", "mpc")),
    "policy:SEBI": ("policy", "SECURITIES AND EXCHANGE BOARD OF INDIA", (
        "sebi", "securities and exchange board of india", "market regulator", "capital markets regulator")),
    "policy:GOVERNMENT_OF_INDIA": ("policy", "GOVERNMENT OF INDIA", (
        "government", "govt", "gov", "goi", "centre", "the centre", "center", "central government",
        "union government", "government of india", "indian government", "india s government",
        "ministry", "the ministry", "cabinet", "union cabinet", "modi government")),
    "policy:MINISTRY_OF_FINANCE": ("policy", "MINISTRY OF FINANCE", (
        "finance ministry", "ministry of finance", "mof", "finance minister", "fm", "dea")),
    "policy:GST_COUNCIL": ("policy", "GST COUNCIL", ("gst council", "gst panel")),
    "policy:REPO_RATE": ("policy", "REPO RATE", (
        "repo rate", "benchmark repo rate", "policy rate", "benchmark rate", "key policy rate")),
    "macro:US_FEDERAL_RESERVE": ("macro", "US FEDERAL RESERVE", (
        "fed", "the fed", "us fed", "federal reserve", "us federal reserve", "fomc")),
    "macro:PEOPLES_BANK_OF_CHINA": ("macro", "PEOPLE'S BANK OF CHINA", (
        "pboc", "people s bank of china", "peoples bank of china", "china s central bank",
        "chinese central bank")),
    "macro:INDIA": ("macro", "INDIA", ("india", "indian economy")),
    "macro:CHINA": ("macro", "CHINA", ("china", "beijing", "chinese government", "china s government")),
    "macro:USA": ("macro", "UNITED STATES", ("us", "usa", "u s", "united states", "america", "washington",
                                              "us government", "trump administration")),
    "macro:INR": ("macro", "INDIAN RUPEE", ("rupee", "inr", "indian rupee", "currency", "the rupee")),
    "macro:USD": ("macro", "US DOLLAR", ("dollar", "dollars", "usd", "us dollar", "greenback")),
    "macro:INDIA_10Y_BOND_YIELD": ("macro", "INDIA 10Y BOND YIELD", (
        "bond yield", "bond yields", "india s 10-year bond", "10-year bond", "10-year bond yield",
        "india 10-year yield", "g-sec yield", "gsec yield", "government bond yield")),
    "macro:INFLATION": ("macro", "INFLATION", ("inflation", "cpi", "cpi inflation", "retail inflation",
                                               "wpi", "wholesale inflation")),
    "commodity:CRUDE_OIL": ("commodity", "CRUDE OIL", ("crude", "crude oil", "oil", "brent", "wti",
                                                       "brent crude", "oil prices")),
    "commodity:GOLD": ("commodity", "GOLD", ("gold", "gold prices", "bullion")),
    "sector:FINANCIALS": ("sector", "FINANCIALS", (
        "banks", "bank", "banking", "banking sector", "banking system", "indian banks", "lenders",
        "psu banks", "public sector banks", "private banks", "private sector banks", "bfsi", "bfsi sector",
        "financial sector", "financial services", "financials", "nbfcs", "nbfc", "top nbfcs")),
    "sector:IT": ("sector", "IT", (
        "it", "it sector", "it services", "it companies", "information technology", "software services",
        "indian it", "tech services", "it stocks")),
    "sector:ENERGY": ("sector", "ENERGY", (
        "energy", "energy sector", "oil and gas", "oil & gas", "oil and gas sector", "refiners", "refining",
        "oil marketing companies", "omcs", "petrochemicals")),
    "sector:AUTO": ("sector", "AUTO", (
        "auto", "autos", "auto sector", "automobile", "automobiles", "automakers", "carmakers",
        "car makers", "passenger vehicles", "two-wheelers")),
    "sector:PHARMA": ("sector", "PHARMA", (
        "pharma", "pharma sector", "pharmaceuticals", "drugmakers", "drug makers", "drug companies")),
    "sector:FMCG": ("sector", "FMCG", ("fmcg", "fmcg sector", "consumer staples", "consumer goods")),
    "sector:METAL": ("sector", "METAL", ("metal", "metals", "metal sector", "steel", "steelmakers")),
}

# Modifiers stripped before a second canonical lookup ("rising crude oil prices" -> "crude oil").
_LEADING_MODIFIERS = ("rising", "falling", "higher", "lower", "high", "low", "elevated", "soaring",
                      "surging", "easing", "sticky", "global", "domestic", "indian")
_TRAILING_MODIFIERS = ("price hikes", "price hike", "prices", "price", "hikes", "hike", "concerns",
                       "worries", "fears", "pressures", "pressure", "costs", "cost", "rates", "levels",
                       "outlook", "sector", "stocks", "shares")

# "it"/"us" are pronouns unless written IT / US or type-prefixed.
_PRONOUN_ALIASES = frozenset({"it", "us"})

# Endpoint names too generic to mean anything on their own — rejected, never repaired.
GENERIC_ENDPOINTS = frozenset({
    "scheme", "the scheme", "schemes", "sops", "feedback", "policy", "the policy", "plan", "proposal",
    "measures", "move", "step", "steps", "norms", "rules", "regulations", "guidelines", "framework",
    "reforms", "announcement", "decision", "order", "it is", "this", "that", "market", "markets",
    "companies", "company", "firms", "people", "consumers", "investors", "sentiment", "market sentiment",
})


def _build_canon_index() -> dict[str, str]:
    idx = {}
    for node_id, (_t, display, aliases) in CANONICAL_ENTITIES.items():
        for a in (*aliases, display, node_id, node_id.split(":", 1)[1].replace("_", " ")):
            idx[_normalize_alias(a.replace("-", " "))] = node_id
    return idx


def canonical_entity_for(raw_name: str) -> str | None:
    """The canonical node_id for an institution/macro alias (type prefix ignored), else None."""
    raw = str(raw_name or "").strip()
    prefixed = ":" in raw and raw.split(":", 1)[0].lower() in NODE_TYPES
    if prefixed:
        raw = raw.split(":", 1)[1]
    norm = _normalize_alias(raw.replace("_", " ").replace("-", " "))
    if norm in _PRONOUN_ALIASES and not (prefixed or raw.isupper()):
        return None
    hit = _CANON_INDEX.get(norm)
    if hit is not None:
        return hit
    words = norm.split()
    changed = True
    while changed and words:
        changed = False
        if words[0] in _LEADING_MODIFIERS and len(words) > 1:
            words, changed = words[1:], True
        for mod in _TRAILING_MODIFIERS:
            mw = mod.split()
            if len(words) > len(mw) and words[-len(mw):] == mw:
                words, changed = words[:-len(mw)], True
                break
    return _CANON_INDEX.get(" ".join(words))


def is_generic_endpoint(raw_name: str) -> bool:
    """True for names like "scheme" / "policy:FEEDBACK" that carry no entity."""
    raw = str(raw_name or "").strip()
    if ":" in raw and raw.split(":", 1)[0].lower() in NODE_TYPES:
        raw = raw.split(":", 1)[1]
    return _normalize_alias(raw.replace("_", " ")) in GENERIC_ENDPOINTS


def _normalize_alias(text: str) -> str:
    s = re.sub(r"[^a-z0-9&\-]+", " ", (text or "").lower()).strip()
    return re.sub(r"\s+", " ", s)


def _canonical_token(text: str) -> str:
    s = re.sub(r"[^A-Z0-9&_]+", "_", (text or "").upper().strip())
    return re.sub(r"_+", "_", s).strip("_")


_CANON_INDEX: dict[str, str] = _build_canon_index()


def canonical_node_id(raw_name: str, node_type: str) -> str:
    """Construct a canonical node_id for a given raw name and node_type."""
    nt = (node_type or "theme").strip().lower()
    raw = (raw_name or "").strip()
    if ":" in raw:
        prefix, rest = raw.split(":", 1)
        if prefix.lower() in NON_COMPANY_NODE_TYPES and rest.strip():
            return f"{prefix.lower()}:{_canonical_token(rest)}"
    if nt == "company":
        base = raw.upper().removesuffix(".NS").strip()
        return base
    return f"{nt}:{_canonical_token(raw)}"


def upsert_node(
    conn,
    node_id: str,
    node_type: str,
    display_name: str | None = None,
    aliases: list[str] | tuple[str, ...] | None = None,
    in_universe: int | bool = 0,
    sector: str | None = None,
    now: str | None = None,
    increment_mention: bool = False,
) -> None:
    """Insert or update a `graph_nodes` row, merging aliases and preserving
    `in_universe=1` or existing `sector` when already set."""
    ts = _now_iso(now)
    disp = (display_name or node_id).strip()
    norm_aliases = sorted({
        _normalize_alias(a)
        for a in (list(aliases or []) + [node_id, disp, node_id.removesuffix(".NS")])
        if _normalize_alias(a)
    })
    row = conn.execute(
        "SELECT aliases_json, in_universe, sector, mention_count FROM graph_nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO graph_nodes (node_id, node_type, display_name, aliases_json, "
            "in_universe, sector, created_at, last_seen_at, mention_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                node_type,
                disp,
                json.dumps(norm_aliases),
                1 if in_universe else 0,
                sector,
                ts,
                ts,
                1 if increment_mention else 0,
            ),
        )
    else:
        existing_aliases = []
        try:
            existing_aliases = json.loads(row["aliases_json"] or "[]")
        except (ValueError, TypeError):
            existing_aliases = []
        merged = sorted(set(existing_aliases) | set(norm_aliases))
        new_univ = 1 if (in_universe or row["in_universe"]) else 0
        new_sector = sector or row["sector"]
        new_mentions = int(row["mention_count"] or 0) + (1 if increment_mention else 0)
        conn.execute(
            "UPDATE graph_nodes SET aliases_json = ?, in_universe = ?, sector = ?, "
            "last_seen_at = ?, mention_count = ? WHERE node_id = ?",
            (json.dumps(merged), new_univ, new_sector, ts, new_mentions, node_id),
        )


def ensure_nodes_from_edges(conn, now: str | None = None) -> int:
    """Back-fill `graph_nodes` from distinct `source_node` / `target_node`
    endpoints already present in `graph_edges`, without modifying `graph_edges`.
    Returns the number of newly created node rows."""
    ensure_schema(conn)
    ts = _now_iso(now)
    existing = {
        r["node_id"] for r in conn.execute("SELECT node_id FROM graph_nodes").fetchall()
    }
    endpoints: set[str] = set()
    for r in conn.execute("SELECT source_node, target_node FROM graph_edges").fetchall():
        if r["source_node"]:
            endpoints.add(r["source_node"])
        if r["target_node"]:
            endpoints.add(r["target_node"])
    added = 0
    for ep in sorted(endpoints - existing):
        if ":" in ep and ep.split(":", 1)[0].lower() in NON_COMPANY_NODE_TYPES:
            ntype = ep.split(":", 1)[0].lower()
        else:
            ntype = "theme"
        upsert_node(conn, node_id=ep, node_type=ntype, display_name=ep, now=ts)
        added += 1
    conn.commit()
    return added


STRUCTURAL_SOURCE = "structural_seed"
STRUCTURAL_WEIGHT = 0.8
# Traded indices a sector feeds beyond its own `nse_index` (config/sector_universe.json).
SECTOR_EXTRA_INDICES = {"FINANCIALS": ("NIFTY FIN SERVICE",)}


def has_structural_edges(conn) -> bool:
    ensure_schema(conn)
    return conn.execute("SELECT 1 FROM graph_edges WHERE source = ? LIMIT 1",
                        (STRUCTURAL_SOURCE,)).fetchone() is not None


def seed_structural_edges(conn, sector_universe_path: Path | str | None = None, now: str | None = None) -> int:
    """sector -contains-> stock and sector -feeds-> traded index edges, so energy that
    lands on a sector can reach the option universe. Decay-exempt, +1, source
    `structural_seed` (context only — never `outcome_derived`). Idempotent."""
    from src import graph_engine
    from src.options_proposer import LOT_SIZES

    ensure_schema(conn)
    path = Path(sector_universe_path) if sector_universe_path else SECTOR_UNIVERSE_PATH
    try:
        sectors = json.loads(path.read_text()).get("sectors", {})
    except (OSError, ValueError):
        return 0
    indices = {i.upper() for i in LOT_SIZES}
    pairs = []
    for name, meta in sorted(sectors.items()):
        if not isinstance(meta, dict):
            continue
        sec = f"sector:{name.strip().upper()}"
        for t in meta.get("constituents") or []:
            base = str(t).strip().upper().removesuffix(".NS")
            if base:
                pairs.append((sec, "contains", base))
        targets = [str(meta.get("nse_index") or "").upper(), *SECTOR_EXTRA_INDICES.get(name.strip().upper(), ())]
        for idx in targets:
            if idx in indices:
                pairs.append((sec, "feeds", idx))
    for src, rel, tgt in pairs:
        graph_engine.add_edge(conn, src, rel, tgt, confidence_score=1.0, decay_lambda=0.0,
                              valid_from=now, source=STRUCTURAL_SOURCE)
        conn.execute(
            "UPDATE graph_edges SET edge_class = 'membership', polarity = 1, weight = ?, "
            "evidence_count = 1, extractor = 'seed' WHERE source_node = ? AND relation = ? AND target_node = ?",
            (STRUCTURAL_WEIGHT, src, rel, tgt))
    conn.commit()
    return len(pairs)


def seed_nodes(
    conn,
    sector_universe_path: Path | str | None = None,
    entity_groups_path: Path | str | None = None,
    now: str | None = None,
) -> dict:
    """Seed `graph_nodes` from:
      1. The VM option universe (`options_proposer.LOT_SIZES` + `EQUITY_OPTION_UNDERLYINGS`)
         and `event_engine.detect.STOCK_ALIASES` (`in_universe=1`).
      2. `config/sector_universe.json` (sectors + constituents with home sector).
      3. `config/entity_groups.json` (promoter groups + group member tickers).
      4. `ensure_nodes_from_edges()` for any existing `graph_edges` endpoints.
    Idempotent."""
    ensure_schema(conn)
    ts = _now_iso(now)
    from src.event_engine.detect import STOCK_ALIASES
    from src.options_proposer import EQUITY_OPTION_UNDERLYINGS, LOT_SIZES

    universe_equities = {sym.removesuffix(".NS").upper() for sym in EQUITY_OPTION_UNDERLYINGS}
    universe_indices = {idx.upper() for idx in LOT_SIZES}

    count_before = conn.execute("SELECT COUNT(*) AS n FROM graph_nodes").fetchone()["n"]

    # 1. Option-universe indices (macro/index nodes, in_universe=1)
    for idx_name in sorted(universe_indices):
        upsert_node(
            conn,
            node_id=idx_name,
            node_type="macro",
            display_name=idx_name,
            aliases=[idx_name.lower()],
            in_universe=1,
            now=ts,
        )

    # 2. Option-universe equities (`in_universe=1`)
    for base in sorted(universe_equities):
        aliases = list(STOCK_ALIASES.get(base, ())) + list(COMMON_COMPANY_ALIASES.get(base, ())) + [f"{base}.ns"]
        upsert_node(
            conn,
            node_id=base,
            node_type="company",
            display_name=base,
            aliases=aliases,
            in_universe=1,
            now=ts,
        )

    # 3. Sector universe (`config/sector_universe.json`)
    sec_path = Path(sector_universe_path) if sector_universe_path else SECTOR_UNIVERSE_PATH
    sectors_seeded = 0
    if sec_path.exists():
        try:
            sec_data = json.loads(sec_path.read_text()).get("sectors", {})
        except (OSError, ValueError):
            sec_data = {}
        for sec_name, meta in sorted(sec_data.items()):
            sec_key = sec_name.strip().upper()
            sec_node_id = f"sector:{sec_key}"
            sec_aliases = [sec_key.lower(), f"sector:{sec_key.lower()}"]
            if isinstance(meta, dict) and meta.get("nse_index"):
                sec_aliases.append(str(meta["nse_index"]).lower())
            upsert_node(
                conn,
                node_id=sec_node_id,
                node_type="sector",
                display_name=sec_key,
                aliases=sec_aliases,
                in_universe=0,
                sector=sec_key,
                now=ts,
            )
            sectors_seeded += 1
            for raw_ticker in (meta.get("constituents") or []) if isinstance(meta, dict) else []:
                base = str(raw_ticker).strip().upper().removesuffix(".NS")
                if not base:
                    continue
                aliases = list(STOCK_ALIASES.get(base, ())) + list(COMMON_COMPANY_ALIASES.get(base, ())) + [
                    str(raw_ticker).lower(),
                    base.lower(),
                ]
                upsert_node(
                    conn,
                    node_id=base,
                    node_type="company",
                    display_name=base,
                    aliases=aliases,
                    in_universe=1 if base in universe_equities else 0,
                    sector=sec_key,
                    now=ts,
                )

    # 4. Promoter/entity groups (`config/entity_groups.json`)
    grp_path = Path(entity_groups_path) if entity_groups_path else ENTITY_GROUPS_PATH
    groups_seeded = 0
    if grp_path.exists():
        try:
            grp_data = json.loads(grp_path.read_text()).get("groups", {})
        except (OSError, ValueError):
            grp_data = {}
        for grp_name, members in sorted(grp_data.items()) if isinstance(grp_data, dict) else []:
            grp_key = str(grp_name).strip().upper()
            if not grp_key:
                continue
            grp_node_id = f"person_group:{grp_key}"
            upsert_node(
                conn,
                node_id=grp_node_id,
                node_type="person_group",
                display_name=f"{grp_key} GROUP",
                aliases=[f"{grp_key.lower()} group", f"person_group:{grp_key.lower()}"],
                in_universe=0,
                now=ts,
            )
            groups_seeded += 1
            for raw_ticker in members if isinstance(members, list) else []:
                base = str(raw_ticker).strip().upper().removesuffix(".NS")
                if not base:
                    continue
                aliases = list(STOCK_ALIASES.get(base, ())) + list(COMMON_COMPANY_ALIASES.get(base, ())) + [
                    str(raw_ticker).lower(),
                    base.lower(),
                ]
                upsert_node(
                    conn,
                    node_id=base,
                    node_type="company",
                    display_name=base,
                    aliases=aliases,
                    in_universe=1 if base in universe_equities else 0,
                    now=ts,
                )

    # 5. Any remaining common aliases (e.g. TATAMOTORS if sector_universe uses TMPV/TMCV)
    for base, extra_aliases in sorted(COMMON_COMPANY_ALIASES.items()):
        upsert_node(
            conn,
            node_id=base,
            node_type="company",
            display_name=base,
            aliases=list(extra_aliases) + [f"{base.lower()}.ns", base.lower()],
            in_universe=1 if base in universe_equities else 0,
            now=ts,
        )

    structural = seed_structural_edges(conn, sector_universe_path=sec_path, now=ts)
    backfilled = ensure_nodes_from_edges(conn, now=ts)
    conn.commit()
    count_after = conn.execute("SELECT COUNT(*) AS n FROM graph_nodes").fetchone()["n"]
    return {
        "total_nodes": count_after,
        "added_nodes": count_after - count_before,
        "sectors_seeded": sectors_seeded,
        "groups_seeded": groups_seeded,
        "structural_edges": structural,
        "backfilled_from_edges": backfilled,
    }


def resolve_node(conn, raw_name: str, declared_type: str | None = None) -> dict | None:
    """Resolve an endpoint against `graph_nodes` (by node_id, display_name, or
    aliases_json), or accept it as a new non-company node (`in_universe=0`)
    when `declared_type` is in `NON_COMPANY_NODE_TYPES`.

    Unrecognised `company` endpoints return None (rejected by the validator so
    hallucinated company names never enter the graph)."""
    if not raw_name or not str(raw_name).strip():
        return None
    raw = str(raw_name).strip()
    norm = _normalize_alias(raw)
    upper = raw.upper().removesuffix(".NS").strip()
    dtype = (declared_type or "").strip().lower()

    # 0. Canonical alias map first (RBI -> Reserve Bank of India, Govt -> Government of India)
    canon = canonical_entity_for(raw)
    if canon is not None:
        ctype, cdisp, caliases = CANONICAL_ENTITIES[canon]
        row = conn.execute(
            "SELECT node_id, node_type, display_name, in_universe, sector FROM graph_nodes WHERE node_id = ?",
            (canon,),
        ).fetchone()
        if row is None:
            upsert_node(conn, node_id=canon, node_type=ctype, display_name=cdisp,
                        aliases=list(caliases), in_universe=0)
            return {"node_id": canon, "node_type": ctype, "display_name": cdisp,
                    "in_universe": 0, "sector": None}
        return dict(row)

    # 1. Direct node_id match (exact or upper ticker)
    candidates = [raw, upper]
    if dtype in NON_COMPANY_NODE_TYPES:
        candidates.append(canonical_node_id(raw, dtype))
    for cand in candidates:
        row = conn.execute(
            "SELECT node_id, node_type, display_name, in_universe, sector "
            "FROM graph_nodes WHERE node_id = ?",
            (cand,),
        ).fetchone()
        if row is not None:
            return dict(row)

    # 2. Alias / display_name match across graph_nodes
    # Prefer company / in_universe matches first when an alias could collide.
    rows = conn.execute(
        "SELECT node_id, node_type, display_name, aliases_json, in_universe, sector "
        "FROM graph_nodes ORDER BY in_universe DESC, CASE WHEN node_type='company' THEN 0 ELSE 1 END"
    ).fetchall()
    for row in rows:
        if _normalize_alias(row["display_name"]) == norm or _normalize_alias(row["node_id"]) == norm:
            return {
                "node_id": row["node_id"],
                "node_type": row["node_type"],
                "display_name": row["display_name"],
                "in_universe": int(row["in_universe"] or 0),
                "sector": row["sector"],
            }
        try:
            aliases = json.loads(row["aliases_json"] or "[]")
        except (ValueError, TypeError):
            aliases = []
        if norm in aliases:
            return {
                "node_id": row["node_id"],
                "node_type": row["node_type"],
                "display_name": row["display_name"],
                "in_universe": int(row["in_universe"] or 0),
                "sector": row["sector"],
            }

    # 3. Not in alias table: accept ONLY if declared_type is a valid non-company type
    if dtype in NON_COMPANY_NODE_TYPES:
        cid = canonical_node_id(raw, dtype)
        if not cid or cid.endswith(":"):
            return None
        disp = raw.split(":", 1)[-1].strip() if ":" in raw else raw
        return {
            "node_id": cid,
            "node_type": dtype,
            "display_name": disp,
            "in_universe": 0,
            "sector": None,
        }
    return None


def upsert_graphrag_edge(
    conn,
    source_node: dict,
    relation: str,
    target_node: dict,
    edge_class: str,
    polarity: int,
    strength: float,
    content_hash: str,
    quote: str,
    source_ref: str | None = None,
    extractor: str = "llm:auto:fast",
    decay_lambda: float = DEFAULT_LLM_DECAY_LAMBDA,
    extracted_at: str | None = None,
) -> dict:
    """Idempotently write a validated GraphRAG edge + node rows + evidence row.

    Confidence math (ARCHITECTURE_ROADMAP.md §2.3 step 6):
      * First observation:  confidence_score = 0.5 * strength, evidence_count = 1
      * Re-observation (new content_hash):
            confidence_score <- 1 - (1 - c) * (1 - 0.5 * strength)
            evidence_count   += 1
      * Same (source_node, relation, target_node, content_hash) seen again:
            no-op (idempotent replay).
    """
    ensure_schema(conn)
    ts = _now_iso(extracted_at)
    src_id = source_node["node_id"]
    tgt_id = target_node["node_id"]
    strength = max(0.0, min(1.0, float(strength)))
    polarity = int(polarity)

    upsert_node(
        conn,
        node_id=src_id,
        node_type=source_node["node_type"],
        display_name=source_node.get("display_name") or src_id,
        in_universe=source_node.get("in_universe", 0),
        sector=source_node.get("sector"),
        now=ts,
        increment_mention=True,
    )
    upsert_node(
        conn,
        node_id=tgt_id,
        node_type=target_node["node_type"],
        display_name=target_node.get("display_name") or tgt_id,
        in_universe=target_node.get("in_universe", 0),
        sector=target_node.get("sector"),
        now=ts,
        increment_mention=True,
    )

    dup = conn.execute(
        "SELECT id FROM graph_edge_evidence "
        "WHERE source_node = ? AND relation = ? AND target_node = ? AND content_hash = ?",
        (src_id, relation, tgt_id, content_hash),
    ).fetchone()
    if dup is not None:
        existing_edge = conn.execute(
            "SELECT confidence_score, weight, evidence_count FROM graph_edges "
            "WHERE source_node = ? AND relation = ? AND target_node = ?",
            (src_id, relation, tgt_id),
        ).fetchone()
        return {
            "written": False,
            "is_new_edge": False,
            "duplicate_evidence": True,
            "source_node": src_id,
            "relation": relation,
            "target_node": tgt_id,
            "confidence_score": float(existing_edge["confidence_score"]) if existing_edge else round(0.5 * strength, 6),
            "weight": float(existing_edge["weight"]) if existing_edge and existing_edge["weight"] is not None else strength,
            "evidence_count": int(existing_edge["evidence_count"] or 1) if existing_edge else 1,
        }

    existing = conn.execute(
        "SELECT confidence_score, evidence_count FROM graph_edges "
        "WHERE source_node = ? AND relation = ? AND target_node = ?",
        (src_id, relation, tgt_id),
    ).fetchone()
    if existing is None:
        is_new_edge = True
        new_conf = round(0.5 * strength, 6)
        ev_count = 1
    else:
        is_new_edge = False
        old_c = max(0.0, min(1.0, float(existing["confidence_score"] or 0.0)))
        new_conf = round(1.0 - (1.0 - old_c) * (1.0 - 0.5 * strength), 6)
        ev_count = int(existing["evidence_count"] or 1) + 1

    snippet = (quote or "")[:240]
    graph_engine.add_edge(
        conn,
        src_id,
        relation,
        tgt_id,
        confidence_score=new_conf,
        context=snippet,
        decay_lambda=decay_lambda,
        valid_from=ts,
        source=LLM_EXTRACTED_SOURCE,
    )
    conn.execute(
        "UPDATE graph_edges SET edge_class = ?, polarity = ?, weight = ?, "
        "evidence_count = ?, extractor = ?, source = ? "
        "WHERE source_node = ? AND relation = ? AND target_node = ?",
        (edge_class, polarity, strength, ev_count, extractor, LLM_EXTRACTED_SOURCE, src_id, relation, tgt_id),
    )
    conn.execute(
        "INSERT INTO graph_edge_evidence "
        "(source_node, relation, target_node, content_hash, source_ref, quote, extracted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (src_id, relation, tgt_id, content_hash, source_ref, snippet, ts),
    )
    conn.commit()
    return {
        "written": True,
        "is_new_edge": is_new_edge,
        "duplicate_evidence": False,
        "source_node": src_id,
        "relation": relation,
        "target_node": tgt_id,
        "edge_class": edge_class,
        "polarity": polarity,
        "confidence_score": new_conf,
        "weight": strength,
        "evidence_count": ev_count,
        "touches_universe": bool(source_node.get("in_universe") or target_node.get("in_universe")),
    }

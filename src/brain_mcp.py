"""
Brain-Map MCP server — the data product's first door (cycle_hunter Phase D
prototype, owner directive 2026-07-22: "sell the memory; they bring the
mouth").

An MCP (Model Context Protocol) stdio server any MCP client (Claude,
ChatGPT, Cursor) can plug in, exposing this firm's DERIVED data — tier
tables, valuation scores, earnings-quality reads, event→outcome memory,
big-money affinity, F&O liquidity tiers — as queryable tools.

House rules, enforced by construction and by tests:
  * READ-ONLY everywhere: sqlite opens `mode=ro` (this process cannot
    write the brain), JSON artifacts are only ever read.
  * FACTS AND SCORES, never advice: tool names/descriptions carry no
    buy/sell/recommend verbs (the SEBI posture — we sell data access).
    Internal tier labels (e.g. "strong_buy") DO surface in outputs for
    now; PRODUCT TODO before any external user sees this (G2 gate):
    neutralize those labels at this boundary.
  * DERIVED data only — no raw NSE feed re-stream (exchange-licensing
    posture).
  * Zero new dependencies (framework-free doctrine): MCP's stdio
    transport is newline-delimited JSON-RPC 2.0, hand-rolled below.
  * Fail-open per call: a tool error answers `isError`, never kills the
    server; a missing artifact/table answers an honest "unavailable".
  * Fully injectable (`Sources`, `serve(inp, out)`) — offline tests.

Run: `python3 -m src.brain_mcp` (stdio; wire it into a client's MCP
config with this repo as cwd). Paper/simulated figures are stamped as
such in every payload that carries them.
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "alpha-brain-map", "version": "0.1.0"}


class Sources:
    """Read-only handles to the brain + artifact files. Injectable; the
    sqlite connection is lazy and opened `mode=ro` so this server can
    never write the brain, whatever a tool does."""

    def __init__(self, root: Path = None, db_path: Path = None):
        self.root = Path(root or ROOT)
        self.db_path = Path(db_path or (self.root / "data" / "brain_map.db"))
        self._conn = None

    def conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def artifact(self, rel: str):
        """Parsed JSON artifact under data/, or None (honest miss)."""
        p = self.root / "data" / rel
        try:
            return json.loads(p.read_text())
        except Exception:
            return None


# ------------------------------------------------------------ the tools

def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def darling_tier_table(src: Sources, args: dict) -> dict:
    doc = src.artifact("darling_tiers.json")
    if not doc:
        return {"available": False, "why": "tier table artifact missing"}
    return {"available": True, "as_of": doc.get("as_of"),
            "counts": doc.get("counts"), "tiers": doc.get("tiers"),
            "note": "internal lifecycle grades over the tracked cohort; "
                    "descriptive data, not a recommendation"}


def valuation_scores(src: Sources, args: dict) -> dict:
    doc = src.artifact("darlings_valuation.json")
    if not doc:
        return {"available": False, "why": "valuation artifact missing"}
    ticker = (args.get("ticker") or "").upper() or None
    scores = doc.get("scores") or {}
    if ticker:
        return {"available": True, "as_of": doc.get("as_of"),
                "ticker": ticker, "score": scores.get(ticker),
                "vetoed": ticker in (doc.get("vetoed") or {}),
                "veto_reason": (doc.get("vetoed") or {}).get(ticker),
                "scale": "1=deeply undervalued .. 100=richly valued"}
    return {"available": True, "as_of": doc.get("as_of"),
            "universe_n": doc.get("universe_n"), "scores": scores,
            "vetoed": doc.get("vetoed"),
            "scale": "1=deeply undervalued .. 100=richly valued"}


def earnings_quality(src: Sources, args: dict) -> dict:
    ticker = (args.get("ticker") or "").upper()
    if not ticker:
        return {"available": False, "why": "ticker is required"}
    tdir = src.root / "data" / "lake" / "fundamental_reports" / ticker
    years = sorted(tdir.glob("FY*.json"), reverse=True)
    if not years:
        return {"available": False, "ticker": ticker,
                "why": "no analyzed annual report for this ticker"}
    doc = json.loads(years[0].read_text())
    return {"available": True, "ticker": ticker,
            "fiscal_year": doc.get("fiscal_year"),
            "analyzed_on": doc.get("analyzed_on"),
            "conviction_score": doc.get("conviction_score"),
            "sub_scores": doc.get("sub_scores"),
            "red_flags": doc.get("red_flags"),
            "yellow_flags": doc.get("yellow_flags"),
            "hidden_debt_flags": doc.get("hidden_debt_flags")}


def event_history(src: Sources, args: dict) -> dict:
    ticker = (args.get("ticker") or "").upper()
    if not ticker:
        return {"available": False, "why": "ticker is required"}
    limit = min(int(args.get("limit") or 20), 100)
    events = _rows(src.conn().execute(
        "SELECT id, date, event_type, tag, sentiment, source FROM events "
        "WHERE ticker = ? ORDER BY date DESC LIMIT ?", (ticker, limit)))
    sim_n = 0
    cols = {r[1] for r in src.conn().execute("PRAGMA table_info(outcomes)")}
    sim_expr = "(o.journal_ref LIKE 'sim:%')" if "journal_ref" in cols else "0"
    for ev in events:
        ev["measured_outcomes"] = _rows(src.conn().execute(
            "SELECT o.date, o.archetype, o.r_multiple, o.result, "
            f"{sim_expr} AS simulated "
            "FROM outcomes o JOIN event_outcome_link l "
            "ON l.outcome_id = o.id WHERE l.event_id = ?", (ev["id"],)))
        sim_n += sum(1 for o in ev["measured_outcomes"] if o.get("simulated"))
    as_of = src.conn().execute("SELECT MAX(date) FROM outcomes").fetchone()[0]
    return {"available": True, "ticker": ticker, "events": events, "as_of": as_of,
            "simulated_outcomes": sim_n,
            "note": ("outcomes are the firm's own paper-trading measurements; rows with simulated=1 are "
                     "synthetic-chain backtests (inflated ~10x) and must never be read as realised P&L")}


def entity_affinity(src: Sources, args: dict) -> dict:
    name = (args.get("name") or "").upper()
    if not name:
        return {"available": False, "why": "name is required "
                "(a ticker/group or a market participant)"}
    limit = min(int(args.get("limit") or 15), 50)
    rows = _rows(src.conn().execute(
        "SELECT client, grp, deal_count, buy_qty, sell_qty, "
        "buy_value_rs, sell_value_rs, first_seen, last_seen "
        "FROM entity_affinity WHERE UPPER(grp) = ? OR UPPER(client) LIKE ? "
        "ORDER BY deal_count DESC LIMIT ?", (name, f"%{name}%", limit)))
    return {"available": True, "name": name, "relationships": rows,
            "note": "bulk/block-deal footprint pairs (participant ↔ stock)"}


def fo_liquidity(src: Sources, args: dict) -> dict:
    doc = src.artifact("fo_liquidity.json")
    if not doc:
        return {"available": False, "why": "liquidity artifact missing"}
    ticker = (args.get("ticker") or "").upper() or None
    symbols = doc.get("symbols") or {}
    if ticker:
        return {"available": True, "as_of": doc.get("as_of"),
                "ticker": ticker, "detail": symbols.get(ticker),
                "banned": ticker in (doc.get("banned") or []),
                "tier_rule": doc.get("tier_rule")}
    return {"available": True, "as_of": doc.get("as_of"),
            "banned": doc.get("banned"), "tier_rule": doc.get("tier_rule"),
            "symbols": symbols}


def market_regime(src: Sources, args: dict) -> dict:
    try:
        row = src.conn().execute(
            "SELECT date, vix, vix_band, macro_nifty_short, "
            "macro_nifty_medium, macro_bank_short, macro_bank_medium, "
            "news_net, fii_net, dii_net FROM daily_context "
            "ORDER BY date DESC LIMIT 1").fetchone()
        context = dict(row) if row else None
    except sqlite3.Error:
        context = None
    macro = src.artifact("macro_snapshot.json") or {}
    return {"available": context is not None, "latest_context": context,
            "macro_metrics": macro.get("metrics"),
            "macro_as_of": macro.get("as_of")}


def firm_equity_curve(src: Sources, args: dict) -> dict:
    try:
        rows = _rows(src.conn().execute(
            "SELECT ts, equity, peak_equity, drawdown_pct "
            "FROM equity_curve ORDER BY ts, rowid"))
    except sqlite3.Error:
        rows = []
    return {"available": bool(rows), "points": rows,
            "basis": "paper trading — no real money, not a performance claim"}


EARNINGS_CELL_FLOOR_N = 20     # spec §2 S3: nothing is read from a cell below this n


def _earnings_split(src: Sources) -> list:
    """Resolved outcomes by (entry-time results window, strategy); placebo/sim refs excluded."""
    from src.validation.stat_gates import EXCLUDED_REF_PREFIXES
    where = " AND ".join("journal_ref NOT LIKE ?" for _ in EXCLUDED_REF_PREFIXES)
    try:
        rows = _rows(src.conn().execute(
            "SELECT COALESCE(earnings_window, 'unlabelled') AS window, "
            "archetype AS strategy, COUNT(*) AS n, "
            "SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS wins, "
            "ROUND(AVG(r_multiple), 2) AS avg_r, "
            "SUM(CASE WHEN results_before_expiry = 1 THEN 1 ELSE 0 END) AS results_before_expiry, "
            "SUM(CASE WHEN results_crossed = 1 THEN 1 ELSE 0 END) AS results_crossed "
            f"FROM outcomes WHERE {where} GROUP BY 1, 2 ORDER BY 1, n DESC",
            [prefix + "%" for prefix in EXCLUDED_REF_PREFIXES]))
    except sqlite3.Error:
        return []
    for row in rows:
        row["readable"] = row["n"] >= EARNINGS_CELL_FLOOR_N
    return rows


def strategy_stats(src: Sources, args: dict) -> dict:
    try:
        rows = _rows(src.conn().execute(
            "SELECT strategy, COUNT(*) AS n, "
            "SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS wins, "
            "ROUND(AVG(pnl_net), 2) AS avg_pnl_net, "
            "ROUND(AVG(r_multiple), 2) AS avg_r "
            "FROM simulated_trades WHERE pnl_net IS NOT NULL "
            "GROUP BY strategy ORDER BY n DESC"))
    except sqlite3.Error:
        rows = []
    return {"available": bool(rows), "by_strategy": rows,
            "basis": "simulated/paper fills — mechanics evidence, "
                     "never expected return",
            "by_earnings_window": _earnings_split(src),
            "earnings_basis": "resolved paper outcomes (incl. hypothetical) labelled by the "
                              f"entry-time results window; a cell is read only at n >= "
                              f"{EARNINGS_CELL_FLOOR_N}"}


def graphrag_propagation(src: Sources, args: dict) -> dict:
    seed = (args.get("seed") or "").strip()
    if not seed:
        return {"available": False, "why": "seed is required"}
    polarity = float(args.get("polarity") if args.get("polarity") is not None else 1.0)
    polarity = max(-1.0, min(1.0, polarity))
    if polarity == 0.0:
        return {"available": False, "why": "non-zero polarity is required"}
    try:
        from src.knowledge_graph import resonance, spreading_activation
        conn = src.conn()
        g = resonance._load_readonly_digraph(conn)
        if g is None or g.number_of_edges() == 0:
            return {"available": False, "seed": seed, "why": "no active graph edges"}
        canon = seed.upper().removesuffix(".NS").removesuffix(".BO")
        candidates = [seed, canon, f"commodity:{canon}", f"macro:{canon}",
                      f"policy:{canon}", f"sector:{canon}"]
        seeds = {c: polarity for c in candidates if c in g}
        if not seeds:
            return {"available": False, "seed": seed, "why": "seed node not present in active graph"}
        res = spreading_activation.propagate(
            g, seeds, conn=conn, run_at=args.get("as_of"), log_activation=False
        )
        return {
            "available": True,
            "seed": seed,
            "seeds_resolved": list(seeds.keys()),
            "activated": res.get("activated") or [],
            "basis": "read-only 3D associative graph propagation (context only)",
        }
    except sqlite3.Error as exc:
        return {"available": False, "seed": seed, "why": str(exc)}


def bayesian_win_posterior(src: Sources, args: dict) -> dict:
    from src.knowledge_graph import brain_evolution
    conn = src.conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(outcomes)")}
    except sqlite3.Error as exc:
        return {"available": False, "why": str(exc)}
    if not cols:
        return {"available": False, "why": "outcomes table not present"}
    jref_col = "journal_ref" if "journal_ref" in cols else "NULL AS journal_ref"
    hyp_col = "hypothetical" if "hypothetical" in cols else "0 AS hypothetical"
    sql = f"SELECT date, ticker, archetype, r_multiple, result, {jref_col}, {hyp_col} FROM outcomes WHERE 1=1"
    params = []
    if args.get("archetype"):
        sql += " AND LOWER(archetype) = LOWER(?)"
        params.append(str(args["archetype"]).strip())
    if args.get("ticker"):
        sql += " AND UPPER(ticker) = UPPER(?)"
        params.append(str(args["ticker"]).strip().upper().removesuffix(".NS").removesuffix(".BO"))
    rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    post = brain_evolution.compute_bayesian_confidence(
        rows,
        as_of=args.get("as_of"),
    )
    return {
        "available": post["raw_n"] > 0,
        "posterior": post,
        "basis": "time-decayed Beta-Bernoulli posterior over learnable paper outcomes (sim/shadow/placebo excluded)",
    }


TOOLS = {
    "darling_tier_table": (
        "Current lifecycle-tier table for the tracked equity cohort "
        "(descriptive grades with the rule that fired per name).",
        {"type": "object", "properties": {}},
        darling_tier_table),
    "valuation_scores": (
        "Valuation composite (1-100, filed-data z-scores) for the cohort "
        "or one ticker, with veto reasons.",
        {"type": "object", "properties": {
            "ticker": {"type": "string"}}},
        valuation_scores),
    "earnings_quality": (
        "Forensic earnings-quality read of a company's newest analyzed "
        "annual report: conviction score, sub-scores, red/yellow flags.",
        {"type": "object", "properties": {
            "ticker": {"type": "string"}}, "required": ["ticker"]},
        earnings_quality),
    "event_history": (
        "The brain map's event memory for a ticker with any measured "
        "outcomes linked to each event.",
        {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "limit": {"type": "integer"}}, "required": ["ticker"]},
        event_history),
    "entity_affinity": (
        "Bulk/block-deal relationship footprints for a stock or market "
        "participant (who repeatedly deals in what).",
        {"type": "object", "properties": {
            "name": {"type": "string"},
            "limit": {"type": "integer"}}, "required": ["name"]},
        entity_affinity),
    "fo_liquidity": (
        "F&O liquidity tiers by options traded value, with the exchange "
        "ban list, for the derivatives universe or one ticker.",
        {"type": "object", "properties": {
            "ticker": {"type": "string"}}},
        fo_liquidity),
    "market_regime": (
        "Latest daily market context: VIX band, macro trend reads, news "
        "balance, FII/DII net flows.",
        {"type": "object", "properties": {}},
        market_regime),
    "firm_equity_curve": (
        "The firm's paper-trading equity curve (equity, peak, drawdown "
        "over time).",
        {"type": "object", "properties": {}},
        firm_equity_curve),
    "strategy_stats": (
        "Aggregate paper-trade counts, win counts and average P&L by "
        "strategy from the simulation ledger, plus resolved outcomes split "
        "by results window (pre / through / post / clear).",
        {"type": "object", "properties": {}},
        strategy_stats),
    "graphrag_propagation": (
        "Multi-hop associative graph propagation (3D energy and temporal "
        "momentum) from a seed entity or policy node over active edges.",
        {"type": "object", "properties": {
            "seed": {"type": "string"},
            "polarity": {"type": "number"},
            "as_of": {"type": "string"}}, "required": ["seed"]},
        graphrag_propagation),
    "bayesian_win_posterior": (
        "Time-decayed Beta-Bernoulli posterior win-rate distribution over "
        "learnable outcome history, optionally filtered by archetype or ticker.",
        {"type": "object", "properties": {
            "archetype": {"type": "string"},
            "ticker": {"type": "string"},
            "as_of": {"type": "string"}}},
        bayesian_win_posterior),
}


# --------------------------------------------------- JSON-RPC plumbing

def _result(msg_id, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def handle_message(msg: dict, src: Sources) -> dict:
    """One JSON-RPC message → one response dict, or None for
    notifications. Pure enough to test without a transport."""
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        return _result(msg_id, {
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        tools = [{"name": name, "description": desc, "inputSchema": schema}
                 for name, (desc, schema, _) in TOOLS.items()]
        return _result(msg_id, {"tools": tools})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        if name not in TOOLS:
            return _error(msg_id, -32602, f"unknown tool: {name}")
        _, _, fn = TOOLS[name]
        try:
            payload = fn(src, params.get("arguments") or {})
            return _result(msg_id, {
                "content": [{"type": "text",
                             "text": json.dumps(payload, default=str)}],
                "isError": False})
        except Exception as exc:      # fail-open: answer, never die
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"tool error: {exc}"}],
                "isError": True})
    if msg_id is None:                # unknown notification: stay silent
        return None
    return _error(msg_id, -32601, f"method not found: {method}")


def serve(inp=None, out=None, src: Sources = None) -> None:
    """Newline-delimited JSON-RPC over stdio. A malformed line is
    skipped (a broken client message must never kill the server)."""
    inp, out = inp or sys.stdin, out or sys.stdout
    src = src or Sources()
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_message(msg, src)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


if __name__ == "__main__":
    serve()

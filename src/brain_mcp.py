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
    for ev in events:
        ev["measured_outcomes"] = _rows(src.conn().execute(
            "SELECT o.date, o.archetype, o.r_multiple, o.result "
            "FROM outcomes o JOIN event_outcome_link l "
            "ON l.outcome_id = o.id WHERE l.event_id = ?", (ev["id"],)))
    return {"available": True, "ticker": ticker, "events": events,
            "note": "outcomes are the firm's own paper-trading measurements"}


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
            "FROM equity_curve ORDER BY ts"))
    except sqlite3.Error:
        rows = []
    return {"available": bool(rows), "points": rows,
            "basis": "paper trading — no real money, not a performance claim"}


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
                     "never expected return"}


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
        "strategy from the simulation ledger.",
        {"type": "object", "properties": {}},
        strategy_stats),
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

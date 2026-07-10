"""
Alpha Trading — Phase 6G: the capital & margin allocation layer
================================================================

A dedicated account profile for the automated options pipeline: a
simulated pool of starting capital (default Rs.10,00,000) living in
DATABASE state (brain_map.db — additive tables owned here, same pattern
as the simulator's `simulated_trades`), with three strict risk guards:

  1. MARGIN LOCKING — whenever the headless proposer fires an entry
     signal (iron condor or any defined-risk spread), the SPAN margin the
     structure blocks (portfolio.calculate_span_margin × lots) is
     digitally locked against the account BEFORE the proposal is allowed
     out. Locks are keyed by the entry's journal short_id and released
     when the plan tracker resolves the trade (or the human rejects it).

  2. MARGIN EXHAUSTION — if a new entry's required margin exceeds the
     account's available liquid cash (equity minus everything already
     locked), the entry signal is SILENTLY rejected: no journal line, no
     Discord alert — just a `margin_exhaustion` row in `account_events`.

  3. RISK OF RUIN — the account tracks its full equity curve and trailing
     drawdown from peak. If net portfolio drawdown ever breaches the
     hard-coded MAX_DRAWDOWN_PCT (10%), execution is blocked ENTIRELY:
     every subsequent entry is rejected until the equity recovers above
     the threshold, and the halt is logged as a `risk_of_ruin_halt`.

Design rules (matching the rest of the codebase):
  * Pure-Python + sqlite3 only, every function takes an injectable
    `conn` — the whole module tests offline against ':memory:'.
  * Additive: nothing here mutates portfolio.json, journal.jsonl, or any
    core brain_map table. The paper book's cash-settlement flow
    (plan_tracker._settle_spread_cash) is untouched — margin here is
    *virtually* blocked, exactly like a real clearing house blocks SPAN
    without taking the cash.
  * Fail-safe at the seams: the proposer/tracker call through the
    `gate_headless_entry` / `release_entry` wrappers, which never raise —
    an unreadable DB prints a note and FAILS OPEN so the learning
    pipeline keeps flowing (the guard is a paper-risk simulation, not a
    production brake).

Inspect the account from the project folder:

    python3 -m src.portfolio_manager
"""

from datetime import datetime

from src import brain_map

STARTING_CAPITAL = 1_000_000.0   # Rs.10,00,000 simulated allocation pool
MAX_DRAWDOWN_PCT = 10.0          # hard-coded risk-of-ruin parameter

# Owned by this module — additive to brain_map's tables, same .db file.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    starting_capital  REAL NOT NULL,
    realized_pnl      REAL NOT NULL DEFAULT 0,
    peak_equity       REAL NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS margin_locks (
    journal_ref  TEXT PRIMARY KEY,    -- the entry's journal short_id
    margin_rs    REAL NOT NULL,
    locked_at    TEXT NOT NULL,
    released_at  TEXT,                -- NULL = still locked
    pnl_net      REAL                 -- realized P&L applied on release
);
CREATE TABLE IF NOT EXISTS equity_curve (
    ts            TEXT NOT NULL,
    equity        REAL NOT NULL,
    peak_equity   REAL NOT NULL,
    drawdown_pct  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_events (
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,       -- margin_exhaustion | risk_of_ruin_halt | ...
    detail      TEXT
);
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def get_account(conn) -> dict:
    """The singleton account row, created with the default pool on first
    touch. Returns a plain dict so callers never depend on row_factory."""
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO account_state (id, starting_capital, realized_pnl, "
            "peak_equity, created_at) VALUES (1, ?, 0, ?, ?)",
            (STARTING_CAPITAL, STARTING_CAPITAL, _now_iso()))
        conn.commit()
        row = conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
    keys = ("id", "starting_capital", "realized_pnl", "peak_equity", "created_at")
    return {k: row[k] for k in keys} if hasattr(row, "keys") else dict(zip(keys, row))


def equity(conn) -> float:
    """Realized account equity: the starting pool plus every settled P&L."""
    acct = get_account(conn)
    return round(acct["starting_capital"] + acct["realized_pnl"], 2)


def locked_margin(conn) -> float:
    """Sum of every margin lock not yet released."""
    ensure_schema(conn)
    row = conn.execute("SELECT COALESCE(SUM(margin_rs), 0) FROM margin_locks "
                       "WHERE released_at IS NULL").fetchone()
    return round(float(row[0]), 2)


def available_cash(conn) -> float:
    """Liquid cash an entry may still lock: equity minus active locks."""
    return round(equity(conn) - locked_margin(conn), 2)


def drawdown_pct(conn) -> float:
    """Trailing drawdown from the ratcheted peak equity, in percent."""
    acct = get_account(conn)
    peak = float(acct["peak_equity"])
    if peak <= 0:
        return 0.0
    return round(max(0.0, (peak - equity(conn)) / peak * 100), 4)


def trading_halted(conn) -> bool:
    """True once trailing drawdown breaches the risk-of-ruin threshold."""
    return drawdown_pct(conn) >= MAX_DRAWDOWN_PCT


def log_event(conn, event_type: str, detail: str = "") -> None:
    ensure_schema(conn)
    conn.execute("INSERT INTO account_events (ts, event_type, detail) "
                 "VALUES (?, ?, ?)", (_now_iso(), event_type, detail))
    conn.commit()


def _snapshot_equity(conn) -> dict:
    """Append one equity-curve point (after any realized P&L change)."""
    eq, dd = equity(conn), drawdown_pct(conn)
    peak = float(get_account(conn)["peak_equity"])
    conn.execute("INSERT INTO equity_curve (ts, equity, peak_equity, "
                 "drawdown_pct) VALUES (?, ?, ?, ?)", (_now_iso(), eq, peak, dd))
    conn.commit()
    return {"equity": eq, "peak_equity": peak, "drawdown_pct": dd}


def required_margin_for(proposal: dict) -> float:
    """What a build_proposal() result blocks: the SPAN total (already
    hedge-offset by portfolio.calculate_span_margin) times the lots."""
    spread = proposal["spread"]
    return round(float(spread["margin"]["total_margin"])
                 * int(spread.get("lots", proposal.get("lots", 1))), 2)


def request_entry(conn, journal_ref: str, required_margin: float) -> dict:
    """The strict entry guard. Approve = the margin is locked under
    `journal_ref` (idempotent: re-requesting an active ref re-approves
    without double-locking). Reject = nothing is locked and the reason is
    logged to `account_events`.

    Order of the guards matters: the risk-of-ruin halt beats everything
    (even a tiny trade is blocked once the account is down 10%), then the
    margin-exhaustion check against available liquid cash."""
    ensure_schema(conn)
    get_account(conn)

    active = conn.execute("SELECT 1 FROM margin_locks WHERE journal_ref = ? "
                          "AND released_at IS NULL", (journal_ref,)).fetchone()
    if active:
        return {"approved": True, "reason": "margin already locked for this entry"}

    if trading_halted(conn):
        reason = (f"risk-of-ruin halt: drawdown {drawdown_pct(conn):.2f}% >= "
                  f"{MAX_DRAWDOWN_PCT:g}% — all entries blocked")
        log_event(conn, "risk_of_ruin_halt",
                  f"entry {journal_ref} rejected ({reason})")
        return {"approved": False, "reason": reason}

    cash = available_cash(conn)
    margin = round(float(required_margin), 2)
    if margin > cash:
        reason = (f"margin exhaustion: needs Rs.{margin:,.2f} but only "
                  f"Rs.{cash:,.2f} liquid (Rs.{locked_margin(conn):,.2f} "
                  "already locked)")
        log_event(conn, "margin_exhaustion",
                  f"entry {journal_ref} rejected ({reason})")
        return {"approved": False, "reason": reason}

    conn.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at) "
                 "VALUES (?, ?, ?)", (journal_ref, margin, _now_iso()))
    conn.commit()
    return {"approved": True, "reason": "margin locked"}


def release_margin(conn, journal_ref: str, pnl_net: float = 0.0) -> dict:
    """Close out one lock: mark it released, settle its realized P&L into
    the account, ratchet the peak, and append an equity-curve point.
    Unknown/already-released refs are a safe no-op (the tracker may sweep
    entries that never passed through the gate)."""
    ensure_schema(conn)
    active = conn.execute("SELECT margin_rs FROM margin_locks WHERE "
                          "journal_ref = ? AND released_at IS NULL",
                          (journal_ref,)).fetchone()
    if active is None:
        return {"released": False, "reason": "no active lock for this ref"}

    was_halted = trading_halted(conn)
    conn.execute("UPDATE margin_locks SET released_at = ?, pnl_net = ? "
                 "WHERE journal_ref = ?", (_now_iso(), round(float(pnl_net), 2),
                                           journal_ref))
    conn.execute("UPDATE account_state SET realized_pnl = round(realized_pnl + ?, 2), "
                 "peak_equity = max(peak_equity, starting_capital + realized_pnl + ?) "
                 "WHERE id = 1", (float(pnl_net), float(pnl_net)))
    conn.commit()
    snap = _snapshot_equity(conn)
    if not was_halted and trading_halted(conn):
        log_event(conn, "risk_of_ruin_halt",
                  f"drawdown hit {snap['drawdown_pct']:.2f}% after settling "
                  f"{journal_ref} (pnl Rs.{pnl_net:,.2f}) — execution blocked")
    return dict(snap, released=True, reason="settled",
                halted=trading_halted(conn))


def account_summary(conn) -> dict:
    """One dict with everything the CLI / notifier could want to show."""
    acct = get_account(conn)
    return {
        "starting_capital": acct["starting_capital"],
        "realized_pnl": acct["realized_pnl"],
        "equity": equity(conn),
        "peak_equity": acct["peak_equity"],
        "locked_margin": locked_margin(conn),
        "available_cash": available_cash(conn),
        "drawdown_pct": drawdown_pct(conn),
        "trading_halted": trading_halted(conn),
        "open_locks": conn.execute("SELECT COUNT(*) FROM margin_locks WHERE "
                                   "released_at IS NULL").fetchone()[0],
    }


# --- fail-safe seams for the proposer / tracker -------------------------
# These are the ONLY functions the pipeline calls. They open the real DB,
# never raise, and fail OPEN with a printed note: this layer is a paper
# risk simulation and must never be the reason the learning loop stalls.

def gate_headless_entry(journal_ref: str, required_margin: float,
                        conn=None) -> tuple:
    """(allowed, reason) for the headless proposer's entry signal."""
    try:
        owns = conn is None
        if conn is None:
            conn = brain_map.connect()
        verdict = request_entry(conn, journal_ref, required_margin)
        if owns:
            conn.close()
        return bool(verdict["approved"]), verdict["reason"]
    except Exception as e:
        print(f"  (margin gate unavailable — failing open: {e})")
        return True, f"margin gate unavailable ({e})"


def release_entry(journal_ref: str, pnl_net: float = 0.0, conn=None) -> dict:
    """Settle a resolved/rejected entry's lock; safe on unknown refs.

    Post-trade hook (Wealth-Locking Flywheel, paper scope): a PROFITABLE
    settlement that actually released a lock also triggers the 50%
    GOLDBEES paper sweep — a wealth_lock_ledger row + Discord card,
    advisory only, never a cash movement. Same fail-safe contract as the
    rest of this seam: a broken sweep can never block a settlement."""
    try:
        owns = conn is None
        if conn is None:
            conn = brain_map.connect()
        result = release_margin(conn, journal_ref, pnl_net)
        if result.get("released") and float(pnl_net) > 0:
            from src import wealth_lock
            result["wealth_sweep"] = wealth_lock.sweep_on_settlement(
                journal_ref, pnl_net, conn=conn)
        if owns:
            conn.close()
        return result
    except Exception as e:
        print(f"  (margin release skipped: {e})")
        return {"released": False, "reason": str(e)}


if __name__ == "__main__":
    import json
    connection = brain_map.connect()
    print(json.dumps(account_summary(connection), indent=2))
    connection.close()

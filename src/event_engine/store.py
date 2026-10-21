"""
The VM-side ledger of the event book (tables in brain_map.db). Money never lives here: every rupee
moves through the paper-account door (`portfolio_manager.paper_request_entry` /
`paper_release_margin` on PAPER_EVENTS). These tables are the positions and the audit trail —
which card, which A/B path, which source tags — so the Proving Court can split results later.
"""
from __future__ import annotations

import json
from datetime import datetime

from src.event_engine import IST

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_cards_seen (
    event_id   TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS event_actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event_id   TEXT,
    ticker     TEXT,
    path       TEXT,
    action     TEXT NOT NULL,          -- opened | abstained | refused | planned | same_strike | closed
    reason     TEXT,
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS event_positions (
    journal_ref        TEXT PRIMARY KEY,
    event_id           TEXT NOT NULL,
    underlying         TEXT NOT NULL,
    direction          TEXT NOT NULL,
    path               TEXT NOT NULL,      -- fixed | llm (the strike A/B test)
    option_type        TEXT NOT NULL,      -- CE | PE
    strike             REAL NOT NULL,
    expiry             TEXT NOT NULL,
    lots               INTEGER NOT NULL,
    lot_size           INTEGER NOT NULL,
    entry_premium      REAL NOT NULL,      -- per share, paid at the ask
    entry_total_rs     REAL NOT NULL,      -- the whole premium = the max loss
    opened_at          TEXT NOT NULL,
    horizon_days       INTEGER NOT NULL,
    materiality_source TEXT,
    response_source    TEXT,
    state              TEXT NOT NULL DEFAULT 'open',
    last_mark          REAL,
    last_mark_ts       TEXT,
    exit_premium       REAL,
    exit_reason        TEXT,
    closed_at          TEXT,
    pnl_net            REAL
);
CREATE INDEX IF NOT EXISTS idx_event_positions_state ON event_positions (state);
"""


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def ensure_schema(conn) -> None:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"event_cards_seen", "event_actions", "event_positions"} <= have:
        conn.executescript(SCHEMA)
        conn.commit()


def seen(conn, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM event_cards_seen WHERE event_id = ?", (event_id,)).fetchone() is not None


def mark_seen(conn, event_id: str, status: str, detail: str = "") -> None:
    conn.execute("INSERT OR IGNORE INTO event_cards_seen (event_id, seen_at, status, detail) VALUES (?, ?, ?, ?)",
                 (event_id, now_iso(), status, detail[:500]))
    conn.commit()


def action(conn, event_id, ticker, path, kind, reason=None, detail=None) -> None:
    conn.execute("INSERT INTO event_actions (ts, event_id, ticker, path, action, reason, detail) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (now_iso(), event_id, ticker, path, kind, reason,
                  json.dumps(detail, default=str)[:2000] if detail is not None else None))
    conn.commit()


def open_position(conn, row: dict) -> None:
    cols = ("journal_ref", "event_id", "underlying", "direction", "path", "option_type", "strike", "expiry",
            "lots", "lot_size", "entry_premium", "entry_total_rs", "opened_at", "horizon_days",
            "materiality_source", "response_source")
    conn.execute(f"INSERT INTO event_positions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(row.get(c) for c in cols))
    conn.commit()


def open_positions(conn) -> list:
    cur = conn.execute("SELECT * FROM event_positions WHERE state = 'open' ORDER BY opened_at")
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def record_mark(conn, ref: str, mark: float) -> None:
    conn.execute("UPDATE event_positions SET last_mark = ?, last_mark_ts = ? WHERE journal_ref = ?",
                 (mark, now_iso(), ref))
    conn.commit()


def close_position(conn, ref: str, exit_premium: float, reason: str, pnl: float) -> None:
    conn.execute("UPDATE event_positions SET state = 'closed', exit_premium = ?, exit_reason = ?, "
                 "closed_at = ?, pnl_net = ? WHERE journal_ref = ? AND state = 'open'",
                 (exit_premium, reason, now_iso(), round(float(pnl), 2), ref))
    conn.commit()

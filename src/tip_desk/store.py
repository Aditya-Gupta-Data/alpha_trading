"""The tip desk's tables in brain_map.db. Money never lives here: every rupee moves through the paper-account
door (`portfolio_manager.paper_request_entry` / `paper_release_margin` on PAPER_TIP_SANDBOX).

  tip_ledger     APPEND-ONLY (triggers refuse UPDATE/DELETE): every tip and every step of it — chatter,
                 rejected, stale, carded, cancelled, opened, booked, closed — so the tipster's real
                 post-slippage hit rate and expectancy can be graded later.
  tip_cards      the confirmation card per message group (its latest resolved contract, quote and sizing).
  tip_positions  one row per money lock: a 1-lot (or odd-remainder) tip is one row; a ≥2-lot tip is two
                 tranches, A (half, booked at T1) and B (the rest, trailed)."""
from __future__ import annotations

import json

from src.tip_desk import now_ist

SCHEMA = """
CREATE TABLE IF NOT EXISTS tip_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    group_id    TEXT,
    event       TEXT NOT NULL,
    kind        TEXT,
    reason      TEXT,
    raw_text    TEXT,
    parsed      TEXT,
    detail      TEXT
);
CREATE TRIGGER IF NOT EXISTS tip_ledger_no_update BEFORE UPDATE ON tip_ledger
BEGIN SELECT RAISE(ABORT, 'tip_ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS tip_ledger_no_delete BEFORE DELETE ON tip_ledger
BEGIN SELECT RAISE(ABORT, 'tip_ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS tip_cards (
    card_id     TEXT PRIMARY KEY,
    group_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    kind        TEXT,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tip_positions (
    journal_ref     TEXT PRIMARY KEY,
    card_id         TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    tranche         TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    expiry          TEXT NOT NULL,
    structure       TEXT NOT NULL,
    legs            TEXT NOT NULL,
    lots            INTEGER NOT NULL,
    lot_size        INTEGER NOT NULL,
    entry_premium   REAL NOT NULL,
    entry_total_rs  REAL NOT NULL,
    fees_rs         REAL NOT NULL DEFAULT 0,
    tip_entry_low   REAL,
    tip_entry_high  REAL,
    stale           INTEGER NOT NULL DEFAULT 0,
    sl              REAL,
    targets         TEXT NOT NULL DEFAULT '[]',
    current_sl      REAL,
    targets_hit     INTEGER NOT NULL DEFAULT 0,
    rule            TEXT NOT NULL,
    opened_at       TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'open',
    last_mark       REAL,
    last_mark_ts    TEXT,
    exit_premium    REAL,
    exit_reason     TEXT,
    closed_at       TEXT,
    pnl_net         REAL
);
CREATE INDEX IF NOT EXISTS idx_tip_positions_state ON tip_positions (state);
CREATE INDEX IF NOT EXISTS idx_tip_ledger_group ON tip_ledger (group_id);
"""
TABLES = {"tip_ledger", "tip_cards", "tip_positions"}
_JSON_COLS = ("legs", "targets")


def now_iso() -> str:
    return now_ist().isoformat(timespec="seconds")


def ensure_schema(conn) -> None:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
    if not (TABLES | {"tip_ledger_no_update", "tip_ledger_no_delete"}) <= have:
        conn.executescript(SCHEMA)
        conn.commit()


def _dump(v):
    return json.dumps(v, default=str)[:4000] if v is not None else None


def log(conn, event: str, group_id=None, kind=None, reason=None, raw_text=None, parsed=None, detail=None,
        commit: bool = True) -> None:
    conn.execute("INSERT INTO tip_ledger (ts, group_id, event, kind, reason, raw_text, parsed, detail) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (now_iso(), group_id, event, kind, (reason or None) and str(reason)[:500],
                  (raw_text or None) and str(raw_text)[:4000], _dump(parsed), _dump(detail)))
    if commit:
        conn.commit()


def ledger(conn, group_id: str = None) -> list:
    q, args = "SELECT * FROM tip_ledger", ()
    if group_id is not None:
        q, args = q + " WHERE group_id = ?", (group_id,)
    cur = conn.execute(q + " ORDER BY id", args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------- cards
def card_id_for(group_id: str) -> str:
    return f"tipc:{group_id}"


def save_card(conn, card_id: str, group_id: str, status: str, kind: str, payload: dict) -> None:
    now = now_iso()
    conn.execute("INSERT INTO tip_cards (card_id, group_id, created_at, updated_at, status, kind, payload) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(card_id) DO UPDATE SET updated_at = excluded.updated_at, "
                 "status = excluded.status, kind = excluded.kind, payload = excluded.payload",
                 (card_id, group_id, now, now, status, kind, json.dumps(payload, default=str)))
    conn.commit()


def get_card(conn, card_id: str):
    row = conn.execute("SELECT card_id, group_id, created_at, updated_at, status, kind, payload FROM tip_cards "
                       "WHERE card_id = ?", (card_id,)).fetchone()
    if row is None:
        return None
    keys = ("card_id", "group_id", "created_at", "updated_at", "status", "kind", "payload")
    c = dict(zip(keys, row))
    c["payload"] = json.loads(c["payload"])
    return c


def set_card_status(conn, card_id: str, status: str, only_if: str = None) -> bool:
    q = "UPDATE tip_cards SET status = ?, updated_at = ? WHERE card_id = ?"
    args = [status, now_iso(), card_id]
    if only_if is not None:
        q += " AND status = ?"
        args.append(only_if)
    cur = conn.execute(q, args)
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------ positions
_POS_COLS = ("journal_ref", "card_id", "group_id", "tranche", "underlying", "expiry", "structure", "legs", "lots",
             "lot_size", "entry_premium", "entry_total_rs", "fees_rs", "tip_entry_low", "tip_entry_high", "stale", "sl",
             "targets", "current_sl", "rule", "opened_at")


def insert_position(conn, row: dict, commit: bool = True) -> None:
    vals = tuple(json.dumps(row.get(c) or []) if c in _JSON_COLS else row.get(c) for c in _POS_COLS)
    conn.execute(f"INSERT INTO tip_positions ({', '.join(_POS_COLS)}) VALUES ({', '.join('?' * len(_POS_COLS))})",
                 vals)
    if commit:
        conn.commit()


def _rows(cur) -> list:
    names = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(names, r))
        for c in _JSON_COLS:
            d[c] = json.loads(d[c]) if d.get(c) else []
        out.append(d)
    return out


def positions(conn, state: str = "open", group_id: str = None) -> list:
    q, args = "SELECT * FROM tip_positions WHERE 1 = 1", []
    if state is not None:
        q += " AND state = ?"
        args.append(state)
    if group_id is not None:
        q += " AND group_id = ?"
        args.append(group_id)
    return _rows(conn.execute(q + " ORDER BY opened_at, journal_ref", args))


def position(conn, journal_ref: str):
    rows = _rows(conn.execute("SELECT * FROM tip_positions WHERE journal_ref = ?", (journal_ref,)))
    return rows[0] if rows else None


def open_premium_rs(conn) -> float:
    row = conn.execute("SELECT COALESCE(SUM(entry_total_rs), 0) FROM tip_positions WHERE state = 'open'").fetchone()
    return round(float(row[0]), 2)


def update_levels(conn, journal_ref: str, sl=None, targets=None, current_sl=None, targets_hit=None) -> None:
    sets, args = [], []
    for col, v in (("sl", sl), ("current_sl", current_sl), ("targets_hit", targets_hit)):
        if v is not None:
            sets.append(f"{col} = ?")
            args.append(v)
    if targets is not None:
        sets.append("targets = ?")
        args.append(json.dumps(targets))
    if sets:
        conn.execute(f"UPDATE tip_positions SET {', '.join(sets)} WHERE journal_ref = ? AND state = 'open'",
                     (*args, journal_ref))
        conn.commit()


def record_mark(conn, journal_ref: str, mark: float) -> None:
    conn.execute("UPDATE tip_positions SET last_mark = ?, last_mark_ts = ? WHERE journal_ref = ?",
                 (mark, now_iso(), journal_ref))
    conn.commit()


def close_position(conn, journal_ref: str, exit_premium: float, reason: str, pnl: float) -> bool:
    cur = conn.execute("UPDATE tip_positions SET state = 'closed', exit_premium = ?, exit_reason = ?, closed_at = ?, "
                       "pnl_net = ? WHERE journal_ref = ? AND state = 'open'",
                       (exit_premium, reason, now_iso(), round(float(pnl), 2), journal_ref))
    conn.commit()
    return cur.rowcount > 0

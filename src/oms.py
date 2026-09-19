"""
src/oms.py — the Order Management System schema and state machine (Phase M2, GAP 3)
====================================================================================

WHAT THIS IS (decision #100, 2026-09-19). SYSTEM_BLUEPRINT.md GAP 3 named the
flat execution layer: fills are whole and instantaneous, `outcomes` holds one
r_multiple per journal ref, `margin_locks` one lock per ref, and there is no
representation of an order that is pending, partly filled, or rejected. Real
capital cannot be attached to a system whose memory cannot say what an order
did. This module is the STRUCTURAL PLUMBING for that — the schema, the state
machine and the one write door — and nothing else.

WHAT THIS IS NOT. It places no order. It imports no broker. It is not wired
into `plan_tracker`, `options_proposer` or the capital layer. Rule 7 ("no
broker/order path in src/") is UNCHANGED: an Order Ticket here is a record of
intent with a lifecycle, filled only by whatever paper or (one day, by a
numbered decision) real venue a caller explicitly drives through
`apply_fill` / `reject` / `cancel`. Nothing in this file reads a quote.

SCHEMA (additive tables in brain_map.db, #25 discipline):

  trade_tickets   one row per multi-leg ORDER TICKET the router issued
                  (ticket_id PK, parent journal_ref, underlying, strategy,
                  direction, lots, lot_size, issued_at, status = the
                  ROLL-UP of its legs, reward_risk, source, note)
  trade_legs      one row per LEG of a ticket (leg_id PK, ticket_id FK,
                  leg_index, side, option_type, strike, expiry, qty_target,
                  qty_filled, limit_price, avg_fill_price, fill_basis,
                  state, correlation_id, broker_order_id, reject_reason,
                  created_at, updated_at)
  leg_events      the append-only audit of every transition
                  (leg_id, from_state, to_state, qty, price, ts, detail)

ORDER STATES (per leg; the ticket rolls them up):

  PENDING ──► PARTIAL ──► FILLED
     │           │
     ├──► REJECTED (venue refused; terminal)
     └──► CANCELLED (we withdrew; terminal — a PARTIAL cancel keeps its fills)

  Terminal states never transition again. `qty_filled` can only rise, never
  above `qty_target`; an over-fill is refused with a named reason.
  Ticket status: FILLED when every leg is FILLED; REJECTED when any leg is
  REJECTED; CANCELLED when every leg ended CANCELLED/REJECTED with no fill;
  PARTIAL when any fill exists and the ticket is not complete; else PENDING.

IDEMPOTENCY. Every leg carries a `correlation_id` (≤ 30 chars, the field
Dhan's order API echoes back, decision #94 notes no server-side idempotency)
derived deterministically from (ticket_id, leg_index), so a restart that
re-submits a leg can be recognised rather than duplicated.

Pure Python + sqlite; every function takes a connection. Never raises for
a business refusal — it returns {"ok": False, "reason": ...}.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

PENDING, PARTIAL, FILLED, REJECTED, CANCELLED = (
    "PENDING", "PARTIAL", "FILLED", "REJECTED", "CANCELLED")
STATES = (PENDING, PARTIAL, FILLED, REJECTED, CANCELLED)
TERMINAL = frozenset({FILLED, REJECTED, CANCELLED})
LEGAL_TRANSITIONS = {
    PENDING:   {PARTIAL, FILLED, REJECTED, CANCELLED},
    PARTIAL:   {PARTIAL, FILLED, CANCELLED},
    FILLED:    set(),
    REJECTED:  set(),
    CANCELLED: set(),
}
CORRELATION_ID_MAX = 30
ENTRY, EXIT = "ENTRY", "EXIT"       # trade_tickets.kind (decision #103)
PRIMARY_ACCOUNT = "PAPER_10L"     # = the capital layer's ACCOUNT_PAPER_10L (kept as a literal: no Dept-3 import here)


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


# ------------------------------------------------------------------ schema

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_tickets (
            ticket_id    TEXT PRIMARY KEY,
            journal_ref  TEXT,
            underlying   TEXT NOT NULL,
            strategy     TEXT NOT NULL,
            direction    TEXT,
            lots         INTEGER NOT NULL,
            lot_size     INTEGER NOT NULL,
            reward_risk  REAL,
            source       TEXT NOT NULL,
            status       TEXT NOT NULL,
            issued_at    TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            note         TEXT
        );
        CREATE TABLE IF NOT EXISTS trade_legs (
            leg_id           TEXT PRIMARY KEY,
            ticket_id        TEXT NOT NULL REFERENCES trade_tickets(ticket_id),
            leg_index        INTEGER NOT NULL,
            side             TEXT NOT NULL,
            option_type      TEXT,
            strike           REAL,
            expiry           TEXT,
            qty_target       INTEGER NOT NULL,
            qty_filled       INTEGER NOT NULL DEFAULT 0,
            limit_price      REAL,
            avg_fill_price   REAL,
            fill_basis       TEXT,
            state            TEXT NOT NULL,
            correlation_id   TEXT NOT NULL UNIQUE,
            broker_order_id  TEXT,
            reject_reason    TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_legs_ticket ON trade_legs (ticket_id);
        CREATE TABLE IF NOT EXISTS leg_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            leg_id      TEXT NOT NULL,
            from_state  TEXT,
            to_state    TEXT NOT NULL,
            qty         INTEGER,
            price       REAL,
            ts          TEXT NOT NULL,
            detail      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_leg_events_leg ON leg_events (leg_id);
    """)
    # Dual paper treasury (decision #102): every ticket names the PAPER
    # ACCOUNT it was issued for. Additive in-place upgrade (the brain_map
    # post_mortem pattern) — rows that predate the column are the primary's.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_tickets)")}
    if "account_id" not in cols:
        conn.execute("ALTER TABLE trade_tickets ADD COLUMN account_id TEXT NOT NULL "
                     f"DEFAULT '{PRIMARY_ACCOUNT}'")
    # Decision #103: EXITS go through the OMS too. `kind` = ENTRY | EXIT;
    # rows that predate the column are entries.
    if "kind" not in cols:
        conn.execute(f"ALTER TABLE trade_tickets ADD COLUMN kind TEXT NOT NULL DEFAULT '{ENTRY}'")
    conn.commit()


# ------------------------------------------------------------------ ids

def ticket_id_for(journal_ref: str | None, underlying: str, strategy: str,
                  issued_at: str, account_id: str = None, kind: str = None) -> str:
    """Deterministic ticket id. The primary account's ENTRY id is unchanged
    from #100; a shadow account's ticket for the SAME journal_ref carries
    the account in its key, and an EXIT ticket carries its kind, so the
    tickets on one journal_ref never collide."""
    key = f"{journal_ref or ''}|{underlying}|{strategy}|{issued_at}"
    if account_id and account_id != PRIMARY_ACCOUNT:
        key += f"|{account_id}"
    if kind and kind != ENTRY:
        key += f"|{kind}"
    return "tkt:" + hashlib.sha1(key.encode()).hexdigest()[:14]


def correlation_id_for(ticket_id: str, leg_index: int) -> str:
    """Deterministic, ≤ 30 chars, [A-Za-z0-9_-] only — the shape Dhan's
    correlationId accepts and echoes (#94)."""
    raw = hashlib.sha1(f"{ticket_id}|{leg_index}".encode()).hexdigest()[:22]
    return f"L{leg_index}_{raw}"[:CORRELATION_ID_MAX]


# ------------------------------------------------------------------ issue

def issue_ticket(conn, ticket: dict) -> dict:
    """Persist an Order Ticket from `strategy_router.build_ticket` with every
    leg PENDING. Idempotent on ticket_id. Returns {ok, ticket_id, created}."""
    ensure_schema(conn)
    tid = ticket["ticket_id"]
    if conn.execute("SELECT 1 FROM trade_tickets WHERE ticket_id = ?", (tid,)).fetchone():
        return {"ok": True, "ticket_id": tid, "created": False}
    if not ticket.get("legs"):
        return {"ok": False, "ticket_id": tid, "created": False, "reason": "ticket has no legs"}
    now = _now()
    conn.execute(
        "INSERT INTO trade_tickets (ticket_id, journal_ref, underlying, strategy, direction, "
        "lots, lot_size, reward_risk, source, status, issued_at, updated_at, note, "
        "account_id, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, ticket.get("journal_ref"), ticket["underlying"], ticket["strategy"],
         ticket.get("direction"), int(ticket["lots"]), int(ticket["lot_size"]),
         ticket.get("reward_risk"), ticket.get("source", "unknown"), PENDING,
         ticket.get("issued_at") or now, now, ticket.get("note"),
         ticket.get("account_id") or PRIMARY_ACCOUNT, ticket.get("kind") or ENTRY))
    for leg in ticket["legs"]:
        conn.execute(
            "INSERT INTO trade_legs (leg_id, ticket_id, leg_index, side, option_type, strike, "
            "expiry, qty_target, qty_filled, limit_price, avg_fill_price, fill_basis, state, "
            "correlation_id, broker_order_id, reject_reason, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,?,NULL,?,?,?,NULL,NULL,?,?)",
            (leg["leg_id"], tid, int(leg["leg_index"]), leg["side"], leg.get("option_type"),
             leg.get("strike"), leg.get("expiry"), int(leg["qty_target"]),
             leg.get("limit_price"), leg.get("fill_basis"), PENDING,
             leg["correlation_id"], now, now))
        _event(conn, leg["leg_id"], None, PENDING, None, None, "issued")
    conn.commit()
    return {"ok": True, "ticket_id": tid, "created": True}


# ------------------------------------------------------------------ transitions

def _event(conn, leg_id, frm, to, qty, price, detail):
    conn.execute("INSERT INTO leg_events (leg_id, from_state, to_state, qty, price, ts, detail) "
                 "VALUES (?,?,?,?,?,?,?)", (leg_id, frm, to, qty, price, _now(), detail))


def _leg(conn, leg_id):
    row = conn.execute("SELECT * FROM trade_legs WHERE leg_id = ?", (leg_id,)).fetchone()
    return dict(row) if row else None


def _refuse(reason, **extra):
    return {"ok": False, "reason": reason, **extra}


def apply_fill(conn, leg_id: str, qty: int, price: float,
               broker_order_id: str = None, basis: str = None) -> dict:
    """A fill report for one leg. Moves PENDING/PARTIAL -> PARTIAL or FILLED,
    keeps a volume-weighted average price, refuses over-fills, non-positive
    quantities and fills on terminal legs. Returns {ok, state, qty_filled}."""
    ensure_schema(conn)
    leg = _leg(conn, leg_id)
    if leg is None:
        return _refuse("unknown leg")
    try:
        qty, price = int(qty), float(price)
    except (TypeError, ValueError):
        return _refuse("fill qty/price not numeric")
    if qty <= 0 or price <= 0:
        return _refuse("fill qty and price must be positive")
    if leg["state"] in TERMINAL:
        return _refuse(f"leg is terminal ({leg['state']})", state=leg["state"])
    remaining = leg["qty_target"] - leg["qty_filled"]
    if qty > remaining:
        return _refuse(f"over-fill: {qty} > remaining {remaining}", state=leg["state"])
    filled = leg["qty_filled"] + qty
    prev_avg = leg["avg_fill_price"] or 0.0
    avg = (prev_avg * leg["qty_filled"] + price * qty) / filled
    new_state = FILLED if filled == leg["qty_target"] else PARTIAL
    if new_state not in LEGAL_TRANSITIONS[leg["state"]]:
        return _refuse(f"illegal {leg['state']} -> {new_state}", state=leg["state"])
    conn.execute(
        "UPDATE trade_legs SET qty_filled = ?, avg_fill_price = ?, state = ?, "
        "broker_order_id = COALESCE(?, broker_order_id), fill_basis = COALESCE(?, fill_basis), "
        "updated_at = ? WHERE leg_id = ?",
        (filled, round(avg, 4), new_state, broker_order_id, basis, _now(), leg_id))
    _event(conn, leg_id, leg["state"], new_state, qty, price, "fill")
    _roll_up(conn, leg["ticket_id"])
    conn.commit()
    return {"ok": True, "state": new_state, "qty_filled": filled, "avg_fill_price": round(avg, 4)}


def reject(conn, leg_id: str, reason: str, broker_order_id: str = None) -> dict:
    """The venue refused the leg. Only a PENDING leg can be rejected (a
    PARTIAL has real fills and must be CANCELLED, keeping them)."""
    ensure_schema(conn)
    leg = _leg(conn, leg_id)
    if leg is None:
        return _refuse("unknown leg")
    if REJECTED not in LEGAL_TRANSITIONS[leg["state"]]:
        return _refuse(f"illegal {leg['state']} -> REJECTED", state=leg["state"])
    conn.execute("UPDATE trade_legs SET state = ?, reject_reason = ?, "
                 "broker_order_id = COALESCE(?, broker_order_id), updated_at = ? WHERE leg_id = ?",
                 (REJECTED, str(reason)[:300], broker_order_id, _now(), leg_id))
    _event(conn, leg_id, leg["state"], REJECTED, None, None, str(reason)[:300])
    _roll_up(conn, leg["ticket_id"])
    conn.commit()
    return {"ok": True, "state": REJECTED}


def cancel(conn, leg_id: str, reason: str = "cancelled") -> dict:
    """We withdrew the leg. PENDING or PARTIAL only; fills already made
    stay on the row (qty_filled is never reduced)."""
    ensure_schema(conn)
    leg = _leg(conn, leg_id)
    if leg is None:
        return _refuse("unknown leg")
    if CANCELLED not in LEGAL_TRANSITIONS[leg["state"]]:
        return _refuse(f"illegal {leg['state']} -> CANCELLED", state=leg["state"])
    conn.execute("UPDATE trade_legs SET state = ?, updated_at = ? WHERE leg_id = ?",
                 (CANCELLED, _now(), leg_id))
    _event(conn, leg_id, leg["state"], CANCELLED, None, None, str(reason)[:300])
    _roll_up(conn, leg["ticket_id"])
    conn.commit()
    return {"ok": True, "state": CANCELLED, "qty_filled": leg["qty_filled"]}


def cancel_ticket(conn, ticket_id: str, reason: str = "cancelled") -> dict:
    """Cancel every open leg of a ticket. Returns per-leg results."""
    ensure_schema(conn)
    out = {}
    for row in conn.execute("SELECT leg_id, state FROM trade_legs WHERE ticket_id = ?",
                            (ticket_id,)):
        if row["state"] in TERMINAL:
            out[row["leg_id"]] = {"ok": True, "state": row["state"], "skipped": "terminal"}
        else:
            out[row["leg_id"]] = cancel(conn, row["leg_id"], reason)
    return {"ok": True, "legs": out, "status": ticket_status(conn, ticket_id)}


# ------------------------------------------------------------------ roll-up

def roll_up_status(leg_states: list, leg_fills: list) -> str:
    """The ticket's status from its legs (pure)."""
    if not leg_states:
        return PENDING
    if any(s == REJECTED for s in leg_states):
        return REJECTED
    if all(s == FILLED for s in leg_states):
        return FILLED
    any_fill = any(q > 0 for q in leg_fills)
    if all(s in (CANCELLED, REJECTED) for s in leg_states) and not any_fill:
        return CANCELLED
    if any_fill or any(s == PARTIAL for s in leg_states):
        return PARTIAL
    if all(s in TERMINAL for s in leg_states):
        return CANCELLED
    return PENDING


def _roll_up(conn, ticket_id: str) -> str:
    rows = conn.execute("SELECT state, qty_filled FROM trade_legs WHERE ticket_id = ?",
                        (ticket_id,)).fetchall()
    status = roll_up_status([r["state"] for r in rows], [r["qty_filled"] for r in rows])
    conn.execute("UPDATE trade_tickets SET status = ?, updated_at = ? WHERE ticket_id = ?",
                 (status, _now(), ticket_id))
    return status


def ticket_status(conn, ticket_id: str):
    row = conn.execute("SELECT status FROM trade_tickets WHERE ticket_id = ?",
                       (ticket_id,)).fetchone()
    return row["status"] if row else None


def ticket_view(conn, ticket_id: str) -> dict | None:
    """The ticket with its legs and audit trail, as plain dicts."""
    ensure_schema(conn)
    t = conn.execute("SELECT * FROM trade_tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if t is None:
        return None
    legs = [dict(r) for r in conn.execute(
        "SELECT * FROM trade_legs WHERE ticket_id = ? ORDER BY leg_index", (ticket_id,))]
    events = [dict(r) for r in conn.execute(
        "SELECT e.* FROM leg_events e JOIN trade_legs l ON l.leg_id = e.leg_id "
        "WHERE l.ticket_id = ? ORDER BY e.id", (ticket_id,))]
    return {**dict(t), "legs": legs, "events": events}


def open_legs(conn, ticket_id: str = None) -> list:
    ensure_schema(conn)
    q = "SELECT * FROM trade_legs WHERE state IN (?, ?)"
    args = [PENDING, PARTIAL]
    if ticket_id:
        q += " AND ticket_id = ?"
        args.append(ticket_id)
    return [dict(r) for r in conn.execute(q + " ORDER BY ticket_id, leg_index", args)]


def to_json(view: dict) -> str:
    return json.dumps(view, indent=2, default=str)

"""
src/execution/paper_venue.py — the PAPER venue: a mock exchange for Order Tickets
==================================================================================

WHAT IT IS (decision #101, 2026-09-19). The OMS (`src/oms.py`) gives an
order a lifecycle; this module is the first thing that DRIVES that
lifecycle. It sweeps `trade_legs` for PENDING legs and fills them the way
a real venue would fill a paper order at the close: at the leg's limit
(the proposer's honest #70 fill) worsened by the liquidity-tier slippage
of the underlying (`liquidity_slippage.tier_slippage_frac`: tier1 0.10 %,
tier2 0.25 %, illiquid 0.50 %, indices tier1), BUY pays up, SELL receives
less. Each fill goes through `oms.apply_fill`, so the leg moves
PENDING -> FILLED (or PARTIAL when the venue is told to fill only part),
the ticket rolls up through the OMS's own logic, and the audit trail
records it.

WHAT IT IS NOT. Not a broker. It reads no live quote and touches no
money: the capital layer still locks margin at proposal time exactly as
before, and `plan_tracker` still settles. It is the mechanism by which the
desk can run a REAL order lifecycle on paper — partials, rejects, a
venue's slippage — for weeks before any real venue exists (the M2 entry
criterion "execution path separately proven").

REJECTIONS. A leg with no usable limit price, or whose ticket is missing,
is REJECTED with a named reason; a fill the OMS refuses (over-fill,
terminal leg) is reported, never forced.

THE JOURNAL HANDSHAKE (the one write outside the OMS tables). When every
leg of a ticket is FILLED, `sweep()` stamps the VENUE prices back onto the
journal entry's spread legs (`premium` = the venue fill, `fill_basis =
"venue"`, `venue_fill` = the pre-slippage limit, `ticket_id`) through
`journal.update_entry` — so the tracker's exit math, which already skips
entry-side slippage for a `quoted` basis, sees the venue's slippage as
the entry cost paid and does not charge it twice. Older rows (no ticket)
are untouched.

Pure Python; `conn`, `today`, the slippage function and the journal door
are injectable; the pytest muzzle refuses to open the real brain map.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

from src import oms

IST = timezone(timedelta(hours=5, minutes=30))
VENUE = "paper_venue"
FILL_BASIS = "venue"


def _fill_fraction_default(_leg: dict) -> float:
    return 1.0


def slipped_price(limit: float, side: str, frac: float) -> float:
    """A BUY pays up by `frac`, a SELL receives `frac` less. Never below a
    tick of 0.05."""
    px = float(limit) * (1.0 + frac if str(side).upper() == "BUY" else 1.0 - frac)
    return max(0.05, round(px, 2))


def _tier_frac(underlying: str, slippage_fn=None) -> float:
    if slippage_fn is not None:
        return float(slippage_fn(underlying))
    from src.liquidity_slippage import tier_slippage_frac
    return float(tier_slippage_frac(str(underlying).split(".")[0]))


def fill_leg(conn, leg: dict, ticket: dict, slippage_fn=None,
             fill_fraction_fn=None, today: date = None) -> dict:
    """Fill ONE pending leg. Returns the OMS result plus the venue's price.
    A leg without a positive limit is rejected by name."""
    limit = leg.get("limit_price")
    try:
        limit = float(limit)
    except (TypeError, ValueError):
        limit = None
    if not limit or limit <= 0:
        r = oms.reject(conn, leg["leg_id"], f"{VENUE}: no usable limit price")
        return {**r, "leg_id": leg["leg_id"], "rejected": True}
    frac = _tier_frac(ticket["underlying"], slippage_fn)
    px = slipped_price(limit, leg["side"], frac)
    remaining = int(leg["qty_target"]) - int(leg["qty_filled"])
    share = float((fill_fraction_fn or _fill_fraction_default)(leg))
    qty = max(1, min(remaining, int(round(remaining * share)))) if remaining > 0 else 0
    if qty <= 0:
        return {"ok": False, "leg_id": leg["leg_id"], "reason": "nothing remaining"}
    r = oms.apply_fill(conn, leg["leg_id"], qty, px,
                       broker_order_id=f"{VENUE}:{(today or date.today()).isoformat()}",
                       basis=FILL_BASIS)
    return {**r, "leg_id": leg["leg_id"], "price": px, "qty": qty,
            "slippage_frac": frac, "limit": limit}


def stamp_journal(ticket_id: str, conn, journal_mod=None) -> bool:
    """Write the venue's fills back onto the journal entry's legs (see
    THE JOURNAL HANDSHAKE). Returns True when a row was updated. Only the
    PRIMARY account's ticket stamps — the journal legs ARE the 10L fill; a
    shadow account's fill is journaled on its own ledger (#102)."""
    view = oms.ticket_view(conn, ticket_id)
    if not view or not view.get("journal_ref") or view["status"] != oms.FILLED:
        return False
    if (view.get("account_id") or oms.PRIMARY_ACCOUNT) != oms.PRIMARY_ACCOUNT:
        return False
    from src import journal as _journal
    journal_mod = journal_mod or _journal
    fills = {(str(l["side"]).upper(), str(l.get("option_type") or "").upper(),
              float(l["strike"])): l for l in view["legs"] if l.get("strike") is not None}

    def mutate(entry):
        spread = entry.get("spread") or {}
        legs = spread.get("legs") or []
        if not legs:
            return False
        for leg in legs:
            key = (str(leg.get("side")).upper(), str(leg.get("option_type") or "").upper(),
                   float(leg.get("strike")))
            f = fills.get(key)
            if f is None or f.get("avg_fill_price") is None:
                continue
            leg["venue_fill"] = leg.get("premium")
            leg["premium"] = float(f["avg_fill_price"])
            leg["fill_basis"] = FILL_BASIS
        spread["ticket_id"] = ticket_id
        spread["venue"] = VENUE
        return True

    return journal_mod.update_entry(view["journal_ref"], mutate) is not None


def journal_shadow_fill(conn, ticket_id: str) -> bool:
    """THE SHADOW ACCOUNT'S FILL RECORD (#102). A FILLED ticket issued for a
    non-primary paper account is journaled on that account's own ledger
    (`paper_account_events`, event `venue_fill`, with the per-leg venue
    prices and lots) — never on `journal.jsonl`, which is the 10L book.
    The table is owned by the capital layer; this writes one audit row and
    nothing else. Returns True when a row was written."""
    view = oms.ticket_view(conn, ticket_id)
    if not view or view["status"] != oms.FILLED:
        return False
    account = view.get("account_id") or oms.PRIMARY_ACCOUNT
    if account == oms.PRIMARY_ACCOUNT:
        return False
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'paper_account_events'")}
    if not tables:
        return False
    fills = [{"leg": l["leg_index"], "side": l["side"], "type": l.get("option_type"),
              "strike": l.get("strike"), "limit": l.get("limit_price"),
              "fill": l.get("avg_fill_price"), "qty": l.get("qty_filled")}
             for l in view["legs"]]
    detail = json.dumps({"ticket_id": ticket_id, "underlying": view["underlying"],
                         "strategy": view["strategy"], "lots": view["lots"],
                         "lot_size": view["lot_size"], "venue": VENUE, "legs": fills})
    conn.execute("INSERT INTO paper_account_events (account_id, ts, event_type, journal_ref, "
                 "detail) VALUES (?, ?, 'venue_fill', ?, ?)",
                 (account, datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds"),
                  view.get("journal_ref"), detail))
    conn.commit()
    return True


def sweep(conn=None, today: date = None, slippage_fn=None, fill_fraction_fn=None,
          journal_mod=None, stamp: bool = True) -> dict:
    """One venue pass over every PENDING/PARTIAL leg — every account's
    tickets, same slippage. Returns {legs, filled, partial, rejected,
    refused, tickets_filled, stamped, shadow_journaled, by_account}."""
    summary = {"venue": VENUE, "legs": 0, "filled": 0, "partial": 0, "rejected": 0,
               "refused": 0, "tickets_filled": [], "stamped": 0, "shadow_journaled": 0,
               "by_account": {}, "skip": None}
    own = None
    if conn is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            summary["skip"] = "muzzled_under_pytest"
            return summary
        from src import brain_map
        own = conn = brain_map.connect()
    try:
        oms.ensure_schema(conn)
        touched = set()
        for leg in oms.open_legs(conn):
            ticket = conn.execute("SELECT * FROM trade_tickets WHERE ticket_id = ?",
                                  (leg["ticket_id"],)).fetchone()
            if ticket is None:
                oms.reject(conn, leg["leg_id"], f"{VENUE}: orphan leg, no ticket")
                summary["rejected"] += 1
                continue
            summary["legs"] += 1
            r = fill_leg(conn, leg, dict(ticket), slippage_fn, fill_fraction_fn, today)
            if r.get("rejected"):
                summary["rejected"] += 1
            elif not r.get("ok"):
                summary["refused"] += 1
            elif r.get("state") == oms.FILLED:
                summary["filled"] += 1
            else:
                summary["partial"] += 1
            touched.add(leg["ticket_id"])
        for tid in sorted(touched):
            if oms.ticket_status(conn, tid) == oms.FILLED:
                summary["tickets_filled"].append(tid)
                row = conn.execute("SELECT account_id FROM trade_tickets WHERE ticket_id = ?",
                                   (tid,)).fetchone()
                account = (row[0] if row else None) or oms.PRIMARY_ACCOUNT
                summary["by_account"][account] = summary["by_account"].get(account, 0) + 1
                if account == oms.PRIMARY_ACCOUNT:
                    if stamp and stamp_journal(tid, conn, journal_mod):
                        summary["stamped"] += 1
                elif journal_shadow_fill(conn, tid):
                    summary["shadow_journaled"] += 1
    finally:
        if own is not None:
            own.close()
    return summary


if __name__ == "__main__":
    import json
    print(json.dumps(sweep(), indent=2, default=str))

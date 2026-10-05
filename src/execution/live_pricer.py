"""
src/execution/live_pricer.py — PAPER_2L_LIVE: the live-quote arm (decision #120)
================================================================================
A fourth paper account whose MARKS and EXITS are priced only on live
intra-day option-chain quotes with the bid/ask spread CROSSED — every long
leg valued at the bid it could be sold at, every short leg at the ask it
would be bought back at — never the EOD linear time-value model. It exists
to measure the edge net of real friction on thin stock options before any
rupee goes live.

WHAT IT OWNS. One additive table, `paper_live_positions` (this module's
position + marks record). The money truth stays in `paper_margin_locks`
(portfolio_manager): this account locks margin like every shadow account
(#102) and settles that lock ITSELF through `portfolio_manager.
paper_release_margin` — the primary's exit never closes it, and
`release_shadow_locks` skips a LIVE lock only while an OPEN row exists here
(no row = the position never existed = the lock is released at zero, named).

ENTRY (options_proposer._execute_paper_entry, at approval): the chain is
RE-QUOTED at that moment (`requote_entry`) — the proposal-time quotes may
be hours old under a paused auto-approve — and the account refuses, with
its lock released, unless the market is open and every leg has a usable
crossed quote (BUY at ask, SELL at bid, the #70 rule). A refusal for no
chain carries the door's reason ("option chain unavailable (rate limit
(DH-904: ...))") into the log line and the `live_entry_refused` event. The paper venue
fills that ticket at the crossed limits with ZERO tier slippage
(`paper_venue.ZERO_SLIP_ACCOUNTS`): crossing IS the cost. `open_position`
then records the fills; d = Σ sign × fill (long +, short −; d > 0 debit,
d < 0 credit), width = the structure's `spread_width`, max_profit =
width − d (debit) or −d (credit), max_loss = d (debit) or width + d (credit).

MARK + EXIT (`tick`, from live_bridge.live_cycle every 60 s in market
hours): one option chain per (underlying, expiry) at most every
LIVE_QUOTE_INTERVAL_SECONDS, at most `max_fetches` per tick, paced ≥ 3 s
apart on the tick's own clock and ≥ 3.5 s from ANY chain call on the host
(dhan_client's chain lane); a failed fetch is logged with the door's
reason. Never after `cutoff` (15:27 IST —
the scheduler must still self-terminate at 15:30). Per leg: a SHORT leg
with no ask, a bid above the ask, or a quote > 50% off its last price
means NO mark this tick (abstain, keep the last mark); a LONG leg with no
bid is worth 0 (it cannot be sold) and the structure still marks.
A crossed mark can only be at or BELOW true value, so a mark above the
structure's upper bound (d + max_profit: the width for a debit, 0 for a
credit) by more than a tick is a bad print — the tick abstains on it
(audit F01). profit = clamp(mark − d, −max_loss, +max_profit); capture =
profit ÷ max_profit. The account keeps its OWN profit-ratchet peak/lock
(#110) on its own capture; a read that would raise the lock to a new rung
is persisted only once a SECOND, later chain fetch also clears that rung
(`_confirm_rung`, F01) — one snapshot can never arm a lock. Neutral
structures keep the static 65% take; the pre-expiry rule is the tracker's
own predicate. There is NO stop (#105). A predicate that fires on a
CACHED chain is re-verified on a fresh fetch before anything is issued.
An exit that would cost more than the structure's max loss, or that is
priced on an impossible mark, is refused (held) — expiry can never do
worse.

EXIT EXECUTION: the row is stamped `exiting` FIRST, then ONE EXIT ticket
for this account alone goes through plan_tracker._execute_paper_exit (the
file's single OMS exit door), the venue fills at the crossed limits, P&L =
(exit − d) × qty − the 2026 friction stack on every entry and exit leg (no
slippage ladder — the crossing is the slippage), the lock settles, the row
closes with the tick's IST timestamp. A crash between fill and settlement
is RESUMED from the ticket on the next tick, never re-issued. Unfilled →
cancelled, position kept.

EXPIRY BACKSTOP (`eod_sweep`, from plan_tracker.run_tracker): mirrors the
primary's `_expiry_backstop` byte-for-byte — intrinsic at the last close
on/before expiry, or after the grace window with no bars the defined max
loss at zero frictions.

Fail-open everywhere: a broken tick prints and returns; the live loop and
the primary account are never touched.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STATE_OPEN, STATE_EXITING, STATE_CLOSED = "open", "exiting", "closed"
CHAIN_PACE_SECONDS = 3.0                 # chain_archiver's measured endpoint limit
DEFAULT_CUTOFF = (15, 27)                # no new chain fetch at/after 15:27 IST
DEFAULT_MAX_FETCHES = 3                  # per tick
DEFAULT_BUDGET_SECONDS = 15.0            # wall-clock a tick may spend fetching
STALE_QUOTE_FRACTION = 0.5               # the #70 data-quality rule
TICK_FLOOR = 0.05
FILL_BASIS = "live_crossed"
EVENT_EXIT, EVENT_ENTRY_REFUSED, EVENT_UNFILLED = "live_exit", "live_entry_refused", "live_exit_unfilled"
EVENT_LOCK_NO_POSITION = "live_lock_released_no_position"
EVENT_LOCK_LATE = "live_lock_released_late"
EVENT_UNRECORDED = "live_position_unrecorded"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_live_positions (
    account_id        TEXT NOT NULL,
    journal_ref       TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    strategy          TEXT,
    direction         TEXT,
    expiry            TEXT NOT NULL,
    lots              INTEGER NOT NULL,
    lot_size          INTEGER NOT NULL,
    legs_json         TEXT NOT NULL,      -- [{side, option_type, strike, entry_fill}]
    entry_mark_ps     REAL NOT NULL,      -- d: sum(sign x fill), long +, short -
    width_ps          REAL NOT NULL,
    max_profit_ps     REAL NOT NULL,
    max_loss_ps       REAL NOT NULL,
    opened_at         TEXT NOT NULL,
    entry_quote_ts    TEXT,
    ratchet_peak_pct  REAL,
    ratchet_lock_pct  REAL,
    last_mark_ts      TEXT,
    last_mark_ps      REAL,
    last_profit_ps    REAL,
    last_capture_pct  REAL,
    quote_ts          TEXT,
    state             TEXT NOT NULL DEFAULT 'open',   -- open | exiting | closed
    exit_started_at   TEXT,
    exit_ticket_id    TEXT,
    exit_resolution   TEXT,               -- the predicate that fired (kept across a resume)
    last_exit_attempt_ts TEXT,            -- an unfilled exit is not retried inside one quote interval
    closed_at         TEXT,
    resolution        TEXT,
    exit_mark_ps      REAL,
    settlement_basis  TEXT,
    frictions_rs      REAL,
    pnl_net           REAL,
    PRIMARY KEY (account_id, journal_ref)
);
"""

# per-process chain cache: (ticker, expiry) -> (fetched_epoch, fetched_iso, chain)
_CHAIN_CACHE: dict = {}
_LAST_CHAIN_CALL = 0.0
# per-process ratchet rungs awaiting confirmation (audit F01):
# (account_id, journal_ref) -> {"src": the _CHAIN_CACHE entry the new rung
# was first read on, "capture": that read's floored capture}. Memory only,
# on purpose: a restart forgets a half-seen rung, so it must be seen twice
# again — the safe direction (no lock = no ratchet exit; #105 has no stop).
_PENDING_RUNG: dict = {}


def reset_cache() -> None:
    """Forget every per-process state — chain cache, pacing clock and the
    unconfirmed ratchet rungs — exactly what a restart does."""
    global _LAST_CHAIN_CALL
    _CHAIN_CACHE.clear()
    _PENDING_RUNG.clear()
    _LAST_CHAIN_CALL = 0.0


def _now() -> datetime:
    return datetime.now(IST)


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def _is_test_env() -> bool:
    """Same parse as notifier / equity_desk: only an explicit truthy value."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or \
        str(os.environ.get("IS_TEST_ENV") or "").strip().lower() in ("1", "true", "yes")


def _pace(sleep_fn=time.sleep, now_epoch_fn=time.time) -> None:
    """Keep the tick's own chain calls >= CHAIN_PACE_SECONDS apart, on the
    tick's injectable clock (its fetch budget is counted on it). The
    host-wide guarantee — every chain call on the box, the proposer's too —
    is dhan_client's chain lane."""
    global _LAST_CHAIN_CALL
    wait = CHAIN_PACE_SECONDS - (now_epoch_fn() - _LAST_CHAIN_CALL)
    if wait > 0:
        sleep_fn(wait)
    _LAST_CHAIN_CALL = now_epoch_fn()


class ChainUnavailable(Exception):
    """The live door answered no chain; the message is dhan_client's reason."""


def _default_chain(ticker: str, expiry: str):
    """The one live data door. Paced host-wide by dhan_client's chain lane
    (2026-10-01: this module's own pacer could not see the proposer's fetch
    of the same chain ~2 s earlier). An empty answer raises ChainUnavailable
    carrying the door's reason. Muzzled under pytest (Issue 29 discipline):
    a test that wants a chain injects `chain_fn`."""
    if _is_test_env():
        return None
    from src.dhan_client import get_option_chain, last_chain_error
    chain = get_option_chain(ticker, expiry)
    if not chain:
        raise ChainUnavailable(last_chain_error() or "empty response")
    return chain


def _market_open(now: datetime) -> bool:
    from src.live_bridge import is_market_open
    return bool(is_market_open(now))


def ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _accounts():
    from src import portfolio_manager as pm
    return pm.LIVE_ACCOUNTS


# ------------------------------------------------------------- quotes

def _sign(side: str) -> float:
    return 1.0 if str(side).upper() == "BUY" else -1.0


def _node(chain: dict, strike: float, option_type: str) -> dict:
    oc = (chain or {}).get("oc") or {}
    node = oc.get(f"{float(strike):.6f}") or oc.get(str(strike)) or oc.get(str(float(strike))) or {}
    return node.get(str(option_type).lower()) or {}


def _f(v):
    try:
        v = float(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def leg_quote(chain: dict, leg: dict) -> dict:
    """{bid, ask, ltp} for one leg (None where absent/non-positive)."""
    n = _node(chain, leg["strike"], leg["option_type"])
    return {"bid": _f(n.get("top_bid_price")), "ask": _f(n.get("top_ask_price")),
            "ltp": _f(n.get("last_price"))}


def crossed_close_price(leg: dict, q: dict) -> tuple:
    """(price, reason) to CLOSE one open leg now: a long is sold at the bid
    (no bid → 0.0, it cannot be sold — the structure still marks), a short
    is bought back at the ask (no ask → abstain). Abstains on a crossed
    book (bid > ask) or a quote > 50% off the last price."""
    bid, ask, ltp = q.get("bid"), q.get("ask"), q.get("ltp")
    if bid is not None and ask is not None and bid > ask:
        return None, "crossed book (bid > ask)"
    if _sign(leg["side"]) > 0:
        if bid is None:
            return 0.0, None
        price = bid
    else:
        if ask is None:
            return None, "no ask to buy the short leg back"
        price = ask
    if ltp is not None and abs(price - ltp) > STALE_QUOTE_FRACTION * ltp:
        return None, f"quote {price:g} is >50% off last {ltp:g}"
    return price, None


def crossed_open_price(leg: dict, q: dict) -> tuple:
    """(price, reason) to OPEN one leg now: BUY at the ask, SELL at the bid
    (the #70 rule); every leg needs a positive quoted side."""
    bid, ask, ltp = q.get("bid"), q.get("ask"), q.get("ltp")
    if bid is not None and ask is not None and bid > ask:
        return None, "crossed book (bid > ask)"
    price = ask if _sign(leg["side"]) > 0 else bid
    if price is None:
        return None, f"no {'ask' if _sign(leg['side']) > 0 else 'bid'} for the {leg['side']} leg"
    if ltp is not None and abs(price - ltp) > STALE_QUOTE_FRACTION * ltp:
        return None, f"quote {price:g} is >50% off last {ltp:g}"
    return price, None


def crossed_mark(chain: dict, legs: list) -> dict:
    """{ok, mark_ps, prices: {(strike, type): price}, reason}."""
    prices, mark = {}, 0.0
    for leg in legs:
        price, why = crossed_close_price(leg, leg_quote(chain, leg))
        if price is None:
            return {"ok": False, "mark_ps": None, "prices": {}, "reason": why}
        prices[(float(leg["strike"]), str(leg["option_type"]).upper())] = price
        mark += _sign(leg["side"]) * price
    return {"ok": True, "mark_ps": round(mark, 4), "prices": prices, "reason": None}


# ------------------------------------------------------------- structure math

def structure_bounds(spread: dict, d: float) -> dict:
    """{width_ps, max_profit_ps, max_loss_ps} from the structure's width and
    the ACTUAL crossed entry d (debit > 0, credit < 0)."""
    lot = int(spread.get("lot_size") or 0)
    width = spread.get("spread_width")
    if width is None and lot:
        width = (float(spread.get("max_loss") or 0) + float(spread.get("max_profit") or 0)) / lot
    width = float(width or 0.0)
    if d > 0:
        return {"width_ps": width, "max_profit_ps": round(width - d, 4), "max_loss_ps": round(d, 4)}
    return {"width_ps": width, "max_profit_ps": round(-d, 4), "max_loss_ps": round(width + d, 4)}


def _clamp(profit_ps: float, max_loss_ps: float, max_profit_ps: float) -> float:
    return max(-max_loss_ps, min(profit_ps, max_profit_ps))


def _capture(profit_ps: float, max_profit_ps: float) -> float:
    return (profit_ps / max_profit_ps * 100.0) if max_profit_ps > 0 else 0.0


def impossible_mark_reason(row: dict, mark_ps: float):
    """The named reason a crossed CLOSE mark cannot be a real price, or None.

    Crossing sells every long at its bid and buys every short back at its
    ask, so a crossed mark is at or BELOW the structure's true value — and
    true value never exceeds d + max_profit: the width for a debit
    structure, 0 for a credit one (a short condor or butterfly never PAYS
    you to close it). A mark above that bound by more than a tick is a bad
    print (a stale bid on an untraded strike, a torn opening book), not a
    100% capture. Audit F01: the clamp used to turn it into one, which
    latched the ratchet at lock 70 from one snapshot, fired a condor's
    profit take, and booked full max profit on a pre-expiry exit. The loss
    side has no twin here: a crossed mark far BELOW value is a wide book,
    not an impossible one, and `_exit` holds any exit beyond max loss."""
    bound = float(row["entry_mark_ps"]) + float(row["max_profit_ps"])
    if float(mark_ps) > bound + TICK_FLOOR + 1e-9:
        return f"impossible mark {float(mark_ps):g} above the structure's upper bound {round(bound, 4):g}"
    return None


# ------------------------------------------------------------- entry

def requote_entry(entry: dict, now: datetime = None, chain_fn=None) -> dict:
    """Fresh crossed OPEN prices for every leg at approval time. Returns
    {ok, legs (with premium = crossed price, fill_basis 'live_crossed'),
    quote_ts, reason}. Refuses when the market is closed or any leg lacks a
    usable quote — the account only takes trades it could really fill."""
    now = now or _now()
    if not _market_open(now):
        return {"ok": False, "legs": None, "quote_ts": None, "reason": "market closed at approval"}
    spread = entry["spread"]
    try:
        chain, why = (chain_fn or _default_chain)(entry["ticker"], spread["expiry"]), None
    except ChainUnavailable as exc:
        chain, why = None, str(exc)
    if not chain:
        return {"ok": False, "legs": None, "quote_ts": None,
                "reason": "option chain unavailable" + (f" ({why})" if why else "")}
    legs = []
    for leg in spread["legs"]:
        price, why = crossed_open_price(leg, leg_quote(chain, leg))
        if price is None:
            return {"ok": False, "legs": None, "quote_ts": None,
                    "reason": f"{leg['side']} {leg['strike']:g}{leg['option_type']}: {why}"}
        legs.append(dict(leg, premium=round(price, 2), fill_basis=FILL_BASIS, requoted_from=leg.get("premium")))
    d = round(sum(_sign(l["side"]) * float(l["premium"]) for l in legs), 4)
    b = structure_bounds(spread, d)
    if b["max_profit_ps"] <= 0 or b["max_loss_ps"] <= 0 or b["max_loss_ps"] >= b["width_ps"] > 0:
        return {"ok": False, "legs": None, "quote_ts": None,
                "reason": (f"crossed prices leave no trade: net {d:+.2f}/share on a {b['width_ps']:g}-wide "
                           f"structure (max profit {b['max_profit_ps']:.2f}, max loss {b['max_loss_ps']:.2f})")}
    return {"ok": True, "legs": legs, "quote_ts": _iso(now), "reason": None, "entry_mark_ps": d, **b}


def open_position(conn, account: str, entry: dict, ticket_view: dict, quote_ts: str = None,
                  now: datetime = None) -> dict:
    """Record the account's FILLED entry ticket as an open live position."""
    ensure_schema(conn)
    now = now or _now()
    spread = entry["spread"]
    legs, d = [], 0.0
    for l in ticket_view.get("legs") or []:
        fill = float(l["avg_fill_price"])
        legs.append({"side": str(l["side"]).upper(), "option_type": str(l.get("option_type") or "").upper(),
                     "strike": float(l["strike"]), "entry_fill": fill})
        d += _sign(l["side"]) * fill
    if not legs:
        raise ValueError("ticket has no filled legs")
    d = round(d, 4)
    b = structure_bounds(spread, d)
    conn.execute("INSERT OR REPLACE INTO paper_live_positions (account_id, journal_ref, ticker, strategy, "
                 "direction, expiry, lots, lot_size, legs_json, entry_mark_ps, width_ps, max_profit_ps, "
                 "max_loss_ps, opened_at, entry_quote_ts, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (account, entry.get("short_id"), entry["ticker"], spread.get("strategy"),
                  spread.get("direction"), spread["expiry"], int(ticket_view.get("lots") or spread.get("lots") or 1),
                  int(spread["lot_size"]), json.dumps(legs), d, b["width_ps"], b["max_profit_ps"],
                  b["max_loss_ps"], _iso(now), quote_ts, STATE_OPEN))
    conn.commit()
    return {"journal_ref": entry.get("short_id"), "entry_mark_ps": d, **b, "legs": legs}


def has_open_position(conn, account: str, journal_ref: str) -> bool:
    """True while an OPEN/EXITING row exists. RAISES on a database error —
    the caller must treat 'unknown' as held, never as 'no position'."""
    ensure_schema(conn)
    row = conn.execute("SELECT 1 FROM paper_live_positions WHERE account_id = ? AND journal_ref = ? "
                       "AND state != ?", (account, journal_ref, STATE_CLOSED)).fetchone()
    return row is not None


def closed_pnl(conn, account: str, journal_ref: str):
    """The settled pnl_net of a CLOSED row, or None (no row / still open)."""
    ensure_schema(conn)
    row = conn.execute("SELECT pnl_net FROM paper_live_positions WHERE account_id = ? AND journal_ref = ? "
                       "AND state = ?", (account, journal_ref, STATE_CLOSED)).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def open_rows(conn, account: str = None) -> list:
    ensure_schema(conn)
    cols = ("account_id", "journal_ref", "ticker", "strategy", "direction", "expiry", "lots", "lot_size",
            "legs_json", "entry_mark_ps", "width_ps", "max_profit_ps", "max_loss_ps", "opened_at",
            "entry_quote_ts", "ratchet_peak_pct", "ratchet_lock_pct", "last_mark_ts", "last_mark_ps",
            "last_profit_ps", "last_capture_pct", "quote_ts", "state", "exit_started_at", "exit_ticket_id",
            "exit_resolution", "last_exit_attempt_ts")
    sql = f"SELECT {', '.join(cols)} FROM paper_live_positions WHERE state != ?"
    args = [STATE_CLOSED]
    if account:
        sql += " AND account_id = ?"
        args.append(account)
    out = []
    for r in conn.execute(sql + " ORDER BY opened_at, rowid", args).fetchall():
        row = dict(zip(cols, tuple(r)))
        row["legs"] = json.loads(row.pop("legs_json"))
        out.append(row)
    return out


def positions(conn, include_closed: bool = True) -> list:
    """Every row (for the dashboard / ledger), newest first."""
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM paper_live_positions" + ("" if include_closed else " WHERE state != 'closed'")
                        + " ORDER BY opened_at DESC, rowid DESC").fetchall()
    cols = [c[1] for c in conn.execute("PRAGMA table_info(paper_live_positions)").fetchall()]
    out = []
    for r in rows:
        row = dict(zip(cols, tuple(r)))
        row["legs"] = json.loads(row.pop("legs_json"))
        out.append(row)
    return out


# ------------------------------------------------------------- settlement

def _entry_like(row: dict) -> dict:
    """The tracker-shaped entry the exit door needs, built from the row so
    it works whatever the journal row says now."""
    lot = int(row["lot_size"])
    return {"short_id": row["journal_ref"], "ticker": row["ticker"], "date": row["opened_at"][:10],
            "spread": {"strategy": row["strategy"], "direction": row["direction"], "expiry": row["expiry"],
                       "lot_size": lot, "lots": int(row["lots"]),
                       "legs": [{"side": l["side"], "option_type": l["option_type"], "strike": l["strike"],
                                 "premium": l["entry_fill"], "fill_basis": FILL_BASIS} for l in row["legs"]],
                       "max_loss": round(float(row["max_loss_ps"]) * lot, 2),
                       "max_profit": round(float(row["max_profit_ps"]) * lot, 2),
                       "spread_width": row["width_ps"]}}


def _frictions(row: dict, exit_prices: dict | None) -> float:
    """The 2026 friction stack on every entry leg and (when priced) every
    exit leg. No slippage ladder: the crossing is the slippage."""
    from src import portfolio as pf
    qty = int(row["lots"]) * int(row["lot_size"])
    total = 0.0
    for l in row["legs"]:
        total += pf.calculate_trade_frictions("OPTION", l["side"], float(l["entry_fill"]), qty)
        if exit_prices is not None:
            exit_side = "SELL" if l["side"] == "BUY" else "BUY"
            px = float(exit_prices.get((float(l["strike"]), l["option_type"]), 0.0))
            total += pf.calculate_trade_frictions("OPTION", exit_side, px, qty)
    return round(total, 2)


def _settle(conn, row: dict, exit_mark_ps: float, resolution: str, basis: str, frictions: float,
            now: datetime, ticket_id: str = None, detail: dict = None) -> dict:
    """Close the row and settle ONLY this account's lock. Idempotent: an
    already-closed row settles nothing."""
    from src import portfolio_manager as pm
    qty = int(row["lots"]) * int(row["lot_size"])
    d = float(row["entry_mark_ps"])
    profit_ps = _clamp(float(exit_mark_ps) - d, float(row["max_loss_ps"]), float(row["max_profit_ps"]))
    pnl = round(profit_ps * qty - float(frictions), 2)
    # Everything that may commit or write runs BEFORE the transaction opens
    # (audit Chunk 1 D4, decision #122): the schema check, and the halt
    # read that can latch a halt row. Inside it: only plain statements.
    pm.ensure_accounts_schema(conn)
    if conn.in_transaction:
        conn.commit()
    was_halted = pm.paper_trading_halted(conn, row["account_id"])
    cur = conn.execute("UPDATE paper_live_positions SET state = ?, closed_at = ?, resolution = ?, "
                       "exit_mark_ps = ?, settlement_basis = ?, frictions_rs = ?, pnl_net = ?, "
                       "exit_ticket_id = COALESCE(?, exit_ticket_id), last_profit_ps = ?, "
                       "last_capture_pct = ?, last_mark_ps = ?, last_mark_ts = ? "
                       "WHERE account_id = ? AND journal_ref = ? AND state != ?",
                       (STATE_CLOSED, _iso(now), resolution, round(float(exit_mark_ps), 4), basis,
                        float(frictions), pnl, ticket_id, round(profit_ps, 4),
                        round(_capture(profit_ps, float(row["max_profit_ps"])), 2),
                        round(float(exit_mark_ps), 4), _iso(now),
                        row["account_id"], row["journal_ref"], STATE_CLOSED))
    if cur.rowcount == 0:
        conn.rollback()
        return {"status": "already_closed", "journal_ref": row["journal_ref"]}
    # The row close above is NOT committed yet. The lock release and the
    # account's P&L join it as plain statements (no DDL, no commit inside),
    # and ONE commit lands all three; a failure anywhere rolls the row back
    # to where it was, so the next tick retries — never a closed row with a
    # live lock. (Before #122 a schema executescript inside the release
    # committed the row close on its own: the "one transaction" was two.)
    try:
        lock_released = pm.paper_release_rows(conn, row["account_id"], row["journal_ref"], pnl)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    released = {"released": False}
    if lock_released:
        try:
            released = pm.paper_after_release(conn, row["account_id"], row["journal_ref"], pnl,
                                              was_halted)
        except Exception as exc:    # the money is settled; a curve point is not
            print(f"  (live account: post-settle record skipped for {row['journal_ref']}: {exc})")
            released = {"released": True}
    payload = {"resolution": resolution, "basis": basis, "closed_at": _iso(now), "ticker": row["ticker"],
               "strategy": row["strategy"], "lots": row["lots"], "entry_mark_ps": d,
               "exit_mark_ps": round(float(exit_mark_ps), 4), "profit_ps": round(profit_ps, 4),
               "capture_pct": round(_capture(profit_ps, float(row["max_profit_ps"])), 2),
               "frictions_rs": float(frictions), "pnl_net": pnl, "ticket_id": ticket_id,
               "lock_released": bool(released.get("released")), **(detail or {})}
    pm.paper_log_event(conn, row["account_id"], EVENT_EXIT, row["journal_ref"], json.dumps(payload, sort_keys=True))
    _stamp_journal(row, payload)
    return {"status": "settled", **payload}


def _stamp_journal(row: dict, payload: dict) -> None:
    """Best-effort: the journal row's account verdict shows the close.
    Muzzled under pytest (the real journal is never touched by a test)."""
    if _is_test_env():
        return
    try:
        from src import journal

        def _mutate(e):
            acc = e.get("accounts")
            if not isinstance(acc, dict) or not isinstance(acc.get(row["account_id"]), dict):
                return False
            acc[row["account_id"]].update({"status": "closed", "closed_at": payload["closed_at"],
                                           "resolution": payload["resolution"], "pnl_rs": payload["pnl_net"]})
            return True
        journal.update_entry(row["journal_ref"], _mutate)
    except Exception as exc:
        print(f"  (live account: journal stamp skipped for {row['journal_ref']}: {exc})")


def _find_exit_ticket(conn, row: dict):
    """The EXIT ticket this row's exit issued (for resume), or None."""
    from src import oms
    oms.ensure_schema(conn)
    if row.get("exit_ticket_id"):
        return oms.ticket_view(conn, row["exit_ticket_id"])
    since = row.get("exit_started_at") or row["opened_at"]
    t = conn.execute("SELECT ticket_id FROM trade_tickets WHERE account_id = ? AND journal_ref = ? "
                     "AND issued_at >= ? AND note LIKE 'EXIT %' ORDER BY issued_at DESC LIMIT 1",
                     (row["account_id"], row["journal_ref"], since)).fetchone()
    return oms.ticket_view(conn, t[0]) if t else None


def _mark_from_ticket(view: dict) -> tuple:
    """(exit_mark_ps, prices) from a FILLED exit ticket: the exit side is the
    flipped one, so a SELL fill closes a long (+), a BUY closes a short (−)."""
    mark, prices = 0.0, {}
    for l in view.get("legs") or []:
        fill = float(l["avg_fill_price"])
        key = (float(l["strike"]), str(l.get("option_type") or "").upper())
        prices[key] = fill
        mark += fill if str(l["side"]).upper() == "SELL" else -fill
    return round(mark, 4), prices


def _resume_exiting(conn, row: dict, now: datetime) -> dict:
    """A row left in `exiting` by a crash: settle from a FILLED ticket,
    cancel an unfilled one and reopen. Never issues a new ticket."""
    from src import oms
    view = _find_exit_ticket(conn, row)
    if view and view.get("status") == oms.FILLED:
        mark, prices = _mark_from_ticket(view)
        note = str(view.get("note") or "")
        resolution = row.get("exit_resolution") or (note[5:].strip() if note.startswith("EXIT ") else "") \
            or "resumed_exit"
        return _settle(conn, row, mark, resolution, "live_bid_ask", _frictions(row, prices), now,
                       ticket_id=view["ticket_id"], detail={"resumed": True})
    if view and view.get("status") not in (oms.CANCELLED, oms.REJECTED):
        oms.cancel_ticket(conn, view["ticket_id"], "live account: exit resumed unfilled")
    conn.execute("UPDATE paper_live_positions SET state = ?, exit_started_at = NULL, exit_ticket_id = NULL "
                 "WHERE account_id = ? AND journal_ref = ?", (STATE_OPEN, row["account_id"], row["journal_ref"]))
    conn.commit()
    return {"status": "reopened", "journal_ref": row["journal_ref"]}


def _exit(conn, row: dict, prices: dict, resolution: str, now: datetime, venue_mod=None,
          interval_s: int = 300) -> dict:
    """Issue + fill ONE exit ticket for this account, settle on the crossed
    quotes. The row is stamped `exiting` (with the predicate) BEFORE the
    door is called; a door that lost track of its ticket leaves the row in
    `exiting` for the next tick's resume — it is never reopened blind."""
    from src import oms, plan_tracker as pt, portfolio_manager as pm
    from src.config import PAPER_VENUE_ENABLED
    d = float(row["entry_mark_ps"])
    would = round(sum(_sign(l["side"]) * prices[(float(l["strike"]), l["option_type"])] for l in row["legs"]), 4)
    if would - d < -float(row["max_loss_ps"]) - 1e-9:
        return {"status": "held_loss_beyond_max", "journal_ref": row["journal_ref"],
                "would_loss_ps": round(d - would, 4), "max_loss_ps": row["max_loss_ps"]}
    # The profit-side twin (audit F01): evaluate already abstains on such a
    # mark, but this is the door that books money, so it refuses on its own
    # rather than let _settle clamp an impossible print into max profit.
    impossible = impossible_mark_reason(row, would)
    if impossible:
        return {"status": "held_impossible_mark", "journal_ref": row["journal_ref"], "reason": impossible}
    last = row.get("last_exit_attempt_ts")
    if last:
        try:
            if (now.replace(tzinfo=None) - datetime.fromisoformat(last)).total_seconds() < interval_s:
                return {"status": "held_recent_attempt", "journal_ref": row["journal_ref"], "last": last}
        except ValueError:
            pass
    floored = {k: v for k, v in prices.items() if float(v) < TICK_FLOOR}
    limits = {k: max(TICK_FLOOR, float(v)) for k, v in prices.items()}     # the venue rejects a 0 limit
    frictions = _frictions(row, prices)
    detail = {"quote_ts": row.get("quote_ts"), "crossed_mark_ps": would}
    if floored:
        detail["limit_floored"] = {f"{k[0]:g}{k[1]}": TICK_FLOOR for k in floored}
    if not PAPER_VENUE_ENABLED:
        # no paper venue: the crossed quotes ARE the fill; settle without a ticket
        return _settle(conn, row, would, resolution, "live_bid_ask_no_venue", frictions, now,
                       detail=detail)
    conn.execute("UPDATE paper_live_positions SET state = ?, exit_started_at = ?, exit_resolution = ?, "
                 "last_exit_attempt_ts = ? WHERE account_id = ? AND journal_ref = ? AND state = ?",
                 (STATE_EXITING, _iso(now), resolution, _iso(now), row["account_id"], row["journal_ref"],
                  STATE_OPEN))
    conn.commit()
    rec = pt._execute_paper_exit(_entry_like(row), limits, resolution, conn=conn, venue_mod=venue_mod,
                                 today=now.date(), accounts=[(row["account_id"], int(row["lots"]))])
    tid = rec.get("ticket_id")
    if tid:
        conn.execute("UPDATE paper_live_positions SET exit_ticket_id = ? WHERE account_id = ? AND journal_ref = ?",
                     (tid, row["account_id"], row["journal_ref"]))
        conn.commit()
    if rec.get("mode") == "paper_venue" and rec.get("status") == oms.FILLED:
        view = oms.ticket_view(conn, tid) or {}
        _, fills = _mark_from_ticket(view)
        detail["fills"] = {f"{k[0]:g}{k[1]}": v for k, v in fills.items()}
        # settle on the crossed quotes (the venue filled at exactly them; a
        # floored zero-bid leg is a venue constraint, not a price)
        return _settle(conn, row, would, resolution, "live_bid_ask", frictions, now, ticket_id=tid,
                       detail=detail)
    if rec.get("error") and not tid:
        # the door raised after it may have issued: leave `exiting`; the
        # next tick resumes from whatever ticket exists (or reopens if none)
        pm.paper_log_event(conn, row["account_id"], EVENT_UNFILLED, row["journal_ref"],
                           f"{resolution}: exit door error ({rec.get('error')}) — row left for resume")
        return {"status": "exit_error", "journal_ref": row["journal_ref"], "reason": rec.get("error")}
    if tid:
        try:
            oms.cancel_ticket(conn, tid, "live account: exit not filled")
        except Exception:
            pass
    conn.execute("UPDATE paper_live_positions SET state = ?, exit_started_at = NULL, exit_ticket_id = NULL "
                 "WHERE account_id = ? AND journal_ref = ?", (STATE_OPEN, row["account_id"], row["journal_ref"]))
    conn.commit()
    pm.paper_log_event(conn, row["account_id"], EVENT_UNFILLED, row["journal_ref"],
                       f"{resolution}: exit ticket {tid or '-'} not filled ({rec.get('status')} "
                       f"{rec.get('error') or ''}) — position kept, next attempt after {interval_s}s")
    return {"status": "unfilled", "journal_ref": row["journal_ref"], "ticket_id": tid,
            "reason": rec.get("error") or rec.get("status")}


def _repair_late_locks(conn, account: str) -> list:
    """A CLOSED row whose lock is still active (a crash between the row
    write and the lock settle, before both became one transaction):
    release at the row's settled pnl now, not only at the primary's exit."""
    from src import portfolio_manager as pm
    ensure_schema(conn)
    rows = conn.execute("SELECT p.journal_ref, p.pnl_net FROM paper_live_positions p JOIN paper_margin_locks l "
                        "ON l.account_id = p.account_id AND l.journal_ref = p.journal_ref "
                        "WHERE p.account_id = ? AND p.state = ? AND l.released_at IS NULL AND p.pnl_net IS NOT NULL",
                        (account, STATE_CLOSED)).fetchall()
    fixed = []
    for ref, pnl in (tuple(r) for r in rows):
        rel = pm.paper_release_margin(conn, account, ref, float(pnl))
        if rel.get("released"):
            pm.paper_log_event(conn, account, EVENT_LOCK_LATE, ref,
                               f"lock released at the row's settled pnl Rs.{float(pnl):,.2f} on the next tick")
            fixed.append(ref)
    return fixed


def _repair_unrecorded(conn, account: str, now: datetime) -> list:
    """A FILLED entry ticket with a live lock but no position row (the row
    insert failed at approval): open the position from the ticket."""
    from src import oms
    ensure_schema(conn)
    fixed = []
    locks = conn.execute("SELECT journal_ref FROM paper_margin_locks WHERE account_id = ? AND released_at IS NULL",
                         (account,)).fetchall()
    for (ref,) in (tuple(r) for r in locks):
        if conn.execute("SELECT 1 FROM paper_live_positions WHERE account_id = ? AND journal_ref = ?",
                        (account, ref)).fetchone():
            continue
        t = conn.execute("SELECT ticket_id FROM trade_tickets WHERE account_id = ? AND journal_ref = ? "
                         "AND note NOT LIKE 'EXIT %' AND status = ? ORDER BY issued_at DESC LIMIT 1",
                         (account, ref, oms.FILLED)).fetchone()
        if not t:
            continue
        view = oms.ticket_view(conn, t[0]) or {}
        legs = view.get("legs") or []
        by_type = {}
        for l in legs:
            by_type.setdefault(str(l.get("option_type") or "").upper(), []).append(float(l["strike"]))
        widths = [abs(v[0] - v[1]) for v in by_type.values() if len(v) == 2]
        entry_like = {"short_id": ref, "ticker": view.get("underlying"),
                      "spread": {"strategy": view.get("strategy"), "direction": view.get("direction"),
                                 "expiry": legs[0].get("expiry") if legs else None,
                                 "lot_size": view.get("lot_size"), "lots": view.get("lots"),
                                 "spread_width": max(widths) if widths else 0.0}}
        if not entry_like["spread"]["expiry"]:
            continue
        open_position(conn, account, entry_like, view, quote_ts=None, now=now)
        fixed.append(ref)
    return fixed


# ------------------------------------------------------------- the tick

def evaluate(row: dict, chain: dict, today: date) -> dict:
    """Pure: {ok, reason, mark_ps, profit_ps, capture_pct, peak, lock,
    unconfirmed, signal, prices}. `peak`/`lock` are what the record may
    keep from this read alone: a read that would raise the lock to a new
    rung leaves them at the record's confirmed values and names the raise
    in `unconfirmed` ({peak, lock, capture}) for tick's `_confirm_rung`
    (audit F01). `signal` is judged against the CONFIRMED lock only."""
    from src import plan_tracker as pt, profit_ratchet as pr
    m = crossed_mark(chain, row["legs"])
    if not m["ok"]:
        return {"ok": False, "reason": m["reason"], "signal": None}
    impossible = impossible_mark_reason(row, m["mark_ps"])
    if impossible:
        return {"ok": False, "reason": impossible, "signal": None}
    d, max_loss, max_profit = float(row["entry_mark_ps"]), float(row["max_loss_ps"]), float(row["max_profit_ps"])
    profit_ps = _clamp(m["mark_ps"] - d, max_loss, max_profit)
    capture = _capture(profit_ps, max_profit)
    peak, lock = row.get("ratchet_peak_pct"), row.get("ratchet_lock_pct")
    directional = pr.is_directional({"strategy": row.get("strategy"), "direction": row.get("direction")})
    signal, unconfirmed = "hold", None
    if directional and max_profit > 0:
        confirmed_lock = pr.state(peak, lock)["locked_pct"]
        new_peak = capture if peak is None else max(float(peak), capture)
        new_lock = pr.state(new_peak, lock)["locked_pct"]
        if new_lock is not None and (confirmed_lock is None or new_lock > confirmed_lock):
            # A NEW RUNG from this one read (audit F01): the record keeps its
            # confirmed peak/lock until a second, later fetch agrees. The
            # lock only ever rises, so one bad print would otherwise be
            # permanent — and the next ordinary mark below it is a forced
            # exit #105 forbids on a trade that never really armed.
            unconfirmed = {"peak": pr.floor2(new_peak), "lock": new_lock, "capture": pr.floor2(capture)}
            lock = confirmed_lock
        else:
            peak, lock = new_peak, new_lock
        if pr.ratchet_hit(capture, lock):
            signal = "ratchet_hit"
    elif max_profit > 0 and profit_ps >= pt.OPTION_PROFIT_TAKE_FRACTION * max_profit:
        signal = "profit_take"
    days_left = (date.fromisoformat(row["expiry"]) - today).days
    if signal == "hold" and days_left <= pt._forced_exit_days(row["ticker"]):
        signal = "pre_expiry_exit"
    return {"ok": True, "reason": None, "mark_ps": m["mark_ps"], "profit_ps": round(profit_ps, 4),
            "capture_pct": round(capture, 2), "peak": (pr.floor2(peak) if peak is not None else None),  # floored (#122)
            "lock": lock, "unconfirmed": unconfirmed, "signal": signal, "prices": m["prices"],
            "days_left": days_left}


def _confirm_rung(row: dict, ev: dict, src) -> dict:
    """Audit F01: a ratchet lock rises only when TWO independently fetched
    chains both clear the new rung. `src` is the `_CHAIN_CACHE` entry `ev`
    was read on — the cache only ever moves forward, so a different entry
    is a later fetch, while the same entry re-read on the next 60-s tick is
    the SAME snapshot and confirms nothing. The two reads agree on
    min(first capture, this capture); the rung that clears is persisted
    (it may be lower than the one first seen). Reads must be consecutive:
    a usable read that implies no new rung forgets the earlier sighting.
    An abstaining read (bad quote, no chain) never reaches here — it is no
    evidence either way. Returns `ev`, promoted when confirmed; stamps
    `rung` = 'pending' | 'confirmed' for the tick summary."""
    from src import profit_ratchet as pr
    key = (row["account_id"], row["journal_ref"])
    seen = ev.get("unconfirmed")
    if seen is None:
        _PENDING_RUNG.pop(key, None)
        return ev
    first = _PENDING_RUNG.get(key)
    if first is None or first["src"] is src:
        _PENDING_RUNG.setdefault(key, {"src": src, "capture": seen["capture"]})
        return dict(ev, rung="pending")
    agreed = min(float(first["capture"]), float(seen["capture"]))
    peak0, lock0 = row.get("ratchet_peak_pct"), row.get("ratchet_lock_pct")
    peak = agreed if peak0 is None else max(float(peak0), agreed)
    lock = pr.state(peak, lock0)["locked_pct"]
    # Defensive: the two reads agree on no rung above the confirmed lock
    # (the record moved under the first sighting) — start over from this one.
    if lock is None or (ev["lock"] is not None and lock <= ev["lock"]):
        _PENDING_RUNG[key] = {"src": src, "capture": seen["capture"]}
        return dict(ev, rung="pending")
    if seen["lock"] > lock:
        # this read is higher still: it is the first sighting of THAT rung
        _PENDING_RUNG[key] = {"src": src, "capture": seen["capture"]}
    else:
        _PENDING_RUNG.pop(key, None)
    return dict(ev, peak=pr.floor2(peak), lock=lock, rung="confirmed")


def _record_mark(conn, row: dict, ev: dict, quote_ts: str, now: datetime) -> None:
    conn.execute("UPDATE paper_live_positions SET last_mark_ts = ?, last_mark_ps = ?, last_profit_ps = ?, "
                 "last_capture_pct = ?, quote_ts = ?, ratchet_peak_pct = ?, ratchet_lock_pct = ? "
                 "WHERE account_id = ? AND journal_ref = ? AND state = ?",
                 (_iso(now), ev["mark_ps"], ev["profit_ps"], ev["capture_pct"], quote_ts, ev["peak"], ev["lock"],
                  row["account_id"], row["journal_ref"], STATE_OPEN))
    conn.commit()


def _fetch(key: tuple, chain_fn, sleep_fn, now_epoch_fn) -> tuple | None:
    """One paced chain fetch; returns (iso, chain) or None."""
    _pace(sleep_fn, now_epoch_fn)
    try:
        chain = chain_fn(key[0], key[1])
    except Exception as exc:
        print(f"  (live account: chain {key[0]} {key[1]} failed: {exc})")
        chain = None
    if not chain:
        return None
    stamp = datetime.fromtimestamp(now_epoch_fn(), IST)
    _CHAIN_CACHE[key] = (now_epoch_fn(), _iso(stamp), chain)
    return _CHAIN_CACHE[key][1], chain


def tick(now: datetime = None, conn=None, chain_fn=None, sleep_fn=time.sleep, now_epoch_fn=time.time,
         interval_s: int = None, max_fetches: int = DEFAULT_MAX_FETCHES,
         budget_s: float = DEFAULT_BUDGET_SECONDS, cutoff: tuple = DEFAULT_CUTOFF,
         venue_mod=None, require_market_open: bool = True) -> dict:
    """One pass: resume half-done exits, refresh due chains (paced, capped,
    never after the cutoff), mark every open row, exit on a predicate
    (re-verified on a fresh chain when the mark came from the cache).
    Never raises. Returns a summary."""
    from src.config import LIVE_QUOTE_INTERVAL_SECONDS
    now = now or _now()
    interval_s = LIVE_QUOTE_INTERVAL_SECONDS if interval_s is None else int(interval_s)
    out = {"ts": _iso(now), "rows": 0, "fetched": 0, "marked": 0, "abstained": 0, "exits": [],
           "resumed": 0, "skipped": None}
    if require_market_open and not _market_open(now):
        out["skipped"] = "market closed"
        return out
    own = conn is None
    try:
        if own:
            from src import brain_map
            conn = brain_map.connect()
        chain_fn = chain_fn or _default_chain
        rows = []
        for acct in _accounts():
            try:
                fixed = _repair_unrecorded(conn, acct, now)
                if fixed:
                    out["repaired"] = fixed
                late = _repair_late_locks(conn, acct)
                if late:
                    out["late_released"] = late
            except Exception as exc:
                print(f"  (live account: repair pass failed: {exc})")
            rows.extend(open_rows(conn, acct))
        out["rows"] = len(rows)
        for row in [r for r in rows if r["state"] == STATE_EXITING]:
            try:
                _resume_exiting(conn, row, now)
                out["resumed"] += 1
            except Exception as exc:
                print(f"  (live account: resume {row['journal_ref']} failed: {exc})")
        rows = [r for r in open_rows(conn) if r["state"] == STATE_OPEN]
        # chains due, stalest first, capped per tick and by the clock
        start = now_epoch_fn()
        past_cutoff = (now.hour, now.minute) >= cutoff
        keys = sorted({(r["ticker"], r["expiry"]) for r in rows},
                      key=lambda k: _CHAIN_CACHE.get(k, (0.0,))[0])
        fresh = set()
        for key in keys:
            age = now_epoch_fn() - _CHAIN_CACHE.get(key, (0.0,))[0]
            if key in _CHAIN_CACHE and age < interval_s:
                continue
            if past_cutoff or out["fetched"] >= max_fetches \
                    or now_epoch_fn() - start + CHAIN_PACE_SECONDS > budget_s:
                break
            if _fetch(key, chain_fn, sleep_fn, now_epoch_fn):
                fresh.add(key)
            out["fetched"] += 1
        for row in rows:
            try:
                key = (row["ticker"], row["expiry"])
                cached = _CHAIN_CACHE.get(key)
                if not cached:
                    out["abstained"] += 1
                    continue
                src = cached                 # which fetch this read is on (F01 rung confirmation)
                _, quote_ts, chain = cached
                ev = evaluate(row, chain, now.date())
                if not ev["ok"]:
                    out["abstained"] += 1
                    continue
                if ev["signal"] != "hold" and key not in fresh:
                    # A predicate that fired on a CACHED chain acts only on a
                    # FRESH one. Past the cutoff, over budget, or with the
                    # fetch failing, the mark is recorded and the signal is
                    # forced to hold — the next tick's fresh chain decides.
                    got = None
                    if not past_cutoff and out["fetched"] < max_fetches \
                            and now_epoch_fn() - start + CHAIN_PACE_SECONDS <= budget_s:
                        got = _fetch(key, chain_fn, sleep_fn, now_epoch_fn)
                        out["fetched"] += 1
                    if got:
                        fresh.add(key)
                        quote_ts, chain = got
                        src = _CHAIN_CACHE.get(key)
                        ev2 = evaluate(row, chain, now.date())
                        if not ev2["ok"]:
                            out["abstained"] += 1
                            continue
                        ev = ev2
                    else:
                        ev = dict(ev, signal="hold")
                        out["unconfirmed"] = out.get("unconfirmed", 0) + 1
                # A new ratchet rung is persisted only on a second, later
                # fetch (F01); the mark itself is recorded either way.
                ev = _confirm_rung(row, ev, src)
                if ev.get("rung"):
                    out[f"rung_{ev['rung']}"] = out.get(f"rung_{ev['rung']}", 0) + 1
                _record_mark(conn, row, ev, quote_ts, now)
                out["marked"] += 1
                if ev["signal"] != "hold":
                    row = dict(row, ratchet_peak_pct=ev["peak"], ratchet_lock_pct=ev["lock"], quote_ts=quote_ts)
                    res = _exit(conn, row, ev["prices"], ev["signal"], now, venue_mod=venue_mod,
                                interval_s=interval_s)
                    out["exits"].append({"journal_ref": row["journal_ref"], "signal": ev["signal"], **res})
            except Exception as exc:
                print(f"  (live account: {row.get('journal_ref')} tick failed: {exc})")
        open_keys = {(r["account_id"], r["journal_ref"]) for r in rows}
        for k in [k for k in _PENDING_RUNG if k not in open_keys]:
            _PENDING_RUNG.pop(k, None)              # a closed position's sighting is moot
        return out
    except Exception as exc:
        print(f"  (live account tick skipped: {exc})")
        out["skipped"] = str(exc)
        return out
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ------------------------------------------------------------- expiry backstop

def eod_sweep_standalone(today: date = None) -> dict:
    """run_tracker's hook: its own connection, muzzled under pytest (a test
    calls eod_sweep with its own conn). Never raises."""
    if _is_test_env():
        return {"skipped": "test env"}
    try:
        from src import brain_map
        conn = brain_map.connect()
        try:
            return eod_sweep(conn, today=today)
        finally:
            conn.close()
    except Exception as exc:
        print(f"  (live account sweep skipped: {exc})")
        return {"skipped": str(exc)}


def eod_sweep(conn, today: date = None, bars_fn=None, now: datetime = None) -> dict:
    """Settle open rows whose expiry has passed, exactly like the primary's
    `_expiry_backstop`: intrinsic at the last close on/before expiry, or —
    past the grace window with no bars — the defined max loss at zero
    frictions. Never raises."""
    from src import plan_tracker as pt
    today = today or _now().date()
    now = now or _now()
    out = {"settled": [], "waiting": [], "errors": []}
    try:
        rows = [r for r in open_rows(conn) if r["state"] == STATE_OPEN and r["expiry"] < today.isoformat()]
    except Exception as exc:
        out["errors"].append(str(exc))
        return out
    for row in rows:
        try:
            bars = None
            try:
                bars = (bars_fn or pt._daily_bars)(row["ticker"], row["opened_at"][:10])
            except Exception as exc:
                print(f"  (live account: bars for {row['ticker']} unavailable: {exc})")
            res = pt._expiry_backstop(_entry_like(row), bars or [], today)
            if res is None:
                out["waiting"].append(row["journal_ref"])
                continue
            resolution, exit_mark_ps, _frac, _day, close, basis, _close_day = res
            if basis == "no_price_data_max_loss":
                frictions = 0.0
            else:
                prices = {(float(l["strike"]), l["option_type"]):
                          max(0.0, (float(close) - float(l["strike"])) if l["option_type"] == "CE"
                              else (float(l["strike"]) - float(close))) for l in row["legs"]}
                frictions = _frictions(row, prices)
            out["settled"].append(_settle(conn, row, exit_mark_ps, resolution, basis, frictions, now,
                                          detail={"close": close}))
        except Exception as exc:
            out["errors"].append(f"{row['journal_ref']}: {exc}")
    return out

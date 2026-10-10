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
width − d (a debit structure) or −d (a credit one), max_loss = d (debit) or
width + d (credit) — debit/credit being the STRUCTURE's, never d's sign
(audit F11, below).

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

NOTHING QUIET IS SILENT (audit F02, 2026-10-05). The tick's summary names
every row it did not mark or exit: `row_notes` carries each abstention
and each cached-chain predicate it could not re-verify (`unconfirmed`)
with the row, the reason (the leg is named) and days to expiry; `exits`
carries each exit outcome, holds included. live_bridge.live_cycle prints
it (de-duplicated). Two of those states also write ONE
`paper_account_events` row per position per day, because they are the
ones that silently cost the pre-expiry exit: a held exit
(`live_exit_held`) and an abstention inside the forced-exit window
(`live_mark_abstained`). No Discord card (the #102/#120 telemetry rule).

EXIT EXECUTION: the row is stamped `exiting` FIRST, then ONE EXIT ticket
for this account alone goes through plan_tracker._execute_paper_exit (the
file's single OMS exit door), the venue fills at the crossed limits, P&L =
(exit − d) × qty − the 2026 friction stack on every entry and exit leg (no
slippage ladder — the crossing is the slippage), the lock settles, the row
closes with the tick's IST timestamp. A crash between fill and settlement
is RESUMED from the ticket on the next tick, never re-issued. Unfilled →
cancelled, position kept.

ONE ACTOR PER EXIT, AND WHAT FILLED IS WHAT COUNTS (audit F05 + F06,
2026-10-06). The `exiting` stamp is a compare-and-set on `state = 'open'`:
when it matches no row, another actor owns (or finished) this exit, and
`_exit` returns `exit_not_owned` — no ticket, no fill. Every statement
that puts a row back to `open` is a compare-and-set on the attempt it
undoes (still `exiting`, same exit ticket, same start stamp), so a CLOSED
row is never reopened. After any exit attempt that did not come back
FILLED — a door error resumed on the next tick, an unfilled ticket — the
in-flight ticket is cancelled and the decision is made on the OMS's per-leg
FILLS across every EXIT ticket of the row (`_exit_fills`), never on a
status read before the cancel: all legs closed (a concurrent sweep filled
it) → settle from those fills; nothing filled → reopen; SOME legs closed
(a basket cut between two per-leg commits) → the row stays `exiting`, and
the next chain FETCHED on a tick closes ONLY the legs still open, at their
remaining quantity (`_complete_exit`); settlement then prices every leg at
its own volume-weighted fill across both tickets — no leg is closed twice
and no fill is ignored — and books that legged result UNCLAMPED (the leg
left open really can move past the structure's bounds; such a result is
named `legged_beyond_bounds` on the exit event). A host-wide, non-blocking flock
(`data/.live_pricer_tick.lock`) lets one process tick at a time; a second
scheduler or a hand-run live_bridge skips its tick, named.

EXPIRY BACKSTOP (`eod_sweep`, from plan_tracker.run_tracker): calls the
primary's `_expiry_backstop` itself — intrinsic at the EXPIRY SESSION's own
close (audit F22: an earlier session's close is waited out, never settled
on); after the grace window, the newest earlier close named
`stale_close_after_grace`, or with no bars the defined max loss at zero
frictions. The event records the pricing close's date
(`settlement_close_date`). A row still waiting is named with its reason in
`reasons`, and run_tracker prints it and every per-row error (audit F21).
A row an unfinished exit left `exiting` past expiry is resolved too (audit
F08, `_expire_exiting`): through the completion's exclusive `_claim`, its
working tickets cancelled, then on the fills — all legs closed: settled
from them; none: like an open row; some: the closed legs at their fills
and the open ones at the expiry close's intrinsic, unclamped (no close at
all after the grace window: an open long at 0, an open short at the
width). Every backstop close is a compare-and-set on the state it read.

L2 (2026-10-06, audit F04 / F07 / F15 / F16). A HELD exit is not
re-verified on a new chain inside the quote interval (`_standing_hold`):
the same predicate on the same cached snapshot is the same price refusal,
and an attempt inside the interval is refused at any price — until that
attempt's own interval ends, read off the row (never memoised: L2 review).
The `live_exit` event commits in the SAME transaction as the row close,
the lock release and the P&L — money and audit land together or not at
all; a journal stamp that failed after the commit is re-stamped by the
backstop (`_repair_journal_stamps`). A lock whose entry ticket FILLED is
never released at zero as 'never opened' — not when the primary settles
first, nor when its entry still reads pending at the 15:30 expiry
(`filled_entry_ticket`, the repair's own evidence; `keep_filled_entry_lock`
names it). `ensure_schema` runs no statement once the table exists.

L3 (2026-10-06, audit F11 / F14 + L2 residuals). Debit or credit is the
STRUCTURE's (strategy_router.premium_of / premium_refusal), never the sign
of d: `structure_bounds` takes the formulas from the structure, and prices
that invert it (a debit vertical at a net credit, a condor at a net debit —
an inverted or stale book) are refused by name at the re-quote and at
`open_position`, never booked on bounds read off the sign. `open_position`
never overwrites a row: a (account, journal_ref) is opened ONCE
(`position_state`; LiveEntryRefused). A FILLED entry the row door refuses
is named on one `live_filled_entry_refused` row + one card a day
(`note_refused_fill`), its lock kept. With the arm switched OFF while it
still holds positions (`refs_to_manage`), live_bridge arms `tick` anyway —
manage-only: tick never opens an entry — and `announce_manage_only` says so
on one card a day. Inside a caller's transaction (`commit=False`)
`_log_once_a_day` raises instead of swallowing, so the caller's rollback
runs (expire_pending_lock: nothing expires unless all does). The R:R floor
and risk budget on the re-quote (F12) and the halts on a held lock (F13)
live in options_proposer / portfolio_manager.

L4 (2026-10-06, audit F19 / F20). A chain fetched BEFORE a position
existed never marks it or moves its ratchet (`_predates_entry`): the key is
fetched at once, and until it can be the row abstains, named (L4 review:
a position opened after the tick began abstains on the chain fetched THIS
tick, stamped at the tick's start — named so, no window event — and is
marked on the next tick's fetch). A mark's
time (`last_mark_ts`) is its chain's fetch time, on the tick's clock — a
failing door no longer re-stamps an old chain as fresh. A chain at least a
quote interval old that was not refreshed still marks, but moves no peak,
lock or rung sighting. The ratchet itself is gated by the tracker's own
predicate, `profit_ratchet.applies` — `ratchet_enabled: false` puts this
arm on the static 65% take with everyone else.

Fail-open everywhere: a broken tick prints and returns; the live loop and
the primary account are never touched.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl  # POSIX (Linux VM + macOS) — the single-instance tick lock (audit F06)
except ImportError:  # pragma: no cover — non-POSIX host
    fcntl = None

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
# Audit F02: the two quiet states that cost the pre-expiry / ratchet exit,
# recorded at most once per position per IST day (`_log_once_a_day`).
EVENT_EXIT_HELD, EVENT_MARK_ABSTAINED = "live_exit_held", "live_mark_abstained"
# `_exit` statuses that refuse an exit on its PRICE (the row stays open);
# held_recent_attempt is only the pause after an unfilled attempt, which
# already wrote its own live_exit_unfilled event.
HELD_ON_PRICE = ("held_loss_beyond_max", "held_impossible_mark")
# Audit F05: an exit basket that closed SOME legs only — the row stays
# `exiting` until the rest is closed; one row per position per IST day.
EVENT_EXIT_PARTIAL = "live_exit_partial"
# Architect ruling 2026-10-09 (decision #133): a live exit stuck mid-execution
# needs IMMEDIATE human awareness — a Discord card (sent past the daily
# budget), not only a ledger row. A row left `exiting` this long is stuck:
EVENT_EXIT_STUCK = "live_exit_stuck"
EXIT_STUCK_AFTER_INTERVALS = 2           # x LIVE_QUOTE_INTERVAL_SECONDS
EXIT_REVIEW_CARD = "live_exit_needs_review"
# Audit F06: one live-arm tick at a time on the host (beside the brain map,
# like dhan_client's throttle files). tick reads this constant on every
# call and has no test branch: the suite's conftest points it at a per-test
# tmp file, exactly as it does dhan_client's throttle files (L1 review).
TICK_LOCK_FILE = Path(__file__).resolve().parents[2] / "data" / ".live_pricer_tick.lock"
# Audit F15: the primary's settlement found a FILLED entry ticket for a LIVE
# lock with no position row — the lock is kept for the tick's repair (one
# row per position per IST day: reconcile_orphan_locks retries hourly).
EVENT_LOCK_KEPT_FILLED = "live_lock_kept_filled_entry"
# Audit F11 / L2 residual (b): a FILLED entry ticket open_position refused
# (one row + one card per position per IST day — `note_refused_fill`).
EVENT_FILLED_REFUSED = "live_filled_entry_refused"
# Audit F14: the arm is switched off while it still holds positions (one
# row + one card per IST day — `announce_manage_only`).
EVENT_ARM_OFF = "live_arm_off_managing"
# a FILLED-but-unrecorded entry whose repair raised (lows residual A2, F15)
EVENT_REPAIR_FAILED = "live_unrecorded_repair_failed"

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
_SCHEMA_OBJECTS = ("paper_live_positions",)

# per-process chain cache: (ticker, expiry) -> (fetched_epoch, fetched_iso, chain)
_CHAIN_CACHE: dict = {}
_LAST_CHAIN_CALL = 0.0
# per-process ratchet rungs awaiting confirmation (audit F01):
# (account_id, journal_ref) -> {"src": the _CHAIN_CACHE entry the new rung
# was first read on, "capture": that read's floored capture}. Memory only,
# on purpose: a restart forgets a half-seen rung, so it must be seen twice
# again — the safe direction (no lock = no ratchet exit; #105 has no stop).
_PENDING_RUNG: dict = {}
# per-process exit holds (audit F04): (account_id, journal_ref) -> {"src":
# the _CHAIN_CACHE entry the hold was decided on, "signal", "res": _exit's
# hold result}. While the cache still holds THAT snapshot (younger than the
# quote interval — the fetch loop replaces it once it is due) the same
# predicate on it is the same refusal, so it is not re-verified on a new
# chain every 60-s tick. PRICE holds only (HELD_ON_PRICE — L2 review): a
# recent-attempt hold ends with the attempt's own interval, not the
# snapshot's, and `_standing_hold` answers it exactly from the persisted
# last_exit_attempt_ts. Memory only, beside the cache it is measured
# against: a restart forgets both, and the first tick fetches anyway.
_HELD_EXIT: dict = {}


def reset_cache() -> None:
    """Forget every per-process state — chain cache, pacing clock, the
    unconfirmed ratchet rungs and the standing exit holds — exactly what a
    restart does."""
    global _LAST_CHAIN_CALL
    _CHAIN_CACHE.clear()
    _PENDING_RUNG.clear()
    _HELD_EXIT.clear()
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
    """Create the table once; afterwards touch NOTHING (audit F16, the
    #122/D4 pattern). `executescript` COMMITS whatever transaction the
    caller has open, and `has_open_position` / `closed_pnl` run INSIDE
    portfolio_manager.expire_pending_lock's one commit — its LIVE guard
    used to commit the primary's and every earlier shadow's expiry midway.
    pm._apply_schema: table present → no statement at all; missing inside
    an open transaction → the CREATE joins it."""
    from src import portfolio_manager as pm
    pm._apply_schema(conn, _SCHEMA, _SCHEMA_OBJECTS)
    # M1 (#142): nullable portfolio_id, additive; a PRAGMA read, and ONE ALTER the first time only
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_live_positions)")}
    if "portfolio_id" not in cols:
        conn.execute("ALTER TABLE paper_live_positions ADD COLUMN portfolio_id TEXT")
        if not conn.in_transaction:
            conn.commit()


def _accounts():
    from src import portfolio_manager as pm
    return pm.LIVE_ACCOUNTS


# ------------------------------------------------------------- quotes

def _sign(side: str) -> float:
    return 1.0 if str(side).upper() == "BUY" else -1.0


def _node(chain: dict, strike: float, option_type: str):
    """The leg's quote node, or None when the strike/type is ABSENT from the chain (Chunk 5 Q8)."""
    oc = (chain or {}).get("oc") or {}
    node = oc.get(f"{float(strike):.6f}") or oc.get(str(strike)) or oc.get(str(float(strike)))
    if not node:
        return None
    return node.get(str(option_type).lower()) or None


def _f(v):
    try:
        v = float(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def leg_quote(chain: dict, leg: dict) -> dict:
    """{bid, ask, ltp} for one leg (None where absent/non-positive)."""
    n = _node(chain, leg["strike"], leg["option_type"])
    if n is None:
        return {"bid": None, "ask": None, "ltp": None, "absent": True}
    return {"bid": _f(n.get("top_bid_price")), "ask": _f(n.get("top_ask_price")),
            "ltp": _f(n.get("last_price"))}


def crossed_close_price(leg: dict, q: dict) -> tuple:
    """(price, reason) to CLOSE one open leg now: a long is sold at the bid
    (no bid → 0.0, it cannot be sold — the structure still marks), a short
    is bought back at the ask (no ask → abstain). Abstains on a crossed
    book (bid > ask) or a quote > 50% off the last price."""
    if q.get("absent"):
        return None, "strike absent from the chain"
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
    """{ok, mark_ps, prices: {(strike, type): price}, reason}. An abstaining
    reason names its leg ("SELL 24200CE: no ask ...") — a condor has two
    short legs, and the log line must say which one (audit F02)."""
    prices, mark = {}, 0.0
    for leg in legs:
        price, why = crossed_close_price(leg, leg_quote(chain, leg))
        if price is None:
            label = f"{str(leg['side']).upper()} {float(leg['strike']):g}{str(leg['option_type']).upper()}"
            return {"ok": False, "mark_ps": None, "prices": {}, "reason": f"{label}: {why}"}
        prices[(float(leg["strike"]), str(leg["option_type"]).upper())] = price
        mark += _sign(leg["side"]) * price
    return {"ok": True, "mark_ps": round(mark, 4), "prices": prices, "reason": None}


# ------------------------------------------------------------- structure math

def structure_bounds(spread: dict, d: float) -> dict:
    """{width_ps, max_profit_ps, max_loss_ps} from the structure's width,
    its premium TYPE and the ACTUAL crossed entry d (Σ long − Σ short).

    Audit F11 (2026-10-06): debit or credit is the STRUCTURE's
    (strategy_router.premium_of), never d's sign. The sign rule took a bear
    put quoted at a net credit for a credit structure and a condor quoted at
    a net debit for a debit one, and every clamp then enforced those wrong
    bounds (the condor's loss capped at d instead of the width + d). From
    the structure, a d that contradicts it gives a NON-POSITIVE bound — the
    true bounds of such a fill (a condor bought for a debit can only lose)
    — which requote_entry and open_position refuse by name
    (`premium_refusal`) before anything is booked. A structure the routing
    table does not know raises: its bounds cannot be derived."""
    from src.strategy_router import premium_of
    kind = premium_of(spread.get("strategy"))
    if kind is None:
        raise ValueError(f"unknown structure {spread.get('strategy')!r}: no debit/credit type to bound it by")
    lot = int(spread.get("lot_size") or 0)
    width = spread.get("spread_width")
    if width is None and lot:
        width = (float(spread.get("max_loss") or 0) + float(spread.get("max_profit") or 0)) / lot
    width = float(width or 0.0)
    if kind == "debit":
        return {"width_ps": width, "max_profit_ps": round(width - d, 4), "max_loss_ps": round(d, 4)}
    return {"width_ps": width, "max_profit_ps": round(-d, 4), "max_loss_ps": round(width + d, 4)}


def _premium_refusal(strategy: str, d: float):
    """strategy_router.premium_refusal — the ONE debit/credit rule (F11)."""
    from src.strategy_router import premium_refusal
    return premium_refusal(strategy, d)


class LiveEntryRefused(ValueError):
    """open_position will not record this entry; the message names why
    (audit F11: fills that invert the structure; L2 residual (b): a row
    for this (account, journal_ref) already exists)."""


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
    # Audit F11: prices that invert the structure (a debit vertical at a net
    # credit, a condor at a net debit) are an inverted or stale book — the
    # arm abstains, named, rather than open on bounds taken from the sign.
    inverted = _premium_refusal(spread.get("strategy"), d)
    if inverted:
        return {"ok": False, "legs": None, "quote_ts": None,
                "reason": f"crossed prices invert the structure: {inverted}"}
    b = structure_bounds(spread, d)
    if b["max_profit_ps"] <= 0 or b["max_loss_ps"] <= 0 or b["max_loss_ps"] >= b["width_ps"] > 0:
        return {"ok": False, "legs": None, "quote_ts": None,
                "reason": (f"crossed prices leave no trade: net {d:+.2f}/share on a {b['width_ps']:g}-wide "
                           f"structure (max profit {b['max_profit_ps']:.2f}, max loss {b['max_loss_ps']:.2f})")}
    return {"ok": True, "legs": legs, "quote_ts": _iso(now), "reason": None, "entry_mark_ps": d, **b}


def open_position(conn, account: str, entry: dict, ticket_view: dict, quote_ts: str = None,
                  now: datetime = None) -> dict:
    """Record the account's FILLED entry ticket as an open live position.

    Refuses — raises LiveEntryRefused, nothing written — when (L2 residual
    (b)) a row for this (account, journal_ref) already exists: the table's
    key is that pair, and the old INSERT OR REPLACE let a re-approval of a
    still-pending entry whose LIVE position had been kept and repaired
    overwrite it (an `exiting` row's earlier EXIT tickets would then be
    counted against the new row) — or erase a settled row; or (audit F11)
    when the fills invert the structure. _execute_paper_entry checks the
    first BEFORE any ticket is issued and requote_entry the second; this is
    the backstop at the door that writes."""
    ensure_schema(conn)
    now = now or _now()
    spread = entry["spread"]
    held = position_state(conn, account, entry.get("short_id"))
    if held is not None:
        raise LiveEntryRefused(f"a live position for {entry.get('short_id')} is already recorded "
                               f"({held}) — a ref is opened once; nothing written")
    legs, d = [], 0.0
    for l in ticket_view.get("legs") or []:
        fill = float(l["avg_fill_price"])
        legs.append({"side": str(l["side"]).upper(), "option_type": str(l.get("option_type") or "").upper(),
                     "strike": float(l["strike"]), "entry_fill": fill})
        d += _sign(l["side"]) * fill
    if not legs:
        raise ValueError("ticket has no filled legs")
    d = round(d, 4)
    inverted = _premium_refusal(spread.get("strategy"), d)
    if inverted:
        raise LiveEntryRefused(f"the entry fills invert the structure: {inverted}")
    b = structure_bounds(spread, d)
    portfolio_id = f"{account}/{entry.get('portfolio_family') or 'IDX_SPREADS'}"   # M1 (#142)
    conn.execute("INSERT INTO paper_live_positions (account_id, journal_ref, ticker, strategy, "
                 "direction, expiry, lots, lot_size, legs_json, entry_mark_ps, width_ps, max_profit_ps, "
                 "max_loss_ps, opened_at, entry_quote_ts, state, portfolio_id) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (account, entry.get("short_id"), entry["ticker"], spread.get("strategy"),
                  spread.get("direction"), spread["expiry"], int(ticket_view.get("lots") or spread.get("lots") or 1),
                  int(spread["lot_size"]), json.dumps(legs), d, b["width_ps"], b["max_profit_ps"],
                  b["max_loss_ps"], _iso(now), quote_ts, STATE_OPEN, portfolio_id))
    conn.commit()
    return {"journal_ref": entry.get("short_id"), "entry_mark_ps": d, **b, "legs": legs}


def has_open_position(conn, account: str, journal_ref: str) -> bool:
    """True while an OPEN/EXITING row exists. RAISES on a database error —
    the caller must treat 'unknown' as held, never as 'no position'."""
    ensure_schema(conn)
    row = conn.execute("SELECT 1 FROM paper_live_positions WHERE account_id = ? AND journal_ref = ? "
                       "AND state != ?", (account, journal_ref, STATE_CLOSED)).fetchone()
    return row is not None


def position_state(conn, account: str, journal_ref: str):
    """The row's state ('open' | 'exiting' | 'closed') for this (account,
    journal_ref), or None when no row exists — a ref is opened ONCE (L2
    residual (b)). RAISES on a database error, like has_open_position:
    'unknown' must never be read as 'no position'."""
    ensure_schema(conn)
    row = conn.execute("SELECT state FROM paper_live_positions WHERE account_id = ? AND journal_ref = ?",
                       (account, journal_ref)).fetchone()
    return None if row is None else str(row[0])


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

def _entry_like(row: dict, legs: list = None, lots: int = None) -> dict:
    """The tracker-shaped entry the exit door needs, built from the row so
    it works whatever the journal row says now. `legs` / `lots` narrow it
    to what a partly filled exit left open (audit F05: the completion
    ticket carries ONLY those legs, at the remaining quantity)."""
    lot = int(row["lot_size"])
    return {"short_id": row["journal_ref"], "ticker": row["ticker"], "date": row["opened_at"][:10],
            "spread": {"strategy": row["strategy"], "direction": row["direction"], "expiry": row["expiry"],
                       "lot_size": lot, "lots": int(row["lots"] if lots is None else lots),
                       "legs": [{"side": l["side"], "option_type": l["option_type"], "strike": l["strike"],
                                 "premium": l["entry_fill"], "fill_basis": FILL_BASIS}
                                for l in (row["legs"] if legs is None else legs)],
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
            now: datetime, ticket_id: str = None, detail: dict = None, clamp: bool = True,
            expect_state: str = None, expect_attempt: str = None) -> dict:
    """Close the row and settle ONLY this account's lock. Idempotent: an
    already-closed row settles nothing. `clamp=False` books the mark as it
    is, beyond the structure's bounds — only a legged exit's actual fills
    (`_settle_from_fills`, and the expiry backstop of a partly filled
    exit); everything else is clamped to the bounds.

    `expect_state` makes the close a compare-and-set on the state the
    caller READ (L2: eod_sweep's backstop, which runs in the API process
    without the tick lock) — a row another actor moved since (reopened,
    stamped `exiting`) settles nothing and answers `state_moved`, named.
    `expect_attempt` (with `expect_state='exiting'`) narrows it to ONE exit
    attempt — the row's `exit_started_at` (F06 residual: `_exit`'s FILLED
    path settles only the attempt it stamped, never one another actor
    stamped since)."""
    from src import portfolio_manager as pm
    qty = int(row["lots"]) * int(row["lot_size"])
    d = float(row["entry_mark_ps"])
    profit_ps = float(exit_mark_ps) - d
    if clamp:
        profit_ps = _clamp(profit_ps, float(row["max_loss_ps"]), float(row["max_profit_ps"]))
    pnl = round(profit_ps * qty - float(frictions), 2)
    # Everything that may commit or write runs BEFORE the transaction opens
    # (audit Chunk 1 D4, decision #122): the schema check, and the halt
    # read that can latch a halt row. Inside it: only plain statements.
    pm.ensure_accounts_schema(conn)
    if conn.in_transaction:
        conn.commit()
    was_halted = pm.paper_trading_halted(conn, row["account_id"])
    guard, guard_args = ("state = ?", (expect_state,)) if expect_state else ("state != ?", (STATE_CLOSED,))
    if expect_attempt is not None:
        guard, guard_args = guard + " AND exit_started_at IS ?", guard_args + (expect_attempt,)
    cur = conn.execute("UPDATE paper_live_positions SET state = ?, closed_at = ?, resolution = ?, "
                       "exit_mark_ps = ?, settlement_basis = ?, frictions_rs = ?, pnl_net = ?, "
                       "exit_ticket_id = COALESCE(?, exit_ticket_id), last_profit_ps = ?, "
                       "last_capture_pct = ?, last_mark_ps = ?, last_mark_ts = ? "
                       f"WHERE account_id = ? AND journal_ref = ? AND {guard}",
                       (STATE_CLOSED, _iso(now), resolution, round(float(exit_mark_ps), 4), basis,
                        float(frictions), pnl, ticket_id, round(profit_ps, 4),
                        round(_capture(profit_ps, float(row["max_profit_ps"])), 2),
                        round(float(exit_mark_ps), 4), _iso(now),
                        row["account_id"], row["journal_ref"], *guard_args))
    if cur.rowcount == 0:
        conn.rollback()
        state = _row_state(conn, row)
        if expect_state and state != STATE_CLOSED:
            as_read = f"'{expect_state}'" + (f" on the attempt stamped {expect_attempt}"
                                            if expect_attempt is not None else "")
            return {"status": "state_moved", "journal_ref": row["journal_ref"],
                    "reason": f"row is '{state}' now, not {as_read} as read — another actor moved it; "
                              "nothing settled"}
        return {"status": "already_closed", "journal_ref": row["journal_ref"]}
    payload = {"resolution": resolution, "basis": basis, "closed_at": _iso(now), "ticker": row["ticker"],
               "strategy": row["strategy"], "lots": row["lots"], "entry_mark_ps": d,
               "exit_mark_ps": round(float(exit_mark_ps), 4), "profit_ps": round(profit_ps, 4),
               "capture_pct": round(_capture(profit_ps, float(row["max_profit_ps"])), 2),
               "frictions_rs": float(frictions), "pnl_net": pnl, "ticket_id": ticket_id, **(detail or {})}
    # The row close above is NOT committed yet. The lock release, the
    # account's P&L AND the live_exit audit event join it as plain
    # statements (no DDL, no commit inside), and ONE commit lands all four;
    # a failure anywhere rolls the row back to where it was, so the next
    # tick retries — never a closed row with a live lock. (Before #122 a
    # schema executescript inside the release committed the row close on
    # its own. Audit F07: the event used to be written AFTER the money
    # commit, so a failed INSERT lost the exit's only audit row for good —
    # nothing revisits a closed row — and the settled exit was logged as
    # 'tick failed'. Money and audit now commit together or not at all.)
    try:
        lock_released = pm.paper_release_rows(conn, row["account_id"], row["journal_ref"], pnl)
        payload["lock_released"] = bool(lock_released)
        pm.paper_log_event(conn, row["account_id"], EVENT_EXIT, row["journal_ref"],
                           json.dumps(payload, sort_keys=True), commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # Past the commit nothing may raise: a settled exit is reported settled.
    if lock_released:
        try:
            pm.paper_after_release(conn, row["account_id"], row["journal_ref"], pnl, was_halted)
        except Exception as exc:    # the money is settled; a curve point is not
            print(f"  (live account: post-settle record skipped for {row['journal_ref']}: {exc})")
    _stamp_journal(row, payload)     # best-effort; eod_sweep repairs a missed stamp (F07)
    if row["account_id"] == pm.ACCOUNT_PAPER_SHADOW_LEARNER:
        from src import shadow_learner
        shadow_learner.resolve_from_settle(conn, row, pnl, payload["closed_at"])   # the Court's real r
    return {"status": "settled", **payload}


def _stamp_journal(row: dict, payload: dict) -> bool:
    """Best-effort: the journal row's account verdict shows the close.
    Muzzled under pytest (the real journal is never touched by a test).
    True when the journal row was stamped; a failure is printed, and
    eod_sweep's `_repair_journal_stamps` re-stamps it from the closed row
    (audit F07)."""
    if _is_test_env():
        return False
    try:
        from src import journal

        def _mutate(e):
            acc = e.get("accounts")
            if not isinstance(acc, dict) or not isinstance(acc.get(row["account_id"]), dict):
                return False
            acc[row["account_id"]].update({"status": "closed", "closed_at": payload["closed_at"],
                                           "resolution": payload["resolution"], "pnl_rs": payload["pnl_net"]})
            return True
        return journal.update_entry(row["journal_ref"], _mutate) is not None
    except Exception as exc:
        print(f"  (live account: journal stamp skipped for {row['journal_ref']}: {exc})")
        return False


def _journal_stamped(block, pnl) -> bool:
    """The journal's verdict block for this account already shows the
    close at this P&L."""
    try:
        return block.get("status") == "closed" and abs(float(block.get("pnl_rs")) - float(pnl)) < 0.005
    except (TypeError, ValueError):
        return False


def _repair_journal_stamps(conn) -> list:
    """Audit F07: a CLOSED row whose journal verdict block does not show
    the close (the best-effort stamp after the commit failed — a journal
    lock timeout, a disk error) is stamped from the row itself. Read
    against the journal's own content, so a stamp is never written twice
    and a row the journal does not carry an account block for is left
    alone (`_stamp_journal` would not write it either). Run by eod_sweep
    (hourly), before run_tracker reads the journal. Returns the refs it
    stamped."""
    rows = conn.execute("SELECT account_id, journal_ref, closed_at, resolution, pnl_net FROM paper_live_positions "
                        "WHERE state = ? AND pnl_net IS NOT NULL", (STATE_CLOSED,)).fetchall()
    if not rows:
        return []
    from src import journal
    by_ref = {journal.row_key(e): e for e in journal.read_all()}
    fixed = []
    for acct, ref, closed_at, resolution, pnl in (tuple(r) for r in rows):
        block = ((by_ref.get(ref) or {}).get("accounts") or {}).get(acct)
        if not isinstance(block, dict) or _journal_stamped(block, pnl):
            continue
        if _stamp_journal({"account_id": acct, "journal_ref": ref},
                          {"closed_at": closed_at, "resolution": resolution, "pnl_net": float(pnl)}):
            fixed.append(ref)
    return fixed


def _exit_fills(conn, row: dict) -> dict:
    """What this row's EXIT tickets have ACTUALLY closed, read from the OMS
    (audit F05). Every EXIT ticket of (account, journal_ref) counts — the
    position row is one per pair, so they are all this row's: the first
    attempt and the completion of a basket that only partly filled. Per
    position leg: `filled` (qty_filled summed over every ticket, whatever
    state the leg ended in — a cancelled remainder keeps its fills),
    `avg_fill` (volume-weighted), `remaining` (position qty − filled, never
    below 0), `by_ticket` ({ticket: (qty, avg, limit)}, the fills each
    ticket contributed and that leg's limit on it — L2: the limit is the
    ticket's own record of a zero-bid leg floored to TICK_FLOOR).
    `in_flight` = tickets with a leg still PENDING/PARTIAL (a
    venue may yet fill it); `filled_tickets` = tickets with any fill,
    oldest first; `complete` = every leg closed; `filled_any` = some fill.
    Commits (oms.ensure_schema): call it outside a transaction."""
    from src import oms
    oms.ensure_schema(conn)
    qty = int(row["lots"]) * int(row["lot_size"])
    tickets = [tuple(r) for r in conn.execute(
        "SELECT ticket_id, note FROM trade_tickets WHERE account_id = ? AND journal_ref = ? "
        "AND note LIKE 'EXIT %' ORDER BY rowid", (row["account_id"], row["journal_ref"])).fetchall()]
    agg, per, in_flight, filled_tickets = {}, {}, [], []
    for tid, _note in tickets:
        got = False
        for strike, otype, state, q, avg, limit in (tuple(r) for r in conn.execute(
                "SELECT strike, option_type, state, qty_filled, avg_fill_price, limit_price FROM trade_legs "
                "WHERE ticket_id = ?", (tid,)).fetchall()):
            if state in (oms.PENDING, oms.PARTIAL) and tid not in in_flight:
                in_flight.append(tid)
            q = int(q or 0)
            if q > 0 and avg is not None:
                key = (float(strike), str(otype or "").upper())
                n, notional = agg.get(key, (0, 0.0))
                agg[key] = (n + q, notional + q * float(avg))
                per.setdefault(key, {})[tid] = (q, float(avg), None if limit is None else float(limit))
                got = True
        if got:
            filled_tickets.append(tid)
    legs = []
    for l in row["legs"]:
        key = (float(l["strike"]), str(l["option_type"]).upper())
        n, notional = agg.get(key, (0, 0.0))
        legs.append({"leg": l, "key": key, "filled": n, "remaining": max(0, qty - n),
                     "avg_fill": round(notional / n, 4) if n else None, "by_ticket": per.get(key, {})})
    note = str(tickets[0][1] or "") if tickets else ""
    return {"tickets": [t[0] for t in tickets], "in_flight": in_flight, "filled_tickets": filled_tickets,
            "legs": legs, "filled_any": any(x["filled"] for x in legs),
            "complete": all(x["remaining"] == 0 for x in legs),
            "note_resolution": note[5:].strip() if note.startswith("EXIT ") else ""}


def _leg_label(leg: dict) -> str:
    return f"{str(leg['side']).upper()} {float(leg['strike']):g}{str(leg['option_type']).upper()}"


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


def _booked_fills(ex: dict, attempt: dict = None) -> tuple:
    """({leg key: booked price}, {leg key: the venue's VWAP}, {floored keys})
    over each leg's FILLED quantity, across every EXIT ticket of the row.

    A long leg with no bid is worth 0, but the venue rejects a 0 limit, so
    its limit is floored up to TICK_FLOOR and it fills there. That fill is
    booked at the quote, as `_exit` books it (#120: the floor is the
    venue's constraint, not a price); the VWAP keeps what the venue
    recorded. Which fills were floored:
      * on THIS attempt's ticket (`attempt` = {"ticket", "floored": {key:
        quote}}), exactly the legs it floored — it saw the quotes;
      * on any OTHER ticket (the first ticket of a basket cut between its
        legs, a crash-resumed exit, the expiry backstop) the ticket's own
        record decides (L2, residual (b) of the L1 review): a LONG leg
        whose limit and fill are both TICK_FLOOR is booked at 0. A ticket
        cannot tell a floored zero bid from a real 0.05 bid; 0 is the
        conservative reading (at most 0.05/share, never a mark-up).
    Only a fill AT the floor is ever re-priced: a floored leg the venue
    filled anywhere else is booked at that fill."""
    a_tid = (attempt or {}).get("ticket")
    a_floored = (attempt or {}).get("floored") or {}
    booked, fills, floored = {}, {}, set()
    for x in ex["legs"]:
        if not x["filled"]:
            continue
        fills[x["key"]] = x["avg_fill"]
        notional = 0.0
        for t, (q, avg, limit) in x["by_ticket"].items():
            px = avg
            if abs(avg - TICK_FLOOR) < 1e-9:
                if a_tid is not None and t == a_tid:
                    px = float(a_floored.get(x["key"], avg))
                elif _sign(x["leg"]["side"]) > 0 and limit is not None and abs(limit - TICK_FLOOR) < 1e-9:
                    px = 0.0
            if px != avg:
                floored.add(x["key"])
            notional += q * px
        booked[x["key"]] = round(notional / x["filled"], 4)
    return booked, fills, floored


def _legged_detail(row: dict, ex: dict, mark: float, d: dict) -> None:
    """Name a LEGGED settlement (fills on more than one EXIT ticket) on the
    event, and `legged_beyond_bounds` when its result is outside
    [−max loss, +max profit] — the bounds themselves are inside."""
    d["exit_tickets"] = list(ex["filled_tickets"])
    profit_ps = mark - float(row["entry_mark_ps"])
    if not -float(row["max_loss_ps"]) - 1e-9 <= profit_ps <= float(row["max_profit_ps"]) + 1e-9:
        d["legged_beyond_bounds"] = {"profit_ps": round(profit_ps, 4), "max_loss_ps": row["max_loss_ps"],
                                     "max_profit_ps": row["max_profit_ps"]}


def _settle_from_fills(conn, row: dict, ex: dict, now: datetime, detail: dict = None,
                       attempt: dict = None, expect_state: str = None) -> dict:
    """Settle on what the OMS says filled (audit F05): exit mark = Σ sign ×
    each leg's volume-weighted fill across EVERY exit ticket of the row,
    frictions on those prices — a basket closed over two tickets is booked
    once, on both tickets' real prices. The row's own predicate names it;
    a row stamped before `exit_resolution` existed falls back to the
    ticket's note. Fills at the venue's floor are booked per
    `_booked_fills` (`attempt`: this attempt's ticket and the legs it
    floored); `fills` keeps what the venue recorded.

    A LEGGED exit (fills on more than one ticket) is booked UNCLAMPED (L1
    review, spec 3(b): P&L comes from the actual fills). Legging risk is
    real: the leg left open moves while the other is already closed, so
    the result can fall outside [−max loss, +max profit], and a clamp would
    misstate it. Such a result is named `legged_beyond_bounds` on the
    exit event. A single-ticket exit is clamped, as every other settlement
    — 'legged' counts only tickets that FILLED something (a cancelled
    attempt with no fill is not a leg). `expect_state` → `_settle`."""
    prices, fills, floored = _booked_fills(ex, attempt)
    mark = round(sum(_sign(x["leg"]["side"]) * prices[x["key"]] for x in ex["legs"]), 4)
    resolution = row.get("exit_resolution") or ex.get("note_resolution") or "resumed_exit"
    d = dict(detail or {}, fills={f"{k[0]:g}{k[1]}": v for k, v in fills.items()})
    if floored:
        d["limit_floored"] = {**(d.get("limit_floored") or {}),
                              **{f"{k[0]:g}{k[1]}": TICK_FLOOR for k in floored}}
    legged = len(ex["filled_tickets"]) > 1
    if legged:
        _legged_detail(row, ex, mark, d)
    tid = ex["filled_tickets"][-1] if ex["filled_tickets"] else None
    return _settle(conn, row, mark, resolution, "live_bid_ask", _frictions(row, prices), now,
                   ticket_id=tid, detail=d, clamp=not legged, expect_state=expect_state)


def _reopen(conn, row: dict, exit_ticket_id, exit_started_at) -> bool:
    """Put an `exiting` row back to `open` after an exit attempt that
    filled NOTHING. Audit F06: a compare-and-set on the attempt being
    undone — the row must still be `exiting`, with that attempt's exit
    ticket and start stamp — never an unconditional write (a stale actor
    used to reopen a row another one had CLOSED: a ghost whose lock was
    already released). False = the row moved on; nothing is written."""
    cur = conn.execute("UPDATE paper_live_positions SET state = ?, exit_started_at = NULL, exit_ticket_id = NULL "
                       "WHERE account_id = ? AND journal_ref = ? AND state = ? AND exit_ticket_id IS ? "
                       "AND exit_started_at IS ?",
                       (STATE_OPEN, row["account_id"], row["journal_ref"], STATE_EXITING,
                        exit_ticket_id, exit_started_at))
    conn.commit()
    return cur.rowcount == 1


def _row_state(conn, row: dict):
    r = conn.execute("SELECT state FROM paper_live_positions WHERE account_id = ? AND journal_ref = ?",
                     (row["account_id"], row["journal_ref"])).fetchone()
    return r[0] if r else None


def _resolve_attempt(conn, row: dict, ex: dict, now: datetime, exit_ticket_id, exit_started_at,
                     detail: dict = None, attempt: dict = None) -> dict:
    """THE decision after an exit attempt that did not come back FILLED,
    made on the OMS's per-leg fills across every EXIT ticket of the row
    (audit F05) — never on a status read before the cancel. `row` is the
    row AS THIS ATTEMPT LEFT IT (its own predicate and ticket — L1 review:
    a caller's pre-stamp copy can carry an earlier attempt's predicate);
    `attempt` ({"ticket", "floored"}) goes to `_settle_from_fills`:
      a leg still in flight → `exit_in_flight` (row stays `exiting`; the
                              next tick's resume decides);
      every leg closed      → settle from the fills (a concurrent sweep
                              filled it: never reopen a closed exit);
      some legs closed      → `partial` (row stays `exiting`; only the
                              rest is ever exited again — `_complete_exit`);
      nothing filled        → `reopened` (compare-and-set on this attempt,
                              F06) or `not_reopened` when the row moved on."""
    ref = row["journal_ref"]
    if ex["in_flight"]:
        return {"status": "exit_in_flight", "journal_ref": ref,
                "reason": f"exit ticket(s) {', '.join(ex['in_flight'])} still have a leg working — "
                          "row left 'exiting' for the next tick"}
    if ex["complete"]:
        return _settle_from_fills(conn, row, ex, now, detail, attempt)
    if ex["filled_any"]:
        closed = ", ".join(f"{_leg_label(x['leg'])} {x['filled']}" for x in ex["legs"] if x["filled"])
        left = ", ".join(f"{_leg_label(x['leg'])} {x['remaining']}" for x in ex["legs"] if x["remaining"])
        reason = (f"{row.get('exit_resolution') or 'exit'} basket partly filled: closed {closed}; still open "
                  f"{left} — the row stays 'exiting' and only the open legs are exited, on the next fetched chain")
        _alert_exit(conn, row, EVENT_EXIT_PARTIAL, reason)
        return {"status": "partial", "journal_ref": ref, "reason": reason,
                "remaining": {_leg_label(x["leg"]): x["remaining"] for x in ex["legs"] if x["remaining"]}}
    if _reopen(conn, row, exit_ticket_id, exit_started_at):
        return {"status": "reopened", "journal_ref": ref}
    return {"status": "not_reopened", "journal_ref": ref,
            "reason": f"row is '{_row_state(conn, row)}' now — another actor moved it; nothing reopened"}


def _attempt_age_s(row: dict, now: datetime):
    """Seconds since the row's exit attempt last moved (its claim /
    ticket stamp `last_exit_attempt_ts`, else its `exit_started_at`);
    None when neither parses (a legacy row: treated as old)."""
    for key in ("last_exit_attempt_ts", "exit_started_at"):
        ts = _ts(row.get(key))
        if ts is not None:
            return (now.replace(tzinfo=None) - ts.replace(tzinfo=None)).total_seconds()
    return None


def _resume_exiting(conn, row: dict, now: datetime, interval_s: int = None, guarded: bool = True) -> dict:
    """A row left in `exiting` — a crash or a door error mid-exit, or a
    basket that only partly filled: cancel whatever is still in flight
    (keeping what each cancel returns), then resolve on what the OMS says
    ACTUALLY filled across all of the row's EXIT tickets (audit F05: it
    used to reopen the FULL position on any non-FILLED read, so a
    half-filled basket — or a ticket filled under the cancel — was closed
    a second time later). Never issues a ticket: closing the legs a
    partial basket left open is tick's job, on a freshly fetched chain.

    F06 residual — only when the tick runs UNGUARDED (`guarded=False`: the
    single-instance lock could not be taken and the tick failed open). With
    the lock held no other tick can be in its door, so a crash is resumed at
    once, as before. Unguarded, an attempt that moved inside the last quote
    interval may be ANOTHER actor's, in its door right now. Of what a
    resume does, two things would break that actor: it
    cancels a ticket that still has a leg working (its exit is lost), and
    it reopens an attempt that has no ticket yet (its ticket would then be
    fenced off and cancelled). Such a young attempt is left untouched —
    `exit_in_flight` with `young=True` — and resolved once it is older,
    the column's own rule (an exit attempt is not retried, nor undone,
    inside one quote interval). Settling an exit whose legs all filled, or
    naming a partly filled one, is safe at any age and runs as before."""
    from src import oms
    from src.config import LIVE_QUOTE_INTERVAL_SECONDS
    interval_s = LIVE_QUOTE_INTERVAL_SECONDS if interval_s is None else int(interval_s)
    ex = _exit_fills(conn, row)
    age = None if guarded else _attempt_age_s(row, now)
    if age is not None and age < interval_s and (
            ex["in_flight"] or (not ex["filled_any"] and not row.get("exit_ticket_id"))):
        what = (f"ticket(s) {', '.join(ex['in_flight'])} still working" if ex["in_flight"]
                else "no exit ticket yet")
        return {"status": "exit_in_flight", "journal_ref": row["journal_ref"], "young": True,
                "reason": f"exit attempt last moved {int(age)}s ago ({what}), inside the {interval_s}-s quote "
                          "interval — in flight (possibly another actor's), left untouched"}
    cancelled = {}
    for tid in ex["in_flight"]:
        cancelled[tid] = oms.cancel_ticket(conn, tid, "live account: exit resumed unfilled").get("status")
    if cancelled:
        ex = _exit_fills(conn, row)     # what the cancels left — a fill that beat them counts
    res = _resolve_attempt(conn, row, ex, now, row.get("exit_ticket_id"), row.get("exit_started_at"),
                           detail={"resumed": True})
    if cancelled:
        res["cancelled"] = cancelled
    return res


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
    started = _iso(now)
    cur = conn.execute("UPDATE paper_live_positions SET state = ?, exit_started_at = ?, exit_resolution = ?, "
                       "last_exit_attempt_ts = ? WHERE account_id = ? AND journal_ref = ? AND state = ?",
                       (STATE_EXITING, started, resolution, started, row["account_id"], row["journal_ref"],
                        STATE_OPEN))
    owned = cur.rowcount == 1
    conn.commit()
    if not owned:
        # Audit F06: this stamp IS the exit's serialisation. Matching no row
        # means the caller's copy is stale — another actor already stamped,
        # resumed or settled this position — so this one issues NOTHING (a
        # stale copy used to issue and fill a second EXIT ticket, refused
        # only afterwards by _settle's 'already_closed').
        return {"status": "exit_not_owned", "journal_ref": row["journal_ref"],
                "reason": f"row is '{_row_state(conn, row)}', not 'open' — another actor owns this exit; "
                          "no ticket issued"}
    def _fence(tids: dict) -> bool:
        # F06 residual: the ticket is attached to THIS attempt by a
        # compare-and-set BEFORE the venue may fill it. An actor frozen
        # between its stamp and its ticket (a stall past one quote interval
        # lets another actor's resume treat the attempt as a crash, reopen
        # it and exit) finds the row no longer its own and its ticket is
        # cancelled unswept — it can never fill a second basket. The same
        # write refreshes last_exit_attempt_ts, so a resume sees a ticket
        # just attached as IN FLIGHT for one more interval.
        cur = conn.execute("UPDATE paper_live_positions SET exit_ticket_id = ?, last_exit_attempt_ts = ? "
                           "WHERE account_id = ? AND journal_ref = ? AND state = ? AND exit_started_at IS ? "
                           "AND exit_ticket_id IS NULL",
                           (tids.get(row["account_id"]), _iso(now), row["account_id"], row["journal_ref"],
                            STATE_EXITING, started))
        conn.commit()
        return cur.rowcount == 1

    rec = pt._execute_paper_exit(_entry_like(row), limits, resolution, conn=conn, venue_mod=venue_mod,
                                 today=now.date(), accounts=[(row["account_id"], int(row["lots"]))],
                                 fence=_fence)
    tid = rec.get("ticket_id")
    if rec.get("fenced"):
        return {"status": "exit_not_owned", "journal_ref": row["journal_ref"], "ticket_id": tid,
                "reason": f"another actor took this exit over while this attempt (stamped {started}) was in "
                          f"its door — row is '{_row_state(conn, row)}'; ticket {tid} cancelled unfilled"}
    if rec.get("mode") == "paper_venue" and rec.get("status") == oms.FILLED:
        view = oms.ticket_view(conn, tid) or {}
        _, fills = _mark_from_ticket(view)
        detail["fills"] = {f"{k[0]:g}{k[1]}": v for k, v in fills.items()}
        # settle on the crossed quotes (the venue filled at exactly them; a
        # floored zero-bid leg is a venue constraint, not a price)
        try:
            # only the attempt THIS call stamped (F06 residual): a row another
            # actor has since reopened or re-stamped answers state_moved, and
            # its own resume books these fills with the rest of its tickets
            return _settle(conn, row, would, resolution, "live_bid_ask", frictions, now, ticket_id=tid,
                           detail=detail, expect_state=STATE_EXITING, expect_attempt=started)
        except Exception as exc:
            # Audit F07: the settlement is ONE transaction (row, lock, P&L,
            # live_exit event), so a failure here booked nothing — say that,
            # not 'tick failed'. The row stays `exiting` on its FILLED
            # ticket; the next tick's resume settles it from the fills.
            return {"status": "exit_error", "journal_ref": row["journal_ref"], "ticket_id": tid,
                    "reason": f"exit FILLED but its settlement failed and was rolled back ({exc}) — nothing "
                              "booked; the next tick settles it from the filled ticket"}
    if rec.get("error") and not tid:
        # the door raised after it may have issued: leave `exiting`; the
        # next tick resumes from whatever ticket exists (or reopens if none)
        pm.paper_log_event(conn, row["account_id"], EVENT_UNFILLED, row["journal_ref"],
                           f"{resolution}: exit door error ({rec.get('error')}) — row left for resume")
        return {"status": "exit_error", "journal_ref": row["journal_ref"], "reason": rec.get("error")}
    if tid:
        try:
            oms.cancel_ticket(conn, tid, "live account: exit not filled")
        except Exception as exc:
            # what is still working is unknown: never reopen on a guess (F05)
            pm.paper_log_event(conn, row["account_id"], EVENT_UNFILLED, row["journal_ref"],
                               f"{resolution}: exit ticket {tid} not filled and its cancel failed ({exc}) "
                               "— row left for resume")
            return {"status": "exit_error", "journal_ref": row["journal_ref"], "ticket_id": tid,
                    "reason": f"cancel failed: {exc}"}
    # Audit F05: decide on what the venue ACTUALLY filled across this row's
    # exit tickets — a ticket filled between the sweep and the cancel is
    # settled, a basket that closed some legs is never restored whole. On
    # the row as THIS attempt stamped it (L1 review): the caller's copy is
    # pre-stamp, and a reopened earlier attempt leaves its predicate in
    # exit_resolution — a fill landing now settles under THIS predicate.
    stamped = dict(row, state=STATE_EXITING, exit_resolution=resolution, exit_started_at=started,
                   last_exit_attempt_ts=started, exit_ticket_id=tid)
    res = _resolve_attempt(conn, stamped, _exit_fills(conn, stamped), now, tid, started, detail=detail,
                           attempt={"ticket": tid, "floored": floored})
    if res["status"] != "reopened":
        return dict(res, ticket_id=res.get("ticket_id") or tid)
    pm.paper_log_event(conn, row["account_id"], EVENT_UNFILLED, row["journal_ref"],
                       f"{resolution}: exit ticket {tid or '-'} not filled ({rec.get('status')} "
                       f"{rec.get('error') or ''}) — position kept, next attempt after {interval_s}s")
    return {"status": "unfilled", "journal_ref": row["journal_ref"], "ticket_id": tid,
            "reason": rec.get("error") or rec.get("status")}


def _claim(conn, row: dict, now: datetime, interval_s: int = None, actor: str = "this tick") -> dict:
    """THE exclusive claim on the next step of an `exiting` row's exit —
    `_complete_exit` (tick) and the expiry backstop (`eod_sweep`, the API
    process, which runs WITHOUT the tick lock) both take it. A
    compare-and-set that stamps last_exit_attempt_ts = now, and matches
    only when
      * the row is still as the caller READ it (state `exiting`, the same
        exit ticket, start stamp and last claim — F06 / L1 review: a
        claimant holding a stale snapshot is refused), AND
      * no claim is IN FLIGHT: the last attempt is at least one quote
        interval old (L2, residual (a) of the L1 review). Matching the
        read alone was not exclusive: a reader that read the row AFTER
        the first claim committed but BEFORE that claimant's ticket
        existed saw the new stamp, matched it, and sold the open leg a
        second time. The in-flight test is INSIDE the compare-and-set,
        so it holds whatever the caller read. It is the column's own rule
        ("an unfilled exit is not retried inside one quote interval"),
        and it covers the same-second claim the L1 review guarded.
    Returns {"owned": True, "claim": stamp} or {"owned": False, "status",
    "reason"}: `partial_held` (in flight — the row is as read, retried
    after the interval) or `exit_not_owned` (the row moved on). `actor`
    names the claimant in that reason ("this tick" for the completion,
    "the expiry backstop" for eod_sweep — L2 review)."""
    from src.config import LIVE_QUOTE_INTERVAL_SECONDS
    interval_s = LIVE_QUOTE_INTERVAL_SECONDS if interval_s is None else int(interval_s)
    claim, read = _iso(now), row.get("last_exit_attempt_ts")
    settled_before = _iso(now - timedelta(seconds=max(1, interval_s)))
    ident = (row["account_id"], row["journal_ref"])
    cur = conn.execute("UPDATE paper_live_positions SET last_exit_attempt_ts = ? WHERE account_id = ? "
                       "AND journal_ref = ? AND state = ? AND exit_ticket_id IS ? AND exit_started_at IS ? "
                       "AND last_exit_attempt_ts IS ? "
                       "AND (last_exit_attempt_ts IS NULL OR last_exit_attempt_ts <= ?)",
                       (claim, *ident, STATE_EXITING, row.get("exit_ticket_id"), row.get("exit_started_at"),
                        read, settled_before))
    owned = cur.rowcount == 1
    conn.commit()
    if owned:
        return {"owned": True, "claim": claim}
    now_row = conn.execute("SELECT state, exit_ticket_id, exit_started_at, last_exit_attempt_ts FROM "
                           "paper_live_positions WHERE account_id = ? AND journal_ref = ?", ident).fetchone()
    if now_row is not None and tuple(now_row) == (STATE_EXITING, row.get("exit_ticket_id"),
                                                  row.get("exit_started_at"), read):
        return {"owned": False, "status": "partial_held",
                "reason": f"an exit attempt on this row was already claimed at {read} — inside the "
                          f"{interval_s}-s quote interval it is in flight or was just tried; retried after it"}
    return {"owned": False, "status": "exit_not_owned",
            "reason": f"row is '{now_row[0] if now_row else None}' and no longer as {actor} read it — "
                      "another actor owns this exit; nothing issued or settled"}


def _complete_exit(conn, row: dict, chain: dict, quote_ts: str, now: datetime, venue_mod=None,
                   interval_s: int = None) -> dict:
    """Close ONLY the legs a partly filled exit basket left open (audit
    F05), at their remaining quantity, on a chain FETCHED this tick and
    crossed like every live exit (a long sold at the bid, a short bought
    back at the ask). The exit decision was made and half executed, so
    there is no predicate and no price hold here: the remainder is no
    longer the defined-risk structure the holds protect (a short leg left
    open can lose more than the spread's max loss by expiry). A leg with no
    usable quote waits (`partial_held`). Issues only under `_claim` (one
    actor, never inside a quote interval of the last attempt). Settles
    from the fills of BOTH tickets, UNCLAMPED (L1 review): what the legging
    cost or made is booked, named `legged_beyond_bounds` when it is
    outside the bounds."""
    from src import oms, plan_tracker as pt, portfolio_manager as pm
    ref = row["journal_ref"]
    ex = _exit_fills(conn, row)
    if ex["in_flight"] or ex["complete"] or not ex["filled_any"]:
        # not a partial basket any more (the resume pass owns these states)
        return _resolve_attempt(conn, row, ex, now, row.get("exit_ticket_id"), row.get("exit_started_at"),
                                detail={"completion": True})
    open_legs = [x for x in ex["legs"] if x["remaining"] > 0]
    qtys = {x["remaining"] for x in open_legs}
    lot = int(row["lot_size"])
    if len(qtys) != 1 or next(iter(qtys)) % lot:
        # one exit ticket carries ONE lot count for all its legs. The paper
        # venue fills a leg whole (fill fraction 1.0), so this is not reached
        # in production; a remainder it cannot express waits, named.
        reason = ("the open remainder (" + ", ".join(f"{_leg_label(x['leg'])} {x['remaining']}" for x in open_legs)
                  + f") is not one whole-lot basket of lot size {lot} — it cannot be ticketed as one exit; "
                  "held for a human")
        _alert_exit(conn, row, EVENT_EXIT_PARTIAL, reason)
        return {"status": "partial_held", "journal_ref": ref, "reason": reason}
    lots = next(iter(qtys)) // lot
    prices = {}
    for x in open_legs:
        price, why = crossed_close_price(x["leg"], leg_quote(chain, x["leg"]))
        if price is None:
            return {"status": "partial_held", "journal_ref": ref,
                    "reason": f"closing the open remainder: {_leg_label(x['leg'])}: {why}"}
        prices[x["key"]] = price
    limits = {k: max(TICK_FLOOR, float(v)) for k, v in prices.items()}
    resolution = row.get("exit_resolution") or ex.get("note_resolution") or "resumed_exit"
    got = _claim(conn, row, now, interval_s)
    if not got["owned"]:
        return {"status": got["status"], "journal_ref": ref, "reason": got["reason"]}
    claim = got["claim"]

    def _fence(tids: dict) -> bool:
        # F06 residual, as in _exit: the completion ticket is attached to
        # THIS claim (the row unchanged since it, the claim stamp still
        # ours) before the venue may fill it; otherwise it is cancelled
        # unswept. A ticket id this row already has is left alone (below).
        new = tids.get(row["account_id"])
        if new in ex["tickets"]:
            return True
        cur = conn.execute("UPDATE paper_live_positions SET exit_ticket_id = ? WHERE account_id = ? "
                           "AND journal_ref = ? AND state = ? AND exit_started_at IS ? AND exit_ticket_id IS ? "
                           "AND last_exit_attempt_ts IS ?",
                           (new, row["account_id"], ref, STATE_EXITING, row.get("exit_started_at"),
                            row.get("exit_ticket_id"), claim))
        conn.commit()
        return cur.rowcount == 1

    rec = pt._execute_paper_exit(_entry_like(row, legs=[x["leg"] for x in open_legs], lots=lots), limits,
                                 resolution, conn=conn, venue_mod=venue_mod, today=now.date(),
                                 accounts=[(row["account_id"], lots)], fence=_fence)
    tid = rec.get("ticket_id")
    if rec.get("fenced"):
        return {"status": "exit_not_owned", "journal_ref": ref, "ticket_id": tid,
                "reason": f"another actor moved this partly filled exit after this completion's claim "
                          f"({claim}) — row is '{_row_state(conn, row)}'; ticket {tid} cancelled unfilled"}
    if tid in ex["tickets"]:
        # exit ticket ids are keyed to the wall-clock second, and issuing is
        # idempotent: an id this row already has means NOTHING new was issued
        # (a tick always comes later in production) — never read the old one
        return {"status": "partial_held", "journal_ref": ref,
                "reason": f"the completion ticket's id {tid} is an earlier exit ticket's (same wall-clock "
                          "second) — nothing new was issued; retried on the next fetched chain"}
    if rec.get("error") and not tid:
        pm.paper_log_event(conn, row["account_id"], EVENT_UNFILLED, ref,
                           f"{resolution}: completion of a partly filled exit — door error "
                           f"({rec.get('error')}) — row left for resume")
        return {"status": "exit_error", "journal_ref": ref, "reason": rec.get("error")}
    if tid and rec.get("status") != oms.FILLED:
        try:
            oms.cancel_ticket(conn, tid, "live account: exit completion not filled")
        except Exception as exc:
            # the same record as _exit's failed cancel: what is still working
            # is unknown, the row stays `exiting` for the resume
            pm.paper_log_event(conn, row["account_id"], EVENT_UNFILLED, ref,
                               f"{resolution}: completion ticket {tid} of a partly filled exit not filled and "
                               f"its cancel failed ({exc}) — row left for resume")
            return {"status": "exit_error", "journal_ref": ref, "ticket_id": tid,
                    "reason": f"cancel failed: {exc}"}
    claimed = dict(row, last_exit_attempt_ts=claim, exit_ticket_id=tid or row.get("exit_ticket_id"))
    floored = {k: v for k, v in prices.items() if float(v) < TICK_FLOOR}
    res = _resolve_attempt(conn, claimed, _exit_fills(conn, claimed), now, tid, row.get("exit_started_at"),
                           detail={"completion": True, "quote_ts": quote_ts},
                           attempt={"ticket": tid, "floored": floored})
    return dict(res, ticket_id=res.get("ticket_id") or tid)


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


def filled_entry_ticket(conn, account: str, journal_ref: str):
    """The ticket id of this account's FILLED ENTRY ticket for the ref, or
    None — the evidence that a live position really was opened at the
    venue. ONE reading of it (audit F15): `_repair_unrecorded` opens the
    row from this ticket, and portfolio_manager.release_shadow_locks keeps
    the lock rather than release it at zero as 'never opened'. No OMS
    tables (an OMS never used) → None; read-only, no DDL."""
    from src import oms
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trade_tickets'").fetchone() is None:
        return None
    t = conn.execute("SELECT ticket_id FROM trade_tickets WHERE account_id = ? AND journal_ref = ? "
                     "AND note NOT LIKE 'EXIT %' AND status = ? ORDER BY issued_at DESC LIMIT 1",
                     (account, journal_ref, oms.FILLED)).fetchone()
    return t[0] if t else None


def keep_filled_entry_lock(conn, account: str, journal_ref: str, ticket_id: str, commit: bool = True) -> str:
    """Audit F15: a LIVE lock with no position row whose entry ticket
    FILLED (`filled_entry_ticket`) is KEPT for the one repair door
    (`_repair_unrecorded`, on the next live tick, under the tick lock),
    never released at zero as 'never opened'. Names it — one
    `live_lock_kept_filled_entry` row per position per IST day — and
    returns the reason. Both doors that would release such a lock at zero
    use it: portfolio_manager.release_shadow_locks (the primary settled
    first) and portfolio_manager.expire_pending_lock (the approval's
    journal write failed too, so the entry still reads pending — L2
    review), which passes `commit=False`: the row joins its one
    transaction (F16)."""
    try:
        row = conn.execute("SELECT state, pnl_net FROM paper_live_positions WHERE account_id = ? "
                           "AND journal_ref = ?", (account, journal_ref)).fetchone()
        unread = None
    except Exception as exc:
        if not commit:
            raise           # inside the caller's transaction: its rollback decides (L2 residual (a))
        row, unread = None, exc
    if unread is not None:
        why = (f"entry ticket {ticket_id} FILLED and the live position row could not be read ({unread}) "
               "— lock kept")
    elif row is None:
        why = (f"entry ticket {ticket_id} FILLED but no live position row was recorded — lock kept; "
               "the next live tick opens the position from the ticket")
    elif row[0] == STATE_CLOSED:
        # L2 residual (c): a CLOSED row whose lock is still active (a crash
        # between the row close and the lock release) is not 'no row' — it
        # is the late lock _repair_late_locks releases at the row's P&L
        why = ((f"entry ticket {ticket_id} FILLED and its live position row is CLOSED (settled pnl "
                f"Rs.{float(row[1]):,.2f}) but its lock is still active — lock kept; the next live tick "
                "releases it at that pnl") if row[1] is not None else
               (f"entry ticket {ticket_id} FILLED and its live position row is CLOSED with no settled pnl "
                "recorded, its lock still active — lock kept; nothing releases it on its own: needs a "
                "human look"))
    else:
        why = (f"entry ticket {ticket_id} FILLED and its live position row is {row[0]} — lock kept; "
               "the position settles on its own quotes")
    _log_once_a_day(conn, {"account_id": account, "journal_ref": journal_ref}, EVENT_LOCK_KEPT_FILLED, why,
                    commit=commit)
    return why


def _repair_unrecorded(conn, account: str, now: datetime) -> list:
    """A FILLED entry ticket with a live lock but no position row (the row
    insert failed at approval): open the position from the ticket. The
    primary's settlement keeps such a lock for this pass (audit F15)."""
    from src import oms
    ensure_schema(conn)
    fixed = []
    locks = conn.execute("SELECT journal_ref FROM paper_margin_locks WHERE account_id = ? AND released_at IS NULL",
                         (account,)).fetchall()
    for (ref,) in (tuple(r) for r in locks):
        if conn.execute("SELECT 1 FROM paper_live_positions WHERE account_id = ? AND journal_ref = ?",
                        (account, ref)).fetchone():
            continue
        tid = filled_entry_ticket(conn, account, ref)
        if not tid:
            continue
        view = oms.ticket_view(conn, tid) or {}
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
        try:
            open_position(conn, account, entry_like, view, quote_ts=None, now=now)
        except LiveEntryRefused as exc:
            # audit F11: fills that invert the structure are never recorded
            # with bounds from their sign — named (one row + one card a
            # day), the lock kept, and the next ref is still repaired
            note_refused_fill(conn, account, ref, tid, str(exc))
            continue
        except Exception as exc:
            # lows residual A2 (F15): ONE ref's failure is that ref's alone —
            # it used to escape the loop, so every other filled-but-unrecorded
            # position of the account (and _repair_late_locks) waited behind
            # it for as long as it kept failing. Named once a day; the lock
            # is kept, and the next tick tries this ref again.
            if conn.in_transaction:
                conn.rollback()
            print(f"  (live account: repair of {ref} failed: {exc} — lock kept, retried next tick)")
            _log_once_a_day(conn, {"account_id": account, "journal_ref": ref}, EVENT_REPAIR_FAILED,
                            f"FILLED entry ticket {tid} could not be recorded as a position ({exc}) — "
                            "lock kept; retried every tick")
            continue
        fixed.append(ref)
    return fixed


def _alert_exit(conn, row: dict, event_type: str, detail: str) -> bool:
    """Decision #133 (Architect ruling 2026-10-09): a half-filled or stuck
    live exit is written as its once-a-day ledger row AND paged — ONE
    `live_exit_needs_review` Discord card per position per event type per
    IST day (the event row is the de-dup, so a state repeated every tick
    never pages twice). The card is in notifier.BUDGET_ALWAYS: sent at once,
    never spooled to the evening digest. Fail-open: a failed card never
    blocks the exit path. True when the row (and so the card) was new."""
    if not _log_once_a_day(conn, row, event_type, detail):
        return False
    try:
        from src.notifier import fire_broadcast
        fire_broadcast({"event": EXIT_REVIEW_CARD, "ticker": row.get("ticker") or row["account_id"],
                        "short_id": row["journal_ref"], "date": _now().date().isoformat(),
                        "description": f"🚨 {row['account_id']} `{row['journal_ref']}` "
                                       f"({row.get('ticker') or '?'}): {detail}"})
    except Exception as exc:
        print(f"  (live account: exit review card for {row['journal_ref']} skipped: {exc})")
    return True


def note_refused_fill(conn, account: str, journal_ref: str, ticket_id: str, why: str) -> str:
    """A FILLED live entry ticket that open_position refused
    (LiveEntryRefused: its fills invert the structure, audit F11; or a row
    for the ref already exists, L2 residual (b)). Nothing books it and
    nothing will on its own: the lock is kept (F15 — a filled trade is never
    released at zero as 'never opened') and a human has to look. ONE
    `live_filled_entry_refused` row and ONE Discord card per position per
    IST day — the event row IS the de-dup, so a repair retried every tick
    never pages twice. Fail-open: a failed card never blocks the caller.
    Returns the detail."""
    detail = (f"entry ticket {ticket_id} FILLED but the live position was refused: {why} — lock kept; "
              "nothing opens or settles it on its own: needs a human look")
    if _log_once_a_day(conn, {"account_id": account, "journal_ref": journal_ref}, EVENT_FILLED_REFUSED, detail):
        try:
            from src.notifier import fire_broadcast
            fire_broadcast({"event": "live_entry_needs_review", "ticker": account, "short_id": journal_ref,
                            "date": _now().date().isoformat(),
                            "description": f"⚠️ {account} `{journal_ref}`: {detail}"})
        except Exception as exc:
            print(f"  (live account: review card for {journal_ref} skipped: {exc})")
    return detail


def refs_to_manage(conn) -> list:
    """Audit F14: every ref the live arm still has to manage, whatever its
    switch says — a row not closed (marks, exits, finishing a partly
    filled basket), an ACTIVE lock whose entry ticket FILLED with no row yet
    (F15: the tick's repair opens it), and a CLOSED row whose lock is still
    active (the tick's late-lock repair releases it at the row's P&L).
    Sorted; read-only. RAISES on a database error: the caller must not read
    'unknown' as 'nothing to manage'."""
    from src import portfolio_manager as pm
    ensure_schema(conn)
    pm.ensure_accounts_schema(conn)
    refs = set()
    for acct in sorted(_accounts()):
        refs.update(r["journal_ref"] for r in open_rows(conn, acct))
        for (ref,) in (tuple(r) for r in conn.execute(
                "SELECT journal_ref FROM paper_margin_locks WHERE account_id = ? AND released_at IS NULL",
                (acct,)).fetchall()):
            state = position_state(conn, acct, ref)
            if state == STATE_CLOSED or (state is None and filled_entry_ticket(conn, acct, ref)):
                refs.add(ref)
    return sorted(refs)


def announce_manage_only(conn, refs: list) -> bool:
    """Audit F14: the arm's switch is OFF but it still holds `refs` — the
    live loop ticks them anyway (marks + exits only; no new live entry can
    open, the switch gates every entry at approval). ONE
    `live_arm_off_managing` row and ONE Discord card per IST day (a
    restarted scheduler does not page again). Fail-open on the card; the
    event write raises (the caller prints it). True when announced."""
    from src import portfolio_manager as pm
    pm.ensure_accounts_schema(conn)
    account = pm.ACCOUNT_PAPER_2L_LIVE
    day = pm._now_iso()[:10]
    if conn.execute("SELECT 1 FROM paper_account_events WHERE account_id = ? AND event_type = ? "
                    "AND substr(ts, 1, 10) = ? LIMIT 1", (account, EVENT_ARM_OFF, day)).fetchone():
        return False
    detail = (f"the live-quote arm is switched OFF but holds {len(refs)} position(s) "
              f"({', '.join(refs)}) — the live loop still marks and exits them on crossed quotes "
              "(manage-only); no new live entry opens while the switch is off")
    pm.paper_log_event(conn, account, EVENT_ARM_OFF, None, detail)
    try:
        from src.notifier import fire_broadcast
        fire_broadcast({"event": "live_arm_off_managing", "ticker": account, "date": day,
                        "description": (f"🟡 {account}: {detail}. To end it: let these positions exit, "
                                        "or switch the arm back on (audit F14).")})
    except Exception as exc:
        print(f"  (live account: manage-only card skipped: {exc})")
    return True


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
    signal, unconfirmed = "hold", None
    # Audit F20: the tracker's and the advisory's ONE gate, #110 kill switch
    # included — `ratchet_enabled: false` puts this arm back on the static
    # 65% take with the primary and the shadows (its stored peak/lock are
    # kept as they are, unused, exactly as the tracker ignores saved rungs).
    if pr.applies({"strategy": row.get("strategy"), "direction": row.get("direction")}, max_profit):
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
    # the tracker's own forced-exit window (audit F23: it includes the last
    # session before a holiday-moved Monday expiry) — one predicate, so the
    # live arm and its control can never disagree about when to be out
    if signal == "hold" and pt.in_forced_exit_window(row["ticker"], row["expiry"], today):
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


def _record_mark(conn, row: dict, ev: dict, quote_ts: str) -> None:
    """Persist one mark. `last_mark_ts` is the CHAIN's fetch time (audit
    F19), the same stamp as `quote_ts`: a mark is as old as the quotes it
    was priced on. It used to be the tick's clock, so a chain left cached
    by a failing door was re-stamped as a fresh mark on every 60-s tick
    for as long as the door stayed down."""
    conn.execute("UPDATE paper_live_positions SET last_mark_ts = ?, last_mark_ps = ?, last_profit_ps = ?, "
                 "last_capture_pct = ?, quote_ts = ?, ratchet_peak_pct = ?, ratchet_lock_pct = ? "
                 "WHERE account_id = ? AND journal_ref = ? AND state = ?",
                 (quote_ts, ev["mark_ps"], ev["profit_ps"], ev["capture_pct"], quote_ts, ev["peak"], ev["lock"],
                  row["account_id"], row["journal_ref"], STATE_OPEN))
    conn.commit()


# ------------------------------------------------------------- chain age (audit F19)

def _ts(value):
    """A stamp this module wrote (`_iso`: naive IST, seconds) as a naive
    datetime, or None when it is absent or does not parse."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _entered_at(row: dict):
    """When the position came to exist: the later of `opened_at` and
    `entry_quote_ts` (the approval re-quote's moment), or None when neither
    parses."""
    stamps = [t for t in (_ts(row.get("opened_at")), _ts(row.get("entry_quote_ts"))) if t is not None]
    return max(stamps) if stamps else None


def _predates_entry(row: dict, quote_ts) -> bool:
    """Audit F19: True when the chain stamped `quote_ts` was fetched BEFORE
    this position existed. `_CHAIN_CACHE` is shared by every row on its
    (ticker, expiry) and the approval re-quote never seeds it, so a second
    position on a cached key used to be marked first on quotes from before
    its own entry — a market that moved against it since the fetch read as
    a profit, a peak it never had and a rung sighting `_confirm_rung` then
    counted as one of its two reads. Both stamps are on the tick's clock
    (`_fetch` stamps the tick's `now`, which is never later than the real
    fetch — the safe direction). A stamp that does not parse cannot order
    the two: the row is marked as before (refusing it for good would leave
    it with no pre-expiry exit at all)."""
    entered, fetched = _entered_at(row), _ts(quote_ts)
    return entered is not None and fetched is not None and fetched < entered


# ------------------------------------------------------------- visibility (audit F02)

def _exit_window(row: dict, today: date) -> tuple:
    """(inside the forced-exit window?, days to expiry) — the predicate
    evaluate's pre_expiry_exit uses (the tracker's own,
    `plan_tracker.in_forced_exit_window`)."""
    from src import plan_tracker as pt
    days_left = (date.fromisoformat(row["expiry"]) - today).days
    return pt.in_forced_exit_window(row["ticker"], row["expiry"], today), days_left


def _log_once_a_day(conn, row: dict, event_type: str, detail: str, commit: bool = True) -> bool:
    """Write ONE `paper_account_events` row per (account, position,
    event_type) per IST day — the day of the stamp pm.paper_log_event
    writes, so the check and the record share one clock. A held or
    abstaining row repeats on every 60-s tick; the ledger needs to know
    that it happened and why, not 375 times a day. No Discord card
    (#102/#120 telemetry rule). Fail-open: a failed write is printed and
    the tick carries on — marks and exits never depend on their own
    telemetry. `commit=False`: the row joins the caller's open transaction
    (pm.paper_log_event's own flag; the schema check is D4-safe) — and a
    failure RAISES (L2 residual (a)): inside someone else's transaction a
    failed write may already have rolled that whole transaction back
    (SQLITE_FULL / IOERR / NOMEM, a trigger's RAISE(ROLLBACK)); swallowed,
    the caller would carry on in a fresh implicit transaction with its own
    earlier writes silently undone (expire_pending_lock's 'nothing expires
    unless all does', F16). The caller's rollback-on-failure must run.
    Returns True when a row was written."""
    from src import portfolio_manager as pm
    try:
        pm.ensure_accounts_schema(conn)
        day = pm._now_iso()[:10]
        if conn.execute("SELECT 1 FROM paper_account_events WHERE account_id = ? AND journal_ref = ? "
                        "AND event_type = ? AND substr(ts, 1, 10) = ? LIMIT 1",
                        (row["account_id"], row["journal_ref"], event_type, day)).fetchone():
            return False
        pm.paper_log_event(conn, row["account_id"], event_type, row["journal_ref"], detail, commit=commit)
        return True
    except Exception as exc:
        if not commit:
            raise
        print(f"  (live account: {event_type} event for {row.get('journal_ref')} skipped: {exc})")
        return False


def _note_abstained(conn, out: dict, row: dict, reason: str, today: date, event: bool = True) -> None:
    """Count AND name one abstention in the tick summary (audit F02: it
    used to be a bare counter that live_cycle threw away). Inside the
    forced-exit window it is also an event: evaluate cannot reach its
    signal block without a mark, so that abstention is the one that
    silently costs the pre-expiry exit and leaves the position to the
    expiry backstop. `event=False` (audit F19, L4 review): an abstention
    the next tick's fetch ends by construction — no chain was missing, the
    position only opened after this tick began — is named, never logged."""
    out["abstained"] += 1
    in_window, days_left = _exit_window(row, today)
    out["row_notes"].append({"account_id": row["account_id"], "journal_ref": row["journal_ref"],
                             "kind": "abstained", "reason": reason, "days_left": days_left,
                             "in_exit_window": in_window, "last_mark_ts": row.get("last_mark_ts")})
    if in_window and event:
        _log_once_a_day(conn, row, EVENT_MARK_ABSTAINED,
                        f"no usable mark inside the forced-exit window ({days_left}d to expiry "
                        f"{row['expiry']}): {reason} — the pre-expiry exit cannot fire on this chain; "
                        f"last mark {row.get('last_mark_ts') or 'never'}; the expiry backstop still covers it")


def _held_detail(signal: str, res: dict) -> str:
    if res.get("status") == "held_loss_beyond_max":
        return (f"{signal} held: a crossed exit would lose {float(res['would_loss_ps']):g}/share, beyond "
                f"the structure's max loss {float(res['max_loss_ps']):g} — position kept; the expiry "
                "backstop can never do worse")
    return f"{signal} held ({res.get('status')}): {res.get('reason')} — position kept"


def _standing_hold(row: dict, ev: dict, src, now: datetime, interval_s: int):
    """Audit F04: the hold `_exit` would answer for a predicate that fired
    on a CACHED chain, known WITHOUT a new fetch — or None (re-verify on a
    fresh chain, as before). A held exit (`_exit` writes nothing for a
    price hold) used to re-fetch the chain on every 60-s tick for as long
    as the hold lasted, ignoring LIVE_QUOTE_INTERVAL_SECONDS:
      * the same predicate was already refused on its PRICE on THIS cached
        snapshot (`_HELD_EXIT`, keyed to the cache entry): the same quotes
        give the same refusal; the fetch loop replaces the snapshot once
        it is a quote interval old, and that fresh chain re-decides;
      * the row's last exit attempt is inside the quote interval: `_exit`
        refuses any price then (`held_recent_attempt`, the persisted
        last_exit_attempt_ts) — and only then: the retry is due at the
        attempt + one interval, whatever snapshot is cached (L2 review:
        a memoised recent-attempt hold outlived it by up to an interval).
    A DIFFERENT predicate, or the same one on a newer snapshot, is new
    evidence and re-verifies as before."""
    memo = _HELD_EXIT.get((row["account_id"], row["journal_ref"]))
    if memo is not None and memo["src"] is src and memo["signal"] == ev["signal"]:
        return dict(memo["res"], held_on_cached_chain=True)
    last = row.get("last_exit_attempt_ts")
    if last:
        try:
            if (now.replace(tzinfo=None) - datetime.fromisoformat(last)).total_seconds() < interval_s:
                return {"status": "held_recent_attempt", "journal_ref": row["journal_ref"], "last": last,
                        "held_on_cached_chain": True}
        except ValueError:
            pass
    return None


def _fetch(key: tuple, chain_fn, sleep_fn, now_epoch_fn, now: datetime = None) -> tuple | None:
    """One paced chain fetch; returns (iso, chain) or None. A failed fetch
    leaves the cache as it was. The iso stamp is on the TICK's clock (`now`,
    the clock `opened_at` / `entry_quote_ts` / `last_mark_ts` are written
    on — audit F19 compares them); the epoch is the pacing/age clock."""
    _pace(sleep_fn, now_epoch_fn)
    try:
        chain = chain_fn(key[0], key[1])
    except Exception as exc:
        print(f"  (live account: chain {key[0]} {key[1]} failed: {exc})")
        chain = None
    if not chain:
        return None
    stamp = now if now is not None else datetime.fromtimestamp(now_epoch_fn(), IST)
    _CHAIN_CACHE[key] = (now_epoch_fn(), _iso(stamp), chain)
    return _CHAIN_CACHE[key][1], chain


def _tick_lock(path):
    """Take the host-wide single-instance tick lock without waiting (audit
    F06). Returns (handle, None) when held; (None, reason) when another
    process holds it; (None, None) when no guard can be taken (no fcntl,
    or the file cannot be opened) — fail-open, so the live arm's positions
    are still marked and exited. Unguarded, ownership rests on the
    compare-and-set chain alone, which the F06 residual fix made
    sufficient: every exit ticket is attached to its attempt by a fence
    BEFORE the venue may fill it (a stale actor's ticket is cancelled
    unswept), `_exit` settles only the attempt it stamped, and an
    unguarded resume leaves a young attempt (working ticket, or none yet)
    untouched for one quote interval."""
    if fcntl is None:
        return None, None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
    except OSError as exc:
        print(f"  (live account: tick lock {path} unavailable, ticking unguarded: {exc})")
        return None, None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None, (f"another process is ticking the live account (tick lock {path} is held) — "
                      "this tick skipped")
    except OSError as exc:
        handle.close()
        print(f"  (live account: tick lock {path} could not be taken, ticking unguarded: {exc})")
        return None, None
    return handle, None


def _release_tick_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def tick(now: datetime = None, conn=None, chain_fn=None, sleep_fn=time.sleep, now_epoch_fn=time.time,
         interval_s: int = None, max_fetches: int = DEFAULT_MAX_FETCHES,
         budget_s: float = DEFAULT_BUDGET_SECONDS, cutoff: tuple = DEFAULT_CUTOFF,
         venue_mod=None, require_market_open: bool = True) -> dict:
    """One pass: resume half-done exits, refresh due chains (paced, capped,
    never after the cutoff), mark every open row, exit on a predicate
    (re-verified on a fresh chain when the mark came from the cache), and
    close the open legs of a partly filled exit on a freshly fetched chain
    (audit F05). Never raises. Returns a summary: counts, `exits` (every
    exit outcome with its row, holds included), `row_notes` (every
    abstention, every unconfirmed cached predicate and every partly filled
    exit still waiting, with its row and reason — audit F02), `resumes`,
    `repaired` / `late_released`, `skipped`.

    Audit F06: the tick first takes the single-instance lock on
    `TICK_LOCK_FILE` (read at call time; tests point it at tmp). Held by
    another process → the tick is skipped, named."""
    from src.config import LIVE_QUOTE_INTERVAL_SECONDS
    now = now or _now()
    interval_s = LIVE_QUOTE_INTERVAL_SECONDS if interval_s is None else int(interval_s)
    out = {"ts": _iso(now), "rows": 0, "fetched": 0, "marked": 0, "abstained": 0, "exits": [],
           "resumed": 0, "skipped": None, "row_notes": []}
    if require_market_open and not _market_open(now):
        out["skipped"] = "market closed"
        return out
    lock, held_elsewhere = _tick_lock(TICK_LOCK_FILE)
    if held_elsewhere:
        out["skipped"] = held_elsewhere
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
        partial = set()
        for row in [r for r in rows if r["state"] == STATE_EXITING]:
            try:
                res = _resume_exiting(conn, row, now, interval_s, guarded=lock is not None)
                if res.get("young"):
                    continue        # in flight inside its quote interval (F06 residual): untouched
                if res.get("status") == "partial":
                    partial.add((row["account_id"], row["journal_ref"]))
                    if not res.get("cancelled"):
                        # a standing partial basket (audit F05): nothing moved
                        # since the last tick — the completion pass below
                        # names it (de-duplicated), not a resume line per tick
                        continue
                out["resumed"] += 1
                out.setdefault("resumes", []).append(
                    {"account_id": row["account_id"], "journal_ref": row["journal_ref"], **res})
            except Exception as exc:
                print(f"  (live account: resume {row['journal_ref']} failed: {exc})")
        after = open_rows(conn)
        # decision #133 (Architect ruling 2026-10-09): an exit still not done
        # EXIT_STUCK_AFTER_INTERVALS quote intervals after it started (a door
        # error, a failed cancel, a ticket that never resolves) is STUCK —
        # paged once a day per position. A partly filled basket has its own
        # card (live_exit_partial), so it is not paged twice.
        try:
            stuck_after = EXIT_STUCK_AFTER_INTERVALS * interval_s
            for r in after:
                started = _ts(r.get("exit_started_at"))
                if (r["state"] != STATE_EXITING or started is None
                        or (r["account_id"], r["journal_ref"]) in partial):
                    continue
                age = (now.replace(tzinfo=None) - started).total_seconds()
                if age >= stuck_after:
                    _alert_exit(conn, r, EVENT_EXIT_STUCK,
                                f"{r.get('exit_resolution') or 'exit'} has been 'exiting' since "
                                f"{r.get('exit_started_at')} ({int(age // 60)} min) and has not completed — "
                                "the position is not settled and its lock is held: needs a human look")
        except Exception as exc:
            print(f"  (live account: stuck-exit check skipped: {exc})")
        rows = [r for r in after if r["state"] == STATE_OPEN]
        completing = [r for r in after if r["state"] == STATE_EXITING
                      and (r["account_id"], r["journal_ref"]) in partial]
        # chains due, stalest first, capped per tick and by the clock (a
        # partly exited row's chain is fetched on the same schedule)
        start = now_epoch_fn()
        past_cutoff = (now.hour, now.minute) >= cutoff
        keys = sorted({(r["ticker"], r["expiry"]) for r in rows + completing},
                      key=lambda k: _CHAIN_CACHE.get(k, (0.0,))[0])
        # Audit F19: a key whose cached chain predates one of its open rows
        # is due NOW, whatever its age — that row may not be marked on it.
        behind = {(r["ticker"], r["expiry"]) for r in rows
                  if (r["ticker"], r["expiry"]) in _CHAIN_CACHE
                  and _predates_entry(r, _CHAIN_CACHE[(r["ticker"], r["expiry"])][1])}
        fresh, failed = set(), set()
        for key in keys:
            age = now_epoch_fn() - _CHAIN_CACHE.get(key, (0.0,))[0]
            if key in _CHAIN_CACHE and age < interval_s and key not in behind:
                continue
            if past_cutoff or out["fetched"] >= max_fetches \
                    or now_epoch_fn() - start + CHAIN_PACE_SECONDS > budget_s:
                break
            if _fetch(key, chain_fn, sleep_fn, now_epoch_fn, now):
                fresh.add(key)
            else:
                failed.add(key)
            out["fetched"] += 1
        for row in rows:
            try:
                key = (row["ticker"], row["expiry"])
                cached = _CHAIN_CACHE.get(key)
                if not cached:
                    _note_abstained(conn, out, row,
                                    "no chain: the fetch failed this tick" if key in failed else
                                    "no chain yet: past the fetch cutoff" if past_cutoff else
                                    "no chain yet: the per-tick fetch cap or time budget was reached",
                                    now.date())
                    continue
                src = cached                 # which fetch this read is on (F01 rung confirmation)
                _, quote_ts, chain = cached
                if _predates_entry(row, quote_ts) and key in fresh:
                    # Audit F19 (L4 review): fetched THIS tick, but `_fetch`
                    # stamps the tick's start, and this position opened
                    # after it (an approval landing while the tick ran).
                    # Not marked on it — the stamp cannot prove the quotes
                    # are its own — and nothing is missing: the stamp
                    # predates the row, so the key is due on the next tick
                    # (`behind`). Named, never a forced-exit-window event.
                    _note_abstained(conn, out, row,
                                    f"the chain fetched this tick is stamped at the tick's start ({quote_ts}), "
                                    f"before this position opened at {_iso(_entered_at(row))}; marked on the "
                                    "next tick's fetch", now.date(), event=False)
                    continue
                if _predates_entry(row, quote_ts):
                    # Audit F19: quotes from before this position existed
                    # never mark it or move its ratchet — it waits for the
                    # fresh fetch its key is now due for (above).
                    why = ("the fetch failed this tick" if key in failed else
                           "past the fetch cutoff" if past_cutoff else
                           "the per-tick fetch cap or time budget was reached")
                    _note_abstained(conn, out, row,
                                    f"no chain since this position opened: the cached chain ({quote_ts}) "
                                    f"predates its entry ({_iso(_entered_at(row))}) and {why} — it waits "
                                    "for a fresh fetch", now.date())
                    continue
                # Audit F19: a chain at least a quote interval old here was
                # due and NOT refreshed (the fetch failed, the per-tick cap or
                # budget ran out, or it is past the cutoff). It may still
                # MARK the row — stamped with its own fetch time — but it
                # does not move the ratchet.
                stale = key not in fresh and now_epoch_fn() - cached[0] >= interval_s
                ev = evaluate(row, chain, now.date())
                if not ev["ok"]:
                    _note_abstained(conn, out, row, ev["reason"], now.date())
                    continue
                standing = (_standing_hold(row, ev, src, now, interval_s)
                            if ev["signal"] != "hold" and key not in fresh else None)
                held_signal = ev["signal"]
                if standing:
                    # Audit F04: this exit is already refused on what the
                    # cache holds — no re-verify fetch inside the quote
                    # interval; the mark is recorded and the hold reported
                    # below, as every tick reports it.
                    ev = dict(ev, signal="hold")
                elif ev["signal"] != "hold" and key not in fresh:
                    # A predicate that fired on a CACHED chain acts only on a
                    # FRESH one. Past the cutoff, over budget, or with the
                    # fetch failing, the mark is recorded and the signal is
                    # forced to hold — the next tick's fresh chain decides.
                    got, attempted = None, False
                    if not past_cutoff and out["fetched"] < max_fetches \
                            and now_epoch_fn() - start + CHAIN_PACE_SECONDS <= budget_s:
                        got, attempted = _fetch(key, chain_fn, sleep_fn, now_epoch_fn, now), True
                        out["fetched"] += 1
                    if got:
                        fresh.add(key)
                        quote_ts, chain = got
                        src = _CHAIN_CACHE.get(key)
                        stale = False
                        ev2 = evaluate(row, chain, now.date())
                        if not ev2["ok"]:
                            _note_abstained(conn, out, row, f"re-verify of {ev['signal']}: {ev2['reason']}",
                                            now.date())
                            continue
                        ev = ev2
                    else:
                        why = ("past the fetch cutoff" if past_cutoff else
                               "the re-verify fetch failed" if attempted else
                               "the per-tick fetch cap or time budget was reached")
                        out["row_notes"].append(
                            {"account_id": row["account_id"], "journal_ref": row["journal_ref"],
                             "kind": "unconfirmed", "days_left": ev.get("days_left"),
                             "reason": f"{ev['signal']} fired on a cached chain ({quote_ts}) and {why} "
                                       "— held for the next tick's fresh chain"})
                        ev = dict(ev, signal="hold")
                        out["unconfirmed"] = out.get("unconfirmed", 0) + 1
                if stale:
                    # Audit F19: the record keeps its peak and lock, and a
                    # pending rung sighting is neither added nor forgotten —
                    # an old snapshot is no evidence either way (like an
                    # abstaining read; a cached predicate on it was already
                    # held above for a fresh chain).
                    ev = dict(ev, peak=row.get("ratchet_peak_pct"), lock=row.get("ratchet_lock_pct"),
                              unconfirmed=None)
                    out["stale_marks"] = out.get("stale_marks", 0) + 1
                else:
                    # A new ratchet rung is persisted only on a second, later
                    # fetch (F01); the mark itself is recorded either way.
                    ev = _confirm_rung(row, ev, src)
                if ev.get("rung"):
                    out[f"rung_{ev['rung']}"] = out.get(f"rung_{ev['rung']}", 0) + 1
                _record_mark(conn, row, ev, quote_ts)
                out["marked"] += 1
                if standing:
                    out["exits"].append({"account_id": row["account_id"], "journal_ref": row["journal_ref"],
                                         "signal": held_signal, **standing})
                    if standing.get("status") in HELD_ON_PRICE:
                        _log_once_a_day(conn, row, EVENT_EXIT_HELD, _held_detail(held_signal, standing))
                elif ev["signal"] != "hold":
                    row = dict(row, ratchet_peak_pct=ev["peak"], ratchet_lock_pct=ev["lock"], quote_ts=quote_ts)
                    res = _exit(conn, row, ev["prices"], ev["signal"], now, venue_mod=venue_mod,
                                interval_s=interval_s)
                    out["exits"].append({"account_id": row["account_id"], "journal_ref": row["journal_ref"],
                                         "signal": ev["signal"], **res})
                    # Audit F04: remember a PRICE hold against the snapshot it
                    # was decided on (`src`), so the next ticks' cached reads
                    # of it do not re-fetch to be refused again. Not a
                    # recent-attempt hold (L2 review): `_standing_hold` reads
                    # that one off the row, and it lapses with the attempt's
                    # interval, not with this snapshot.
                    rk = (row["account_id"], row["journal_ref"])
                    if res.get("status") in HELD_ON_PRICE:
                        _HELD_EXIT[rk] = {"src": src, "signal": ev["signal"], "res": res}
                    else:
                        _HELD_EXIT.pop(rk, None)
                    if res.get("status") in HELD_ON_PRICE:
                        # Audit F02: a hold repeats every tick and wrote
                        # nothing; the ledger now gets one row a day.
                        _log_once_a_day(conn, row, EVENT_EXIT_HELD, _held_detail(ev["signal"], res))
            except Exception as exc:
                print(f"  (live account: {row.get('journal_ref')} tick failed: {exc})")
        # Audit F05: a basket that closed only SOME legs is finished on a
        # chain FETCHED this tick (never a cached one — the same rule as a
        # cached predicate), its open legs only. Its chain is due on the
        # normal schedule, so a remainder that cannot fill yet is retried
        # once per quote interval, not re-fetched every 60 s.
        for row in completing:
            try:
                key = (row["ticker"], row["expiry"])
                cached = _CHAIN_CACHE.get(key)
                res = None
                if key in fresh and cached:
                    res = _complete_exit(conn, row, cached[2], cached[1], now, venue_mod=venue_mod,
                                         interval_s=interval_s)
                    if res.get("status") != "partial_held":
                        out["exits"].append({"account_id": row["account_id"], "journal_ref": row["journal_ref"],
                                             "signal": row.get("exit_resolution"), "completion": True, **res})
                        continue
                why = (res["reason"] if res else
                       "the fetch failed this tick" if key in failed else
                       "past the fetch cutoff" if past_cutoff else
                       "the chain was not fetched this tick (next fetch on the quote interval, or the "
                       "per-tick fetch cap or time budget was reached)")
                out["row_notes"].append(
                    {"account_id": row["account_id"], "journal_ref": row["journal_ref"], "kind": "partial_exit",
                     "days_left": (date.fromisoformat(row["expiry"]) - now.date()).days,
                     "reason": f"{row.get('exit_resolution') or 'exit'} basket partly filled; the open legs "
                               f"wait: {why}"})
            except Exception as exc:
                print(f"  (live account: completing {row.get('journal_ref')} failed: {exc})")
        open_keys = {(r["account_id"], r["journal_ref"]) for r in rows}
        for k in [k for k in _PENDING_RUNG if k not in open_keys]:
            _PENDING_RUNG.pop(k, None)              # a closed position's sighting is moot
        for k in [k for k in _HELD_EXIT if k not in open_keys]:
            _HELD_EXIT.pop(k, None)                 # ... and so is its standing hold (F04)
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
        _release_tick_lock(lock)


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


def _intrinsic(leg: dict, close) -> float:
    """A leg's value at expiry on the underlying's close (time value 0)."""
    return max(0.0, (float(close) - float(leg["strike"])) if leg["option_type"] == "CE"
               else (float(leg["strike"]) - float(close)))


def _waiting_reason(row: dict, bars: list, today: date, partial: bool = False) -> str:
    """Why an expired row still waits for its close. `partial` = a row an
    unfinished exit left `exiting` with SOME legs closed: with no close at
    all after the grace window it does not settle at the defined max loss
    like an open row, but on `_expire_exiting`'s partial rule (L2 review)."""
    from src import plan_tracker as pt
    days = (today - date.fromisoformat(row["expiry"])).days
    older = [b[0] for b in (bars or []) if b[0] <= row["expiry"]]
    if older:
        # audit F22: an earlier close is in the series but not the
        # expiry session's — wait for it rather than book the wrong day
        return (f"{row['ticker']} expired {row['expiry']} and the expiry session's own close "
                f"({pt.expiry_session(row['expiry']).isoformat()}) has not arrived yet — the newest "
                f"close is {max(older)} (day {days} of the {pt.EXPIRY_BACKSTOP_GRACE_DAYS}-day grace "
                f"window; after it the newest close settles, named {pt.STALE_CLOSE_BASIS})")
    after = ("the closed legs settle at their fills and the open legs at their conservative bound — an "
             "open long at 0, an open short at the structure's width, no frictions on them"
             if partial else "the defined max loss settles at zero frictions")
    return (f"{row['ticker']} expired {row['expiry']} and no daily close on or before expiry has "
            f"arrived yet (day {days} of the {pt.EXPIRY_BACKSTOP_GRACE_DAYS}-day grace window; after "
            f"it {after})")


def _expire_exiting(conn, row: dict, res, now: datetime, interval_s: int = None) -> dict:
    """Audit F08: the expiry backstop for a row left `exiting` past expiry
    — an exit the tick never finished (a door error, a crash, a basket cut
    between its legs on expiry day after the 15:27 fetch cutoff), when no
    tick can finish it any more: an expired contract has no chain. It used
    to be skipped (eod_sweep took OPEN rows only) and its lock was held for
    ever. `res` = plan_tracker._expiry_backstop's answer for the row (None
    = the expiry session's close has not arrived; the row waits like an
    open one).

    It goes through the SAME exclusive `_claim` as the tick's completion
    (this runs in the API process, without the tick lock), cancels any
    exit ticket still working (keeping each cancel's status), then decides
    on the OMS's fills across every EXIT ticket of the row (the L1 rule):
      every leg closed  → settled from the fills (`_settle_from_fills`);
      nothing filled    → the whole structure on the backstop, exactly as
                          an open row (clamped);
      some legs closed  → the closed legs at their ACTUAL fills
                          (`_booked_fills`), the open legs at the expiry
                          session's intrinsic (`_expiry_backstop`'s close —
                          the stale-close fallback after the grace window
                          included), booked like L1's legged exits:
                          UNCLAMPED, `legged_beyond_bounds` when outside
                          the bounds. With NO price data at all after the
                          grace window, an open long is valued at 0 and an
                          open short at the structure's width — the
                          conservative bound, never a mark-up (policy
                          default, see the module's L2 note).
    Every close is a compare-and-set on `exiting` (the state it read)."""
    from src import oms
    ref = row["journal_ref"]
    ex = _exit_fills(conn, row)
    if res is None and not ex["complete"] and not ex["in_flight"]:
        return {"status": "waiting", "journal_ref": ref, "filled_any": ex["filled_any"]}
    # A ticket still WORKING past expiry is cancelled now, even while the
    # expiry session's close has not arrived (lows residual A2, F08): left
    # live, any later venue sweep (the primary's exits run one) would fill it
    # at a limit priced before expiry, and the backstop would then book those
    # fills instead of the expiry close's intrinsic.
    got = _claim(conn, row, now, interval_s, actor="the expiry backstop")
    if not got["owned"]:
        return {"status": got["status"], "journal_ref": ref, "reason": got["reason"]}
    row = dict(row, last_exit_attempt_ts=got["claim"])
    cancelled = {}
    for tid in ex["in_flight"]:
        cancelled[tid] = oms.cancel_ticket(conn, tid, "live account: expiry backstop — exit still working "
                                                      "past expiry").get("status")
    if cancelled:
        ex = _exit_fills(conn, row)     # what the cancels left — a fill that beat them counts
    detail = {"expiry_backstop": True, "exit_resolution": row.get("exit_resolution")}
    if cancelled:
        detail["cancelled"] = cancelled
    if ex["in_flight"]:
        return {"status": "exit_in_flight", "journal_ref": ref,
                "reason": f"exit ticket(s) {', '.join(ex['in_flight'])} still have a leg working after the "
                          "cancel — retried next run"}
    if ex["complete"]:
        return _settle_from_fills(conn, row, ex, now, detail, expect_state=STATE_EXITING)
    if res is None:
        out = {"status": "waiting", "journal_ref": ref, "filled_any": ex["filled_any"]}
        if cancelled:
            out["cancelled"] = cancelled
        return out
    resolution, exit_mark_ps, _frac, _day, close, basis, close_day = res
    detail.update(close=close, settlement_close_date=close_day)
    if not ex["filled_any"]:
        frictions = 0.0 if close is None else \
            _frictions(row, {(float(l["strike"]), l["option_type"]): _intrinsic(l, close) for l in row["legs"]})
        return _settle(conn, row, exit_mark_ps, resolution, basis, frictions, now, detail=detail,
                       expect_state=STATE_EXITING)
    booked, fills, floored = _booked_fills(ex)
    qty = int(row["lots"]) * int(row["lot_size"])
    prices, fric, open_at = {}, {}, {}
    for x in ex["legs"]:
        leg = x["leg"]
        if close is not None:
            value = fric_value = _intrinsic(leg, close)
        else:
            # no price at all: the bound that can only overstate the loss
            value, fric_value = (0.0 if _sign(leg["side"]) > 0 else float(row["width_ps"])), 0.0
        if x["remaining"]:
            open_at[_leg_label(leg)] = round(value, 4)
        done = booked.get(x["key"], 0.0) * x["filled"]
        prices[x["key"]] = round((done + value * x["remaining"]) / qty, 4)
        fric[x["key"]] = round((done + fric_value * x["remaining"]) / qty, 4)
    mark = round(sum(_sign(x["leg"]["side"]) * prices[x["key"]] for x in ex["legs"]), 4)
    detail.update(fills={f"{k[0]:g}{k[1]}": v for k, v in fills.items()},
                  partial={"closed_at_fills": {_leg_label(x["leg"]): booked[x["key"]] for x in ex["legs"]
                                               if x["key"] in booked},
                           "open_at_expiry": open_at})
    if floored:
        detail["limit_floored"] = {f"{k[0]:g}{k[1]}": TICK_FLOOR for k in floored}
    _legged_detail(row, ex, mark, detail)
    return _settle(conn, row, mark, resolution, basis, _frictions(row, fric), now,
                   ticket_id=ex["filled_tickets"][-1], detail=detail, clamp=False, expect_state=STATE_EXITING)


def eod_sweep(conn, today: date = None, bars_fn=None, now: datetime = None, interval_s: int = None) -> dict:
    """Settle rows whose expiry has passed, exactly like the primary's
    `_expiry_backstop`: intrinsic at the expiry session's own close (audit
    F22); past the grace window, the newest earlier close
    (`stale_close_after_grace`) or — with no bars — the defined max loss at
    zero frictions. Never raises. `errors` holds "<ref>: <exception>" (the row
    stays open with its lock; the next run retries), `waiting` the refs
    still inside the grace window, `reasons` {ref: why it waits} — the
    caller prints both (audit F21).

    Audit F08 (L2): a row left `exiting` past expiry is resolved too
    (`_expire_exiting`), and named ONCE (L2 review): still waiting for its
    close → in `waiting`, its reason saying it was left `exiting`;
    anything else the backstop did with it → in `exiting` ({journal_ref,
    status, reason}). Every close here is a compare-and-set on the state
    the sweep READ (it runs in the API process, beside the tick); a row
    another actor moved meanwhile is named in `moved`. Then
    `_repair_journal_stamps` (F07): `restamped`."""
    from src import plan_tracker as pt
    today = today or _now().date()
    now = now or _now()
    out = {"settled": [], "waiting": [], "errors": [], "reasons": {}, "exiting": [], "moved": []}
    try:
        rows = [r for r in open_rows(conn) if r["state"] in (STATE_OPEN, STATE_EXITING)
                and r["expiry"] < today.isoformat()]
    except Exception as exc:
        out["errors"].append(str(exc))
        return out
    for row in rows:
        ref = row["journal_ref"]
        try:
            bars = None
            try:
                bars = (bars_fn or pt._daily_bars)(row["ticker"], row["opened_at"][:10])
            except Exception as exc:
                print(f"  (live account: bars for {row['ticker']} unavailable: {exc})")
            res = pt._expiry_backstop(_entry_like(row), bars or [], today)
            if row["state"] == STATE_EXITING:
                r = _expire_exiting(conn, row, res, now, interval_s)
                if r["status"] == "waiting":
                    # named ONCE, in `waiting` beside the open rows that wait
                    # (run_tracker prints a line per entry of each list; in
                    # both lists it was two lines on every hourly run)
                    out["waiting"].append(ref)
                    out["reasons"][ref] = (_waiting_reason(row, bars, today, partial=bool(r.get("filled_any")))
                                           + "; the row was left 'exiting' by an unfinished exit and settles "
                                             "with that close")
                    continue
                if r["status"] == "settled":
                    out["settled"].append(r)
                out["exiting"].append({"journal_ref": ref, "status": r["status"],
                                       "reason": r.get("reason") or _exiting_how(r)})
                continue
            if res is None:
                out["waiting"].append(ref)
                out["reasons"][ref] = _waiting_reason(row, bars, today)
                continue
            resolution, exit_mark_ps, _frac, _day, close, basis, close_day = res
            if basis == "no_price_data_max_loss":
                frictions = 0.0
            else:
                prices = {(float(l["strike"]), l["option_type"]): _intrinsic(l, close) for l in row["legs"]}
                frictions = _frictions(row, prices)
            # which session priced it (audit F22) — the expiry session's, or
            # an earlier one on the named stale fallback; a compare-and-set
            # on 'open' as read (L2: a tick may have stamped it meanwhile)
            s = _settle(conn, row, exit_mark_ps, resolution, basis, frictions, now,
                        detail={"close": close, "settlement_close_date": close_day}, expect_state=STATE_OPEN)
            if s["status"] == "settled":
                out["settled"].append(s)
            else:
                out["moved"].append(f"{ref}: {s.get('reason') or s['status']}")
        except Exception as exc:
            out["errors"].append(f"{ref}: {exc}")
    try:
        fixed = _repair_journal_stamps(conn)
        if fixed:
            out["restamped"] = fixed
    except Exception as exc:
        print(f"  (live account: journal stamp repair skipped: {exc})")
    return out


def _exiting_how(r: dict) -> str:
    """One line on how the backstop settled a row left `exiting` (F08)."""
    if r.get("status") != "settled":
        return r.get("status") or "?"
    if r.get("partial"):
        p = r["partial"]
        return (f"settled {r.get('basis')}: closed legs at their fills {p.get('closed_at_fills')}, open legs "
                f"at expiry {p.get('open_at_expiry')}, P&L Rs.{float(r.get('pnl_net') or 0):+,.2f}"
                + (" — LEGGED BEYOND BOUNDS" if r.get("legged_beyond_bounds") else ""))
    if r.get("basis") == "live_bid_ask":
        return f"settled from its exit fills {r.get('fills')}, P&L Rs.{float(r.get('pnl_net') or 0):+,.2f}"
    return f"nothing had filled: settled {r.get('basis')} like an open row, P&L Rs.{float(r.get('pnl_net') or 0):+,.2f}"

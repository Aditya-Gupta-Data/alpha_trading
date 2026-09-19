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

from datetime import datetime, timedelta, timezone

from src import brain_map
from src.config import (MAX_RISK_PER_TRADE_RS, OPTIONS_RISK_PER_TRADE_PCT,
                        PAPER_2L_ACCOUNT_ENABLED, PAPER_2L_STARTING_CAPITAL_RS)
from src.portfolio import span_stress_factor

IST = timezone(timedelta(hours=5, minutes=30))

STARTING_CAPITAL = 1_000_000.0   # Rs.10,00,000 simulated allocation pool
MAX_DRAWDOWN_PCT = 10.0          # hard-coded risk-of-ruin parameter
HALT_LATCH_EVENT = "ruin_halt_latched"   # the persistent risk-of-ruin brake
HALT_CLEAR_EVENT = "ruin_halt_cleared"   # (Dept 3 ruling 2026-08-17) —
                                 # arming is automatic, clearing is an
                                 # admin-only act: see clear_halt()
MAX_DAILY_LOSS_PCT = 3.0         # daily circuit breaker (merged from
                                 # next_gen_engine 2026-07-19): realized loss
                                 # today >= 3% of session-open equity halts
                                 # NEW entries for the rest of the IST day

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
    """IST wall-clock, naive-formatted (unchanged shape, correct date).
    Issue-16 discipline: stamps must never follow the host timezone — the
    VM runs UTC, and the daily circuit breaker's "today" boundary reads
    these stamps back."""
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def ist_today() -> str:
    return datetime.now(IST).date().isoformat()


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


def halt_latched(conn) -> bool:
    """True while a risk-of-ruin halt is LATCHED and not since cleared.

    The latch lives in `account_events` — the append-only trail this
    module already owns — as the LAST of a `ruin_halt_latched` /
    `ruin_halt_cleared` pair. No schema migration, and every arm and
    every clear is a permanent, timestamped, attributable row: a halt
    that can be silently un-set is not a halt.

    Deliberately its own event type, not the `risk_of_ruin_halt` rows
    `request_entry` writes on every rejection — those record a blocked
    entry, this records the state of the brake."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT event_type FROM account_events WHERE event_type IN (?, ?) "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (HALT_LATCH_EVENT, HALT_CLEAR_EVENT)).fetchone()
    return bool(row) and row[0] == HALT_LATCH_EVENT


def latch_halt(conn, why: str = "") -> bool:
    """Arm the latch (idempotent; True if this call armed it). Fail-open
    on a read-only connection: several dashboard/report readers call
    `trading_halted` over a `mode=ro` handle, and a brake that raises
    inside a read would take down the very panel meant to display it.
    The verdict is still HALTED either way — only the record is lost,
    and the next writer re-arms it."""
    try:
        if halt_latched(conn):
            return False
        log_event(conn, HALT_LATCH_EVENT, why or "risk-of-ruin threshold breached")
        return True
    except Exception as e:
        print(f"  (halt latch not persisted — {e})")
        return False


def clear_halt(conn, why: str = "", who: str = "owner") -> dict:
    """THE ONLY DOOR OUT of a latched halt — a deliberate admin action.

    Dept 3 ruling 2026-08-17 (the architect, after the capital-flow fix
    exposed the dilution trap): *"Throwing capital at a broken strategy
    does not fix the strategy."* A risk-of-ruin halt says the market
    logic is failing; a bank transfer is not evidence that it stopped
    failing. So the halt LATCHES, and nothing automatic — not a deposit,
    not a recovering drawdown, not a new day — takes it off.

    This SUPERSEDES the 2026-07-27 Walkaway ruling's "no override door"
    only in mechanism, not in spirit: resume is still a deliberate human
    decision, it is now merely a recorded one instead of hand-SQL.

    Refuses silently-empty calls: an un-halt with no stated reason is
    exactly the un-halt nobody can defend later."""
    ensure_schema(conn)
    if not why or not str(why).strip():
        return {"cleared": False,
                "reason": "refused: clearing a ruin halt requires a stated why"}
    if not halt_latched(conn):
        return {"cleared": False, "reason": "no latched halt to clear",
                "halted": trading_halted(conn)}
    dd = drawdown_pct(conn)
    log_event(conn, HALT_CLEAR_EVENT,
              f"latched ruin halt cleared by {who} at drawdown {dd:.2f}% "
              f"(limit {MAX_DRAWDOWN_PCT:g}%) — {why}")
    still = trading_halted(conn)   # re-latches instantly if still breached
    return {"cleared": True, "drawdown_pct": dd, "why": why, "who": who,
            "halted": still,
            "reason": ("cleared, but drawdown is still past the limit — "
                       "the latch re-armed immediately" if still
                       else "cleared; entries are live again")}


def trading_halted(conn) -> bool:
    """True while the risk-of-ruin brake is on. LATCHING (Dept 3 ruling
    2026-08-17): breaching the threshold arms a persistent latch, and the
    latch — not the live drawdown — is what keeps the brake on. Recovering
    equity, and above all a capital injection that merely dilutes the
    percentage, no longer resume trading by themselves; only
    `clear_halt()` does."""
    if halt_latched(conn):
        return True
    if drawdown_pct(conn) >= MAX_DRAWDOWN_PCT:
        latch_halt(conn, f"drawdown {drawdown_pct(conn):.2f}% >= "
                         f"{MAX_DRAWDOWN_PCT:g}% of peak equity")
        return True
    return False


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


def required_margin_for(proposal: dict, vix: float = None) -> float:
    """What a build_proposal() result blocks: the SPAN total (already
    hedge-offset by portfolio.calculate_span_margin) times the lots, times
    the ENTRY-TIME VIX-stress factor (owner decision 2026-07-19): in a
    panicky market the reservation grows upfront, so the margin-exhaustion
    check naturally chokes off how many trades fit. `vix` defaults to the
    proposal's own recorded VIX; calm/unknown VIX means factor 1.0 — the
    pre-stress number, byte-identical."""
    spread = proposal["spread"]
    base = (float(spread["margin"]["total_margin"])
            * int(spread.get("lots", proposal.get("lots", 1))))
    factor = span_stress_factor(vix if vix is not None else proposal.get("vix"))
    return round(base * factor, 2)


# --- the daily circuit breaker (merged from next_gen_engine/
# --- portfolio_risk_manager.py, its canonical target, 2026-07-19) --------

def realized_pnl_today(resolved_entries: list, today: str = None) -> float:
    """Sum of net P&L across journal-shaped rows RESOLVED today (IST).
    Rows without a resolution date or P&L are skipped — unknown is not a
    loss. Kept pure/journal-shaped so the simulator can replay the breaker;
    the LIVE gate reads the DB via daily_realized_pnl instead."""
    today = today or ist_today()
    total = 0.0
    for e in resolved_entries or []:
        stamp = e.get("resolved_at") or e.get("closed_at") or ""
        if not str(stamp).startswith(today):
            continue
        pnl = e.get("pnl_net", e.get("pnl"))
        if isinstance(pnl, (int, float)):
            total += float(pnl)
    return round(total, 2)


def check_daily_breaker(session_open_equity: float,
                        pnl_today: float,
                        max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT) -> dict:
    """The verdict. `halted=True` means NO NEW ENTRIES today.

    Fail-safe posture: an unusable equity figure (None/0/negative) returns
    halted=False with an explicit `error` — the daily breaker refusing to
    guess must not freeze the engine, because the lifetime drawdown halt
    is still armed underneath it."""
    if not session_open_equity or session_open_equity <= 0:
        return {"halted": False, "daily_loss_pct": None,
                "error": "no usable session-open equity — breaker abstains "
                         "(lifetime drawdown halt still active)"}
    loss_pct = max(0.0, -pnl_today) / session_open_equity * 100
    # compare UNROUNDED (a 2.9999% loss must not round-trip into a trip),
    # report rounded
    halted = loss_pct >= max_daily_loss_pct
    loss_pct = round(loss_pct, 3)
    return {
        "halted": halted,
        "daily_loss_pct": loss_pct,
        "limit_pct": max_daily_loss_pct,
        "pnl_today": round(pnl_today, 2),
        "session_open_equity": round(session_open_equity, 2),
        "reason": (f"daily circuit breaker TRIPPED: realized "
                   f"{-pnl_today:,.0f} = {loss_pct}% of session-open equity "
                   f"(limit {max_daily_loss_pct}%) — entries halted until "
                   f"tomorrow" if halted else "within daily loss budget"),
    }


def daily_realized_pnl(conn, today: str = None) -> float:
    """Today's realized P&L straight from the locks this module settled —
    no journal read, no second source of truth. Resets by construction at
    the IST day boundary because released_at stamps are IST."""
    ensure_schema(conn)
    today = today or ist_today()
    row = conn.execute("SELECT COALESCE(SUM(pnl_net), 0) FROM margin_locks "
                       "WHERE released_at LIKE ?", (f"{today}%",)).fetchone()
    return round(float(row[0]), 2)


def daily_breaker_status(conn, today: str = None) -> dict:
    """The LIVE breaker verdict: session-open equity is current equity
    minus what settled today (both from this module's own tables)."""
    pnl_today = daily_realized_pnl(conn, today)
    return check_daily_breaker(equity(conn) - pnl_today, pnl_today)


def _daily_breaker_card(conn, verdict: dict) -> None:
    """One Discord card per IST day when the breaker is the thing
    rejecting entries (owner rule: a halt needing human awareness must
    never be log-only). De-duped via account_events; fail-open — a broken
    card never blocks the gate's verdict."""
    try:
        today = ist_today()
        seen = conn.execute(
            "SELECT 1 FROM account_events WHERE event_type = "
            "'daily_breaker_card' AND ts LIKE ?", (f"{today}%",)).fetchone()
        if seen:
            return
        log_event(conn, "daily_breaker_card", verdict["reason"])
        from src.notifier import fire_broadcast
        fire_broadcast({
            "event": "daily_breaker", "ticker": "ACCOUNT", "date": today,
            "description": (f"🛑 {verdict['reason']}\n"
                            f"Realized today: Rs.{verdict['pnl_today']:+,.2f} "
                            f"on session-open equity "
                            f"Rs.{verdict['session_open_equity']:,.2f}. "
                            "Exits and tracking continue — only NEW entries "
                            "are halted, and the halt clears at the IST day "
                            "boundary."),
        })
    except Exception as e:
        print(f"  (daily breaker card skipped: {e})")


def _ruin_halt_card(conn) -> None:
    """The 🔴 SYSTEM PAUSED card for the risk-of-ruin halt (Walkaway
    Protocol, Directive 3 — docs/walkaway_protocol_design.md). Owner
    rulings 2026-07-27: NO override door (resume = clean-sheet reset,
    an owner decision this layer never automates) and the card RE-FIRES
    once per IST day while the halt is active, so a 3-day walkaway can
    never miss it. De-duped via account_events (`ruin_halt_card` row per
    day — the `_daily_breaker_card` pattern); fail-open — a broken card
    never touches the gate's verdict or a settlement."""
    try:
        today = ist_today()
        seen = conn.execute(
            "SELECT 1 FROM account_events WHERE event_type = "
            "'ruin_halt_card' AND ts LIKE ?", (f"{today}%",)).fetchone()
        if seen:
            return
        dd = drawdown_pct(conn)
        log_event(conn, "ruin_halt_card",
                  f"SYSTEM PAUSED card fired (drawdown {dd:.2f}%)")
        from src.notifier import fire_broadcast
        fire_broadcast({
            "event": "ruin_halt", "ticker": "ACCOUNT", "date": today,
            "description": (
                f"🔴 SYSTEM PAUSED — risk-of-ruin halt\n"
                f"Drawdown is {dd:.2f}% from peak equity (limit "
                f"{MAX_DRAWDOWN_PCT:g}%). ALL new entries are blocked, "
                "on both desks.\n"
                "Open positions are still being managed — exits, stops "
                "and settlements continue as normal.\n"
                "This halt is LATCHED: it does NOT resume on its own, and "
                "adding capital will not lift it (Dept 3 ruling "
                "2026-08-17 — throwing capital at a broken strategy does "
                "not fix the strategy). Clearing it is a deliberate owner "
                "act, on the VM:\n"
                "`python3 -m src.portfolio_manager --reset-halt "
                "--why \"...\" --yes`\n"
                "This card repeats daily while the halt is active."),
        })
    except Exception as e:
        print(f"  (ruin halt card skipped: {e})")


def halt_banner_lines(conn=None) -> list:
    """Fail-safe seam for the daily digests (eod_summary / ceo_brief):
    plain-English halt banner lines, [] when trading normally. Calling
    this while halted ALSO re-fires the daily 🔴 card (self de-duped),
    which is what makes the owner's daily-reminder ruling ride the
    existing Mon-Fri digest crons instead of a new schedule. Never
    raises; a broken read reports [] (the digests stay useful)."""
    owns = conn is None
    try:
        if conn is None:
            conn = brain_map.connect()
        ensure_schema(conn)
        if not trading_halted(conn):
            return []
        _ruin_halt_card(conn)
        return [(f"🔴 SYSTEM PAUSED — risk-of-ruin halt LATCHED (drawdown "
                 f"{drawdown_pct(conn):.2f}% vs the {MAX_DRAWDOWN_PCT:g}% "
                 "limit on peak equity). New entries are blocked on both "
                 "desks; open positions are still managed "
                 "(exits/stops/settlements run). It will not lift by "
                 "itself and capital cannot lift it — resume is a "
                 "deliberate owner act: `python3 -m src.portfolio_manager "
                 "--reset-halt --why \"...\" --yes`.")]
    except Exception as e:
        print(f"  (halt banner unavailable: {e})")
        return []
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# --- the composed entry-halt list (review #2 halt-stack rule) ------------
# EVERY account-level entry halt lives in this one ordered list; a new halt
# is a new entry here, never a new call site. Each check returns
# {halted, event, reason} and may attach extras (the breaker's verdict).

def _risk_of_ruin_check(conn) -> dict:
    halted = trading_halted(conn)
    return {"halted": halted, "event": "risk_of_ruin_halt",
            "reason": (f"risk-of-ruin halt: drawdown {drawdown_pct(conn):.2f}% "
                       f">= {MAX_DRAWDOWN_PCT:g}% — all entries blocked"
                       if halted else "drawdown within limits")}


def _daily_breaker_check(conn) -> dict:
    v = daily_breaker_status(conn)
    return {"halted": v["halted"], "event": "daily_breaker_halt",
            "reason": v["reason"], "verdict": v}


ENTRY_HALT_CHECKS = (_risk_of_ruin_check, _daily_breaker_check)


def request_entry(conn, journal_ref: str, required_margin: float) -> dict:
    """The strict entry guard. Approve = the margin is locked under
    `journal_ref` (idempotent: re-requesting an active ref re-approves
    without double-locking). Reject = nothing is locked and the reason is
    logged to `account_events`.

    Order of the guards matters: the composed halt list first (lifetime
    risk-of-ruin, then the daily circuit breaker — even a tiny trade is
    blocked once a halt is up), then the margin-exhaustion check against
    available liquid cash."""
    ensure_schema(conn)
    get_account(conn)

    active = conn.execute("SELECT 1 FROM margin_locks WHERE journal_ref = ? "
                          "AND released_at IS NULL", (journal_ref,)).fetchone()
    if active:
        return {"approved": True, "reason": "margin already locked for this entry"}

    for check in ENTRY_HALT_CHECKS:
        halt = check(conn)
        if halt["halted"]:
            log_event(conn, halt["event"],
                      f"entry {journal_ref} rejected ({halt['reason']})")
            if halt["event"] == "daily_breaker_halt":
                _daily_breaker_card(conn, halt["verdict"])
            elif halt["event"] == "risk_of_ruin_halt":
                _ruin_halt_card(conn)   # cold-start site: halt already up
            return {"approved": False, "reason": halt["reason"]}

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
        _ruin_halt_card(conn)   # the moment it trips — never silent again
    return dict(snap, released=True, reason="settled",
                halted=trading_halted(conn))


def inject_capital(amount: float, why: str = "", conn=None,
                   now_iso=None) -> dict:
    """Add (or withdraw, if negative) capital to the paper account.

    THE OWNER'S DOOR, added 2026-08-07 when the architect raised the pool
    from the ₹2,00,000 of decision #84 to ₹10,00,000. Before this the only
    way to change the base was hand-SQL against `account_state`, which
    leaves no trace of who changed what or when.

    It moves `starting_capital` and NOTHING else that matters:

      * `realized_pnl` is UNTOUCHED — an injection is not a profit, and
        folding it into P&L would corrupt every performance number
        computed off this row.
      * open `margin_locks` are UNTOUCHED — live positions keep their
        locks and settle normally. Available cash rises because equity
        rose, not because anything was released.
      * `peak_equity` is TRANSLATED by the exact amount moved — not
        ratcheted to the new equity (architect ruling 2026-08-17, after
        the P&L-gap audit). A deposit is not a recovery and a withdrawal
        is not a loss, so the high-water mark must move with the base and
        leave the trading result untouched:

            peak_new = peak_old + amount

        which holds the ABSOLUTE rupee distance `peak - equity` exactly
        constant across the capital event. The old ratchet
        (`max(peak, new_equity)`) set that distance to ZERO on every
        injection, i.e. it reported a fresh high-water mark and a 0.00%
        drawdown to an account that had just lost money — the flaw the
        2026-08-07 ₹8L injection put in the record (equity_curve jumps
        239,423.99 → 1,039,423.99 with dd 1.96% → 0.00%).

        The `max(..., new_equity)` floor survives only as an invariant
        guard: peak must never sit below equity, or the ruin halt could
        never arm.

        HONEST LIMIT, stated because it is a real consequence: rupee
        distance is preserved, so the drawdown PERCENT is diluted by a
        large infusion (₹1,00,000 down on a ₹10L book = 10%; the same
        ₹1,00,000 down after a ₹90L deposit = 1%). That is arithmetic,
        not a bug — but it means a big enough deposit CAN clear an armed
        risk-of-ruin halt. Whether the halt should survive its own
        recapitalisation is a Dept 3 ruling, deliberately not decided
        here.
      * one `capital_injection` row is appended to `account_events` —
        that table is the append-only audit trail, and a capital change
        that isn't in it is a capital change nobody can reconstruct.

    Returns {before, after, injected, why}. Raises on a non-numeric
    amount: this is an owner-invoked operation, not a pipeline seam, and
    silently doing nothing with the money would be the worst outcome."""
    own = conn is None
    if conn is None:
        conn = brain_map.connect()
    try:
        amount = float(amount)
        before = account_summary(conn)
        if amount == 0:
            return {"before": before, "after": before, "injected": 0.0,
                    "why": why}
        conn.execute(
            "UPDATE account_state SET starting_capital = starting_capital + ?, "
            "peak_equity = max(peak_equity + ?, "
            "                  starting_capital + realized_pnl + ?) "
            "WHERE id = 1", (amount, amount, amount))
        conn.commit()
        after = account_summary(conn)
        log_event(conn, "capital_injection",
                  f"Rs.{amount:,.2f} "
                  f"({before['starting_capital']:,.2f} -> "
                  f"{after['starting_capital']:,.2f} base; equity "
                  f"{before['equity']:,.2f} -> {after['equity']:,.2f}; "
                  f"available cash {before['available_cash']:,.2f} -> "
                  f"{after['available_cash']:,.2f})"
                  + (f" — {why}" if why else ""))
        _snapshot_equity(conn)
        return {"before": before, "after": after, "injected": amount,
                "why": why}
    finally:
        if own:
            conn.close()


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


# --- THE DUAL PAPER TREASURY (Phase M1.B, decision #102, 2026-09-19) ------
# The architect's proving ground: "nothing goes near live APIs or real money
# until the system proves profitable on a Rs.2,00,000 PAPER portfolio." A
# 10L pool hides margin constraints, so the SAME signal stream is judged
# a second time against a Rs.2L account with its own cash, its own locks,
# its own P&L and its own halts.
#
# ISOLATION BY TABLE, NOT BY COLUMN. The primary account (ACCOUNT_PAPER_10L)
# stays on the four tables above, untouched -- `account_state` / `margin_locks`
# / `equity_curve` / `account_events` are the live ledger and RULE 3 forbids
# rewriting them. Every other account lives in the `paper_*` tables below,
# keyed by `account_id`, so a 2L lock can never land in the 10L ledger and
# `equity()` / `available_cash()` / `request_entry()` are byte-identical.
# The `paper_*` functions delegate to the primary functions when asked
# about ACCOUNT_PAPER_10L, so a caller can treat both accounts uniformly.
#
# WHO ENTERS. The primary gate decides whether the trade EXISTS (journal
# row, tracker, venue). A shadow account then evaluates the same built
# structure INDEPENDENTLY: lots re-sized to its own equity and liquid cash
# (same formula as strategy.size_lots + the hard rupee cap), capped at the
# primary's lots (a shadow never holds more of a trade than was actually
# taken), then its own halt list and margin-exhaustion check. Approved =
# a `paper_margin_locks` row carrying `lots` and `primary_lots`; refused =
# a named `paper_account_events` row AND the reason stamped on the journal
# entry (`entry["accounts"][account]`). "Approved for 10L, refused for 2L
# on margin" is the finding this layer exists to record.
#
# WHO SETTLES. One settlement path (Rule 7): when `plan_tracker` releases
# the primary lock through `release_entry`, the shadow locks on the same
# journal_ref release in the same call with the P&L SCALED BY LOT RATIO
# (pnl_shadow = pnl_primary x lots_shadow / lots_primary -- spread P&L and
# its frictions are linear in lots). A human rejection releases both at
# zero. No second resolver, no second price read.

ACCOUNT_PAPER_10L = "PAPER_10L"     # the primary: account_state id=1 & co.
ACCOUNT_PAPER_2L = "PAPER_2L"       # the Rs.2L stress-test shadow

# account_id -> starting capital. The primary's pool is whatever the live
# `account_state` row says (STARTING_CAPITAL only seeds an empty DB).
PAPER_ACCOUNTS = {
    ACCOUNT_PAPER_2L: float(PAPER_2L_STARTING_CAPITAL_RS),
}

_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id        TEXT PRIMARY KEY,
    starting_capital  REAL NOT NULL,
    realized_pnl      REAL NOT NULL DEFAULT 0,
    peak_equity       REAL NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_margin_locks (
    account_id    TEXT NOT NULL,
    journal_ref   TEXT NOT NULL,
    margin_rs     REAL NOT NULL,
    lots          INTEGER NOT NULL DEFAULT 1,
    primary_lots  INTEGER NOT NULL DEFAULT 1,   -- the 10L trade's lots at lock time
    locked_at     TEXT NOT NULL,
    released_at   TEXT,
    pnl_net       REAL,
    PRIMARY KEY (account_id, journal_ref)
);
CREATE TABLE IF NOT EXISTS paper_equity_curve (
    account_id    TEXT NOT NULL,
    ts            TEXT NOT NULL,
    equity        REAL NOT NULL,
    peak_equity   REAL NOT NULL,
    drawdown_pct  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_account_events (
    account_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,   -- margin_exhaustion | risk_of_ruin_halt | sizing_refused | venue_fill | ...
    journal_ref TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_locks_ref ON paper_margin_locks (journal_ref);
"""


def shadow_accounts_enabled() -> bool:
    """Kill switch (config `paper_2l_account_enabled`). OFF = no shadow row
    is ever written; the primary path is byte-identical either way."""
    return bool(PAPER_2L_ACCOUNT_ENABLED)


def shadow_account_ids() -> tuple:
    return tuple(PAPER_ACCOUNTS) if shadow_accounts_enabled() else ()


def ensure_accounts_schema(conn) -> None:
    ensure_schema(conn)
    conn.executescript(_ACCOUNTS_SCHEMA)
    conn.commit()


def _is_primary(account: str) -> bool:
    return account in (None, "", ACCOUNT_PAPER_10L)


def get_paper_account(conn, account: str) -> dict:
    """The account row (seeded from PAPER_ACCOUNTS on first touch). The
    primary delegates to get_account(). Unknown ids raise: an account that
    was never declared must never be silently created with guessed money."""
    if _is_primary(account):
        acct = get_account(conn)
        return dict(acct, account_id=ACCOUNT_PAPER_10L)
    if account not in PAPER_ACCOUNTS:
        raise KeyError(f"unknown paper account {account!r}")
    ensure_accounts_schema(conn)
    row = conn.execute("SELECT account_id, starting_capital, realized_pnl, peak_equity, "
                       "created_at FROM paper_accounts WHERE account_id = ?",
                       (account,)).fetchone()
    if row is None:
        cap = float(PAPER_ACCOUNTS[account])
        conn.execute("INSERT INTO paper_accounts (account_id, starting_capital, "
                     "realized_pnl, peak_equity, created_at) VALUES (?, ?, 0, ?, ?)",
                     (account, cap, cap, _now_iso()))
        conn.commit()
        row = conn.execute("SELECT account_id, starting_capital, realized_pnl, "
                           "peak_equity, created_at FROM paper_accounts WHERE "
                           "account_id = ?", (account,)).fetchone()
    keys = ("account_id", "starting_capital", "realized_pnl", "peak_equity", "created_at")
    return dict(zip(keys, tuple(row)))


def paper_equity(conn, account: str) -> float:
    if _is_primary(account):
        return equity(conn)
    a = get_paper_account(conn, account)
    return round(float(a["starting_capital"]) + float(a["realized_pnl"]), 2)


def paper_locked_margin(conn, account: str) -> float:
    if _is_primary(account):
        return locked_margin(conn)
    ensure_accounts_schema(conn)
    row = conn.execute("SELECT COALESCE(SUM(margin_rs), 0) FROM paper_margin_locks "
                       "WHERE account_id = ? AND released_at IS NULL", (account,)).fetchone()
    return round(float(row[0]), 2)


def paper_available_cash(conn, account: str) -> float:
    return round(paper_equity(conn, account) - paper_locked_margin(conn, account), 2)


def paper_drawdown_pct(conn, account: str) -> float:
    if _is_primary(account):
        return drawdown_pct(conn)
    a = get_paper_account(conn, account)
    peak = float(a["peak_equity"])
    if peak <= 0:
        return 0.0
    return round(max(0.0, (peak - paper_equity(conn, account)) / peak * 100), 4)


def paper_log_event(conn, account: str, event_type: str, journal_ref: str = None,
                    detail: str = "") -> None:
    """The shadow ledger's append-only trail. Never the primary's
    `account_events`: a 2L refusal must not read as a 10L halt."""
    ensure_accounts_schema(conn)
    conn.execute("INSERT INTO paper_account_events (account_id, ts, event_type, "
                 "journal_ref, detail) VALUES (?, ?, ?, ?, ?)",
                 (account, _now_iso(), event_type, journal_ref, detail))
    conn.commit()


def paper_halt_latched(conn, account: str) -> bool:
    if _is_primary(account):
        return halt_latched(conn)
    ensure_accounts_schema(conn)
    row = conn.execute(
        "SELECT event_type FROM paper_account_events WHERE account_id = ? AND "
        "event_type IN (?, ?) ORDER BY ts DESC, rowid DESC LIMIT 1",
        (account, HALT_LATCH_EVENT, HALT_CLEAR_EVENT)).fetchone()
    return bool(row) and row[0] == HALT_LATCH_EVENT


def paper_trading_halted(conn, account: str) -> bool:
    """The shadow account's own risk-of-ruin brake: same 10% rule, same
    latch semantics (Dept 3 ruling 2026-08-17), its own events table. No
    Discord card -- a shadow ledger has zero authority and one Discord
    door is not for telemetry."""
    if _is_primary(account):
        return trading_halted(conn)
    if paper_halt_latched(conn, account):
        return True
    dd = paper_drawdown_pct(conn, account)
    if dd >= MAX_DRAWDOWN_PCT:
        paper_log_event(conn, account, HALT_LATCH_EVENT, None,
                        f"drawdown {dd:.2f}% >= {MAX_DRAWDOWN_PCT:g}% of peak equity")
        return True
    return False


def paper_daily_breaker_status(conn, account: str, today: str = None) -> dict:
    if _is_primary(account):
        return daily_breaker_status(conn, today)
    ensure_accounts_schema(conn)
    today = today or ist_today()
    row = conn.execute("SELECT COALESCE(SUM(pnl_net), 0) FROM paper_margin_locks WHERE "
                       "account_id = ? AND released_at LIKE ?",
                       (account, f"{today}%")).fetchone()
    pnl_today = round(float(row[0]), 2)
    return check_daily_breaker(paper_equity(conn, account) - pnl_today, pnl_today)


def size_for_account(conn, account: str, spread: dict, primary_lots: int,
                     risk_pct: float = None) -> dict:
    """Lots the shadow account would take of THIS structure: the
    strategy.size_lots formula on the account's own equity and liquid cash,
    the decision #84 hard rupee cap, then capped at the primary's lots.
    Returns {lots, by_risk, by_margin, by_cap, reason}; lots 0 = refused."""
    risk_pct = OPTIONS_RISK_PER_TRADE_PCT if risk_pct is None else float(risk_pct)
    max_loss = float(spread.get("max_loss") or 0)
    per_lot = float((spread.get("margin") or {}).get("total_margin") or 0)
    if max_loss <= 0:
        return {"lots": 0, "by_risk": 0, "by_margin": 0, "by_cap": 0,
                "reason": "unmeasurable max loss"}
    eq = paper_equity(conn, account)
    cash = paper_available_cash(conn, account)
    by_risk = int((eq * risk_pct / 100) // max_loss)
    by_margin = int(cash // per_lot) if per_lot > 0 else by_risk
    by_cap = int(MAX_RISK_PER_TRADE_RS // max_loss)
    lots = max(0, min(by_risk, by_margin, by_cap, int(primary_lots or 0)))
    if lots <= 0:
        if by_cap <= 0:
            why = (f"max loss Rs.{max_loss:,.0f}/lot exceeds the "
                   f"Rs.{MAX_RISK_PER_TRADE_RS:,.0f} hard per-trade risk cap")
        elif by_risk <= 0:
            why = (f"max loss Rs.{max_loss:,.0f}/lot doesn't fit the {risk_pct:g}% "
                   f"options risk budget of Rs.{eq * risk_pct / 100:,.0f} on "
                   f"Rs.{eq:,.0f} equity")
        elif by_margin <= 0:
            why = (f"SPAN margin Rs.{per_lot:,.0f}/lot exceeds liquid cash "
                   f"Rs.{cash:,.0f}")
        else:
            why = "primary trade carries zero lots"
        return {"lots": 0, "by_risk": by_risk, "by_margin": by_margin,
                "by_cap": by_cap, "reason": why}
    return {"lots": lots, "by_risk": by_risk, "by_margin": by_margin,
            "by_cap": by_cap, "reason": "sized"}


def paper_request_entry(conn, account: str, journal_ref: str, required_margin: float,
                        lots: int = 1, primary_lots: int = 1) -> dict:
    """The shadow account's strict entry guard: same order as request_entry
    (idempotent re-request -> approve; halt list; margin exhaustion), its
    own tables. The primary delegates to request_entry()."""
    if _is_primary(account):
        return request_entry(conn, journal_ref, required_margin)
    get_paper_account(conn, account)
    active = conn.execute("SELECT 1 FROM paper_margin_locks WHERE account_id = ? AND "
                          "journal_ref = ? AND released_at IS NULL",
                          (account, journal_ref)).fetchone()
    if active:
        return {"approved": True, "reason": "margin already locked for this entry"}
    if paper_trading_halted(conn, account):
        reason = (f"risk-of-ruin halt: drawdown {paper_drawdown_pct(conn, account):.2f}% "
                  f">= {MAX_DRAWDOWN_PCT:g}% — all entries blocked")
        paper_log_event(conn, account, "risk_of_ruin_halt", journal_ref,
                        f"entry {journal_ref} rejected ({reason})")
        return {"approved": False, "reason": reason}
    breaker = paper_daily_breaker_status(conn, account)
    if breaker["halted"]:
        paper_log_event(conn, account, "daily_breaker_halt", journal_ref,
                        f"entry {journal_ref} rejected ({breaker['reason']})")
        return {"approved": False, "reason": breaker["reason"]}
    cash = paper_available_cash(conn, account)
    margin = round(float(required_margin), 2)
    if margin > cash:
        reason = (f"margin exhaustion: needs Rs.{margin:,.2f} but only "
                  f"Rs.{cash:,.2f} liquid (Rs.{paper_locked_margin(conn, account):,.2f} "
                  "already locked)")
        paper_log_event(conn, account, "margin_exhaustion", journal_ref,
                        f"entry {journal_ref} rejected ({reason})")
        return {"approved": False, "reason": reason}
    conn.execute("INSERT INTO paper_margin_locks (account_id, journal_ref, margin_rs, "
                 "lots, primary_lots, locked_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (account, journal_ref, margin, int(lots), int(primary_lots), _now_iso()))
    conn.commit()
    return {"approved": True, "reason": "margin locked"}


def _paper_snapshot(conn, account: str) -> dict:
    eq, dd = paper_equity(conn, account), paper_drawdown_pct(conn, account)
    peak = float(get_paper_account(conn, account)["peak_equity"])
    conn.execute("INSERT INTO paper_equity_curve (account_id, ts, equity, peak_equity, "
                 "drawdown_pct) VALUES (?, ?, ?, ?, ?)", (account, _now_iso(), eq, peak, dd))
    conn.commit()
    return {"equity": eq, "peak_equity": peak, "drawdown_pct": dd}


def paper_release_margin(conn, account: str, journal_ref: str, pnl_net: float = 0.0) -> dict:
    """Settle one shadow lock (the primary delegates to release_margin).
    `pnl_net` is THIS account's P&L, already scaled by the caller."""
    if _is_primary(account):
        return release_margin(conn, journal_ref, pnl_net)
    ensure_accounts_schema(conn)
    active = conn.execute("SELECT margin_rs FROM paper_margin_locks WHERE account_id = ? "
                          "AND journal_ref = ? AND released_at IS NULL",
                          (account, journal_ref)).fetchone()
    if active is None:
        return {"released": False, "reason": "no active lock for this ref"}
    was_halted = paper_trading_halted(conn, account)
    pnl = round(float(pnl_net), 2)
    conn.execute("UPDATE paper_margin_locks SET released_at = ?, pnl_net = ? WHERE "
                 "account_id = ? AND journal_ref = ?", (_now_iso(), pnl, account, journal_ref))
    conn.execute("UPDATE paper_accounts SET realized_pnl = round(realized_pnl + ?, 2), "
                 "peak_equity = max(peak_equity, starting_capital + realized_pnl + ?) "
                 "WHERE account_id = ?", (pnl, pnl, account))
    conn.commit()
    snap = _paper_snapshot(conn, account)
    if not was_halted and paper_trading_halted(conn, account):
        paper_log_event(conn, account, "risk_of_ruin_halt", journal_ref,
                        f"drawdown hit {snap['drawdown_pct']:.2f}% after settling "
                        f"{journal_ref} (pnl Rs.{pnl:,.2f}) — execution blocked")
    return dict(snap, released=True, reason="settled", pnl_net=pnl,
                halted=paper_trading_halted(conn, account))


def paper_account_summary(conn, account: str) -> dict:
    """The account_summary() shape for any account, plus `account_id`."""
    if _is_primary(account):
        return dict(account_summary(conn), account_id=ACCOUNT_PAPER_10L)
    a = get_paper_account(conn, account)
    return {
        "account_id": account,
        "starting_capital": a["starting_capital"],
        "realized_pnl": a["realized_pnl"],
        "equity": paper_equity(conn, account),
        "peak_equity": a["peak_equity"],
        "locked_margin": paper_locked_margin(conn, account),
        "available_cash": paper_available_cash(conn, account),
        "drawdown_pct": paper_drawdown_pct(conn, account),
        "trading_halted": paper_trading_halted(conn, account),
        "open_locks": conn.execute("SELECT COUNT(*) FROM paper_margin_locks WHERE "
                                   "account_id = ? AND released_at IS NULL",
                                   (account,)).fetchone()[0],
        "rejections": conn.execute("SELECT COUNT(*) FROM paper_account_events WHERE "
                                   "account_id = ? AND event_type IN ('margin_exhaustion', "
                                   "'sizing_refused', 'risk_of_ruin_halt', "
                                   "'daily_breaker_halt')", (account,)).fetchone()[0],
    }


def evaluate_shadow_accounts(journal_ref: str, proposal: dict, conn=None,
                             risk_pct: float = None) -> dict:
    """Judge one PRIMARY-APPROVED proposal against every shadow account,
    independently: size on the account's own capital, then its own gate.
    Returns {account_id: {status, lots, margin_rs, reason}} -- `status` is
    "approved" | "rejected" | "error"; {} when the switch is off.

    Fail-safe seam (the gate_headless_entry contract): never raises, and
    a broken shadow ledger can never touch the primary decision."""
    out = {}
    if not shadow_accounts_enabled():
        return out
    owns = conn is None
    try:
        if conn is None:
            conn = brain_map.connect()
        spread = proposal.get("spread") or {}
        primary_lots = int(spread.get("lots", proposal.get("lots", 1)) or 0)
        for account in shadow_account_ids():
            try:
                sized = size_for_account(conn, account, spread, primary_lots, risk_pct)
                if sized["lots"] <= 0:
                    paper_log_event(conn, account, "sizing_refused", journal_ref,
                                    f"entry {journal_ref} refused ({sized['reason']})")
                    out[account] = {"status": "rejected", "lots": 0, "margin_rs": None,
                                    "reason": f"sizing refused: {sized['reason']}"}
                    continue
                required = required_margin_for(
                    {"spread": dict(spread, lots=sized["lots"]), "vix": proposal.get("vix")})
                verdict = paper_request_entry(conn, account, journal_ref, required,
                                              lots=sized["lots"], primary_lots=primary_lots)
                out[account] = {"status": "approved" if verdict["approved"] else "rejected",
                                "lots": sized["lots"] if verdict["approved"] else 0,
                                "margin_rs": required if verdict["approved"] else None,
                                "reason": verdict["reason"]}
            except Exception as e:
                out[account] = {"status": "error", "lots": 0, "margin_rs": None,
                                "reason": f"shadow account unavailable ({e})"}
        return out
    except Exception as e:
        print(f"  (shadow accounts unavailable — primary decision unaffected: {e})")
        return out
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def release_shadow_locks(conn, journal_ref: str, primary_pnl_net: float = 0.0) -> dict:
    """Settle every shadow lock on `journal_ref` in the same call that
    settled the primary, P&L scaled by lot ratio. Safe on unknown refs."""
    out = {}
    ensure_accounts_schema(conn)
    rows = conn.execute("SELECT account_id, lots, primary_lots FROM paper_margin_locks "
                        "WHERE journal_ref = ? AND released_at IS NULL",
                        (journal_ref,)).fetchall()
    for account, lots, primary_lots in (tuple(r) for r in rows):
        ratio = (float(lots) / float(primary_lots)) if primary_lots else 0.0
        pnl = round(float(primary_pnl_net) * ratio, 2)
        out[account] = paper_release_margin(conn, account, journal_ref, pnl)
    return out


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
    owns = conn is None
    try:
        if conn is None:
            conn = brain_map.connect()
        result = release_margin(conn, journal_ref, pnl_net)
    except Exception as e:
        print(f"  (margin release skipped: {e})")
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return {"released": False, "reason": str(e)}
    # The release is COMMITTED past this point — nothing that follows may
    # flip the answer back to released=False, or the caller keeps
    # accounting a lock the DB has already let go. The sweep is advisory:
    # its failure is recorded on the result, never propagated.
    # Dual treasury (#102): the shadow accounts settle HERE, off the same
    # tracker call, P&L scaled by lot ratio — the one settlement path.
    # Runs whether or not the primary had a lock (a human rejection
    # releases both at zero); fail-open, recorded on the result.
    try:
        shadow = release_shadow_locks(conn, journal_ref, pnl_net)
        if shadow:
            result["shadow_accounts"] = shadow
    except Exception as e:
        print(f"  (shadow account release skipped: {e})")
        result["shadow_accounts_error"] = str(e)
    if result.get("released") and float(pnl_net) > 0:
        try:
            from src import wealth_lock
            result["wealth_sweep"] = wealth_lock.sweep_on_settlement(
                journal_ref, pnl_net, conn=conn)
        except Exception as e:
            print(f"  (wealth sweep skipped: {e})")
            result["wealth_sweep"] = None
            result["wealth_sweep_error"] = str(e)
    if owns:
        try:
            conn.close()
        except Exception:
            pass
    return result


if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="The paper account")
    ap.add_argument("--inject", type=float, default=None,
                    help="add this many rupees to starting_capital "
                         "(negative withdraws)")
    ap.add_argument("--reset-halt", action="store_true",
                    help="clear a LATCHED risk-of-ruin halt (admin only). "
                         "Requires --why and --yes. Nothing else clears it: "
                         "not a capital injection, not a recovering "
                         "drawdown, not a new day.")
    ap.add_argument("--why", default="",
                    help="audit note for --inject / --reset-halt "
                         "(mandatory for --reset-halt)")
    ap.add_argument("--yes", action="store_true",
                    help="required with --inject / --reset-halt: these move "
                         "real paper capital or lift the brake, and are "
                         "written to the append-only ledger")
    cli = ap.parse_args()

    connection = brain_map.connect()
    if cli.reset_halt:
        if not cli.why.strip():
            print("Refusing to clear the halt without --why.")
            print('e.g. --why "reviewed the 6 losing NIFTY bear puts, '
                  'gamma-exit rule tightened"')
            connection.close()
            sys.exit(1)
        if not cli.yes:
            print("Refusing to clear the halt without --yes.")
            print(f"Latched: {halt_latched(connection)} · drawdown "
                  f"{drawdown_pct(connection):.2f}% (limit "
                  f"{MAX_DRAWDOWN_PCT:g}%). Re-run with --yes.")
            connection.close()
            sys.exit(1)
        outcome = clear_halt(connection, why=cli.why, who="owner (CLI)")
        print(json.dumps(outcome, indent=2))
        connection.close()
        sys.exit(0 if outcome["cleared"] else 1)
    if cli.inject is not None:
        if not cli.yes:
            print("Refusing to move capital without --yes.")
            print(f"Would inject Rs.{cli.inject:,.2f}. Re-run with --yes.")
            connection.close()
            sys.exit(1)
        print(json.dumps(inject_capital(cli.inject, why=cli.why,
                                        conn=connection), indent=2))
    else:
        summary = {ACCOUNT_PAPER_10L: account_summary(connection)}
        for _acct in shadow_account_ids():
            summary[_acct] = paper_account_summary(connection, _acct)
        print(json.dumps(summary, indent=2))
    connection.close()

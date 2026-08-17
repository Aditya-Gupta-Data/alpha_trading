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
        print(json.dumps(account_summary(connection), indent=2))
    connection.close()

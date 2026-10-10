"""The Shadow Learner (Architect directive 2026-10-09): PAPER_SHADOW_LEARNER takes every RAW setup
the proposer builds — the advisory vetoes, the firm-wide exposure slot (#68) and the market loop's
2-hour cooldown do not apply to it — on real crossed quotes through the live arm's own book
(#120 marks, #135 ratchet), inside its own margin wall and cadence. Its fills feed the Proving
Court as `shadow_trades` rows (mode SHADOW_LEARNER, pattern `learner:<strategy>`)."""
import hashlib
import time
from datetime import date, datetime

ACCOUNT = "PAPER_SHADOW_LEARNER"
MODE = "SHADOW_LEARNER"
PORTFOLIO_ID = f"{ACCOUNT}/IDX_SPREADS"     # M1 (#142): the learner's one bucket
_LAST_TAKE: dict = {}          # underlying -> epoch of the learner's last entry (process memory)


def enabled() -> bool:
    from src import portfolio_manager as pm
    from src.config import SHADOW_LEARNER_ENABLED
    return bool(SHADOW_LEARNER_ENABLED) and pm.shadow_accounts_enabled()


def learner_ref(journal_ref: str) -> str:
    return "learner:" + hashlib.sha1(str(journal_ref).encode()).hexdigest()[:14]


def open_count(conn) -> int:
    from src.execution.live_pricer import STATE_CLOSED, ensure_schema
    ensure_schema(conn)
    return int(conn.execute("SELECT COUNT(*) FROM paper_live_positions WHERE account_id = ? AND state != ?",
                            (ACCOUNT, STATE_CLOSED)).fetchone()[0])


def ready(underlying: str, now: float = None, conn=None) -> tuple:
    """(ok, reason): the learner's OWN cadence and open-position cap — never the loop's cooldown."""
    from src.config import SHADOW_LEARNER_COOLDOWN_SECONDS, SHADOW_LEARNER_MAX_OPEN
    now = time.time() if now is None else float(now)
    last = _LAST_TAKE.get(underlying)
    if last is not None and now - last < SHADOW_LEARNER_COOLDOWN_SECONDS:
        return False, f"learner cadence: {int(SHADOW_LEARNER_COOLDOWN_SECONDS - (now - last))}s left on {underlying}"
    try:
        own = conn is None
        if own:
            from src import brain_map
            conn = brain_map.connect()
        try:
            n = open_count(conn)
        finally:
            if own:
                conn.close()
    except Exception as exc:
        return False, f"learner book unreadable ({exc})"
    if n >= SHADOW_LEARNER_MAX_OPEN:
        return False, f"learner cap: {n} open positions (max {SHADOW_LEARNER_MAX_OPEN})"
    return True, "ready"


def arm(underlying: str, now: float = None) -> None:
    _LAST_TAKE[underlying] = time.time() if now is None else float(now)


def record_fire(conn, entry: dict, fire_date: str = None) -> dict:
    """One Court row per learner entry; resolved from the live settle with the REAL r."""
    from src.validation import trial
    trial.ensure_schema(conn)
    spread = entry.get("spread") or {}
    ref = learner_ref(entry.get("short_id"))
    cur = conn.execute(
        "INSERT INTO shadow_trades (journal_ref, pattern_id, fire_date, ticker, direction, created_at, host_ref, mode, "
        "portfolio_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (journal_ref) DO NOTHING",
        (ref, f"learner:{spread.get('strategy')}", fire_date or date.today().isoformat(), entry.get("ticker"),
         spread.get("direction") or entry.get("view"), datetime.utcnow().isoformat(timespec="seconds"),
         entry.get("short_id"), MODE, PORTFOLIO_ID))
    conn.commit()
    return {"ref": ref, "created": bool(cur.rowcount)}


def resolve_from_settle(conn, row: dict, pnl_net: float, closed_on: str) -> bool:
    """Called by the live arm's settle for this account: the Court row gets the real r."""
    from src.validation import trial
    try:
        qty = int(row["lots"]) * int(row["lot_size"])
        risk = float(row["max_loss_ps"]) * qty
        r = round(float(pnl_net) / risk, 4) if risk > 0 else 0.0
        result = "win" if pnl_net > 0 else ("loss" if pnl_net < 0 else "scratch")
        return trial.resolve_shadow(conn, learner_ref(row["journal_ref"]), result, r, str(closed_on)[:10])
    except Exception as exc:
        print(f"  (shadow learner: court row not resolved for {row.get('journal_ref')}: {exc})")
        return False


def evidence(conn, strategy: str) -> dict:
    """{n, wins, r_sum} of the learner's resolved fills for one archetype — the Court's read."""
    from src.validation import trial
    trial.ensure_schema(conn)
    rows = conn.execute("SELECT result, r_multiple FROM shadow_trades WHERE pattern_id = ? AND mode = ? "
                        "AND resolved = 1", (f"learner:{strategy}", MODE)).fetchall()
    return {"n": len(rows), "wins": sum(1 for r in rows if r[0] == "win"),
            "r_sum": round(sum(float(r[1] or 0) for r in rows), 3)}


def take(entry: dict, reason: str, conn=None, venue_mod=None) -> dict:
    """Open the learner's position on crossed quotes: sizing on its own equity, the margin wall,
    the live re-quote + desk entry rules (F12), one venue ticket, the live book. Returns the verdict
    (also stamped on entry['accounts'][ACCOUNT]). Never raises."""
    from src import oms, options_proposer as op, portfolio_manager as pm, strategy_router
    from src.config import PAPER_VENUE_ENABLED
    verdict = {"status": "rejected", "lots": 0, "margin_rs": None, "reason": "", "learner_only": reason}
    entry.setdefault("accounts", {})[ACCOUNT] = verdict
    if not PAPER_VENUE_ENABLED:
        verdict["reason"] = "paper venue off — the learner opens only on a real crossed fill"
        return verdict
    own = conn is None
    try:
        if own:
            from src import brain_map
            conn = brain_map.connect()
        spread = entry["spread"]
        ref = entry.get("short_id")
        sized = pm.size_for_account(conn, ACCOUNT, spread, risk_pct=None, vix=entry.get("vix"))
        if int(sized.get("lots") or 0) <= 0:
            verdict["reason"] = f"sizing refused: {sized.get('reason')}"
            return verdict
        lots = int(sized["lots"])
        required = pm.required_margin_for({"spread": dict(spread, lots=lots), "vix": entry.get("vix")})
        gate = pm.paper_request_entry(conn, ACCOUNT, ref, required, lots=lots, primary_lots=int(entry.get("lots") or 1))
        if not gate.get("approved"):
            verdict["reason"] = gate.get("reason") or "margin refused"
            return verdict
        verdict.update(status="approved", lots=lots, margin_rs=required, reason="shadow learner: raw setup",
                       sizing={k: sized.get(k) for k in ("by_risk", "by_margin", "floor_applied", "risk_pct", "equity")})
        rq = op._live_requote(conn, ACCOUNT, entry)
        if rq is None:
            return verdict
        lots_now = op._live_entry_rules(conn, ACCOUNT, entry, rq, lots)
        if lots_now is None:
            return verdict
        venue = venue_mod
        if venue is None:
            from src.execution import paper_venue as venue
        prop = {"ticker": entry["ticker"], "short_id": ref, "signal": entry.get("signal"),
                "spread": dict(spread, legs=rq["legs"])}
        issued = strategy_router.issue(conn, prop, journal_ref=ref, source="shadow_learner",
                                       account_id=ACCOUNT, lots=lots_now)
        stid = issued["ticket_id"]
        venue.sweep(conn, stamp=False)
        sview = oms.ticket_view(conn, stid) or {}
        verdict.update(ticket_id=stid, venue_status=sview.get("status"))
        op._live_after_fill(conn, ACCOUNT, entry, stid, sview, rq["quote_ts"])
        if verdict.get("status") == "approved" and sview.get("status") == oms.FILLED:
            record_fire(conn, entry)
            arm(entry.get("ticker"))
        return verdict
    except Exception as exc:
        verdict["reason"] = f"learner entry failed ({type(exc).__name__}: {exc})"
        return verdict
    finally:
        if own and conn is not None and not op._PAPER_VENUE_KEEP_CONN:
            conn.close()

"""M1 bucket routing seams (decision #142): per-portfolio margin gate and settlement, behind config
`multi_bucket_ledger`. Blueprint docs/m1_multi_bucket_ledger_blueprint.md. Paper money only.

Contract: `gate` returns None when the switch is off or the bucket row does not exist — the caller then
runs the flat ledger exactly as before. Every write here is a plain statement with NO commit, so it joins
the caller's transaction (live_pricer._settle's one-commit rule, D4)."""
from datetime import datetime

from src import config

PRIMARY_ACCOUNT = "PAPER_10L"
IDX_FAMILY = "IDX_SPREADS"
DARLINGS_FAMILY = "DARLINGS"
DARLINGS_LOCK_PREFIX = "eqd:"
HELD_LOCK_REASON = "margin already locked for this entry (bucket)"
EVENT_LOCKED, EVENT_REFUSED, EVENT_SETTLED, EVENT_HALT = "lock", "entry_refused", "settled", "risk_of_ruin_halt"


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def enabled() -> bool:
    return bool(getattr(config, "MULTI_BUCKET_LEDGER", False))


def portfolio_for(account: str, journal_ref: str, portfolio_id: str = None) -> str:
    """Explicit id wins; `eqd:` locks are the DARLINGS bucket; everything else is the account's IDX bucket."""
    if portfolio_id:
        return str(portfolio_id)
    account = account or PRIMARY_ACCOUNT
    if str(journal_ref or "").startswith(DARLINGS_LOCK_PREFIX):
        return f"{PRIMARY_ACCOUNT}/{DARLINGS_FAMILY}"
    return f"{account}/{IDX_FAMILY}"


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def bucket(conn, portfolio_id: str):
    """The portfolio row as a dict, or None (no schema / no row / retired)."""
    if "portfolios" not in _tables(conn):
        return None
    row = conn.execute(
        "SELECT portfolio_id, account_id, strategy_family, starting_capital, realized_pnl, peak_equity, "
        "risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, halted_at, halt_reason, retired_at "
        "FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)).fetchone()
    if row is None:
        return None
    keys = ("portfolio_id", "account_id", "strategy_family", "starting_capital", "realized_pnl", "peak_equity",
            "risk_per_trade_pct", "max_drawdown_pct", "daily_loss_pct", "halted_at", "halt_reason", "retired_at")
    b = dict(zip(keys, tuple(row)))
    return None if b["retired_at"] else b


def equity(b: dict) -> float:
    return round(float(b["starting_capital"]) + float(b["realized_pnl"]), 2)


def open_locks(conn, portfolio_id: str) -> float:
    row = conn.execute("SELECT COALESCE(SUM(margin_rs), 0) FROM portfolio_margin_locks WHERE portfolio_id = ? "
                       "AND released_at IS NULL", (portfolio_id,)).fetchone()
    return round(float(row[0]), 2)


def available_cash(conn, b: dict) -> float:
    return round(equity(b) - open_locks(conn, b["portfolio_id"]), 2)


def drawdown_pct(b: dict) -> float:
    peak = float(b["peak_equity"])
    if peak <= 0:
        return 0.0
    return round(max(0.0, (peak - equity(b)) / peak * 100), 4)


def _event(conn, portfolio_id: str, event_type: str, journal_ref: str, detail: str) -> None:
    conn.execute("INSERT INTO portfolio_events (portfolio_id, ts, event_type, journal_ref, detail) "
                 "VALUES (?, ?, ?, ?, ?)", (portfolio_id, _now_iso(), event_type, journal_ref, detail))


def _halt(conn, b: dict, journal_ref: str = None) -> dict | None:
    """The bucket's own halt right now: the latch, else the ruin check (latched here when breached)."""
    if b.get("halted_at"):
        return {"halted": True, "event": EVENT_HALT,
                "reason": f"bucket {b['portfolio_id']} halted since {b['halted_at']}: {b.get('halt_reason') or ''}"}
    mdd = b.get("max_drawdown_pct")
    if mdd is None:                                   # the Shadow Learner: no latch, by data not by branch
        return None
    dd = drawdown_pct(b)
    if dd >= float(mdd):
        reason = f"risk-of-ruin halt: bucket drawdown {dd:.2f}% >= {float(mdd):g}% of peak equity"
        conn.execute("UPDATE portfolios SET halted_at = ?, halt_reason = ? WHERE portfolio_id = ? "
                     "AND halted_at IS NULL", (_now_iso(), reason, b["portfolio_id"]))
        _event(conn, b["portfolio_id"], EVENT_HALT, journal_ref, reason)
        b["halted_at"] = _now_iso()
        return {"halted": True, "event": EVENT_HALT, "reason": reason}
    return None


def gate(conn, account: str, journal_ref: str, margin: float, lots: int = 1, dry_run: bool = False,
         portfolio_id: str = None):
    """The bucket's entry guard. None = not routed (switch off / no bucket row): the flat ledger decides.
    Else {approved, reason, portfolio_id[, halt]}; an approval writes the bucket lock (not on dry_run)."""
    if not enabled():
        return None
    pid = portfolio_for(account, journal_ref, portfolio_id)
    b = bucket(conn, pid)
    if b is None:
        return None
    verdict = {"portfolio_id": pid}
    held = conn.execute("SELECT 1 FROM portfolio_margin_locks WHERE portfolio_id = ? AND journal_ref = ? "
                        "AND released_at IS NULL", (pid, journal_ref)).fetchone()
    halt = _halt(conn, b, journal_ref)
    if halt:
        if not held:
            _event(conn, pid, EVENT_REFUSED, journal_ref, f"entry rejected ({halt['reason']})")
        return dict(verdict, approved=False, reason=halt["reason"], halt=halt)
    if held:
        return dict(verdict, approved=True, reason=HELD_LOCK_REASON)
    margin = round(float(margin), 2)
    cash = available_cash(conn, b)
    if b.get("max_drawdown_pct") is not None and margin > cash:      # NULL latch = no margin wall either (#140)
        reason = (f"margin exhaustion in bucket {pid}: needs Rs.{margin:,.2f} but only Rs.{cash:,.2f} liquid "
                  f"(Rs.{open_locks(conn, pid):,.2f} already locked)")
        _event(conn, pid, "margin_exhaustion", journal_ref, f"entry rejected ({reason})")
        return dict(verdict, approved=False, reason=reason)
    if dry_run:
        return dict(verdict, approved=True, reason="bucket margin available (dry run — nothing locked)")
    conn.execute("INSERT INTO portfolio_margin_locks (portfolio_id, journal_ref, margin_rs, lots, locked_at, "
                 "released_at, pnl_net) VALUES (?, ?, ?, ?, ?, NULL, NULL) "
                 "ON CONFLICT (portfolio_id, journal_ref) DO UPDATE SET margin_rs = excluded.margin_rs, "
                 "lots = excluded.lots, locked_at = excluded.locked_at, released_at = NULL, pnl_net = NULL",
                 (pid, journal_ref, margin, int(lots or 1), _now_iso()))
    _event(conn, pid, EVENT_LOCKED, journal_ref, f"Rs.{margin:,.2f} locked ({int(lots or 1)} lot(s))")
    return dict(verdict, approved=True, reason="bucket margin locked")


def resize_lock(conn, account: str, journal_ref: str, lots: int, margin_rs: float) -> bool:
    """Shrink the bucket lock with the flat one (audit F12). No commit."""
    if "portfolio_margin_locks" not in _tables(conn):
        return False
    cur = conn.execute("UPDATE portfolio_margin_locks SET lots = ?, margin_rs = ? WHERE journal_ref = ? "
                       "AND released_at IS NULL AND lots > ? AND portfolio_id IN "
                       "(SELECT portfolio_id FROM portfolios WHERE account_id = ?)",
                       (int(lots), round(float(margin_rs), 2), journal_ref, int(lots), account or PRIMARY_ACCOUNT))
    return cur.rowcount == 1


def release(conn, account: str, ref: str, pnl: float, portfolio_id: str = None):
    """Settle the bucket lock on `ref`: close it, book the P&L and peak, one curve point, the ruin latch.
    None when no active bucket lock exists (a lock taken is always settleable, switch on or off). No commit."""
    if "portfolio_margin_locks" not in _tables(conn):
        return None
    account = account or PRIMARY_ACCOUNT
    if portfolio_id:
        row = conn.execute("SELECT portfolio_id FROM portfolio_margin_locks WHERE portfolio_id = ? AND "
                           "journal_ref = ? AND released_at IS NULL", (portfolio_id, ref)).fetchone()
    else:
        row = conn.execute("SELECT l.portfolio_id FROM portfolio_margin_locks l JOIN portfolios p "
                           "ON p.portfolio_id = l.portfolio_id WHERE l.journal_ref = ? AND p.account_id = ? "
                           "AND l.released_at IS NULL", (ref, account)).fetchone()
    if row is None:
        return None
    pid = row[0]
    pnl = round(float(pnl), 2)
    now = _now_iso()
    conn.execute("UPDATE portfolio_margin_locks SET released_at = ?, pnl_net = ? WHERE portfolio_id = ? "
                 "AND journal_ref = ? AND released_at IS NULL", (now, pnl, pid, ref))
    conn.execute("UPDATE portfolios SET realized_pnl = round(realized_pnl + ?, 2), "
                 "peak_equity = max(peak_equity, starting_capital + realized_pnl + ?) WHERE portfolio_id = ?",
                 (pnl, pnl, pid))
    b = bucket(conn, pid)
    was_halted = bool(b.get("halted_at"))
    dd = drawdown_pct(b)
    conn.execute("INSERT INTO portfolio_equity_curve (portfolio_id, ts, equity, peak_equity, drawdown_pct) "
                 "VALUES (?, ?, ?, ?, ?)", (pid, now, equity(b), float(b["peak_equity"]), dd))
    _event(conn, pid, EVENT_SETTLED, ref, f"pnl Rs.{pnl:,.2f}; equity Rs.{equity(b):,.2f}; drawdown {dd:.2f}%")
    halt = _halt(conn, b, ref)
    return {"released": True, "portfolio_id": pid, "pnl_net": pnl, "equity": equity(b),
            "peak_equity": float(b["peak_equity"]), "drawdown_pct": dd,
            "halted": bool(halt), "newly_halted": bool(halt) and not was_halted}


def summary(conn, account: str = None) -> list:
    """Every live bucket (optionally one account's) with equity, open locks and halt state — a report."""
    if "portfolios" not in _tables(conn):
        return []
    sql = "SELECT portfolio_id FROM portfolios WHERE retired_at IS NULL"
    args = ()
    if account:
        sql, args = sql + " AND account_id = ?", (account,)
    out = []
    for (pid,) in conn.execute(sql + " ORDER BY portfolio_id", args).fetchall():
        b = bucket(conn, pid)
        out.append(dict(b, equity=equity(b), open_locks=open_locks(conn, pid), available_cash=available_cash(conn, b),
                        drawdown_pct=drawdown_pct(b)))
    return out

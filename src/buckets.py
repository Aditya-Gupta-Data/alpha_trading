"""M1 bucket routing seams (decision #142): per-portfolio margin gate and settlement, behind config
`multi_bucket_ledger`. Blueprint docs/m1_multi_bucket_ledger_blueprint.md. Paper money only.

Contract: `gate` returns None when the switch is off or the bucket row does not exist — the caller then
runs the flat ledger exactly as before. Every write here is a plain statement with NO commit, so it joins
the caller's transaction (live_pricer._settle's one-commit rule, D4)."""
from datetime import datetime

from src import config

PRIMARY_ACCOUNT = "PAPER_10L"
IDX_FAMILY = "IDX_SPREADS"
EQ_SWING_FAMILY = "EQ_SWING"
DARLINGS_FAMILY = "DARLINGS"
WEALTH_LOCK_FAMILY = "WEALTH_LOCK"
CROSS_ASSET_FAMILY = "CROSS_ASSET"
DARLINGS_LOCK_PREFIX = "eqd:"
HELD_LOCK_REASON = "margin already locked for this entry (bucket)"
EVENT_LOCKED, EVENT_REFUSED, EVENT_SETTLED, EVENT_HALT = "lock", "entry_refused", "settled", "risk_of_ruin_halt"

# M4 5-Bucket Ceiling & Precedence Contract (docs/ARCHITECTURE_ROADMAP.md §5):
# Sum of ceilings = 25 + 20 + 30 + 25 + 15 = 115%. These are per-bucket CEILINGS,
# not target weights; total allocation across all buckets is hard-capped at <= 100%
# of firm equity in deterministic BUCKET_PRECEDENCE order, and a halted bucket's
# capital is quarantined so no other bucket can ever borrow it.
BUCKET_CEILINGS_PCT = {
    IDX_FAMILY: 25.0,          # B1: Index options spreads <= 25%
    EQ_SWING_FAMILY: 20.0,     # B2: Equity swing <= 20%
    DARLINGS_FAMILY: 30.0,     # B3: Darlings delivery <= 30%
    WEALTH_LOCK_FAMILY: 25.0,  # B4: Long-term + GOLDBEES wealth lock <= 25%
    CROSS_ASSET_FAMILY: 15.0,  # B5: Cross-asset / macro <= 15%
}
BUCKET_PRECEDENCE = (
    WEALTH_LOCK_FAMILY,
    DARLINGS_FAMILY,
    IDX_FAMILY,
    EQ_SWING_FAMILY,
    CROSS_ASSET_FAMILY,
)
STRATEGY_FAMILIES = {
    IDX_FAMILY: "index_options",
    EQ_SWING_FAMILY: "equity_swing",
    DARLINGS_FAMILY: "darlings",
    WEALTH_LOCK_FAMILY: "wealth_lock",
    CROSS_ASSET_FAMILY: "cross_asset",
}


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


def ceiling_state(conn, portfolio_id: str):
    """Deployed margin vs the bucket's share of PAPER_10L equity. None = no ceiling applies (shadow account,
    unknown family, no bucket row). `close_only` = a legacy overweight bucket: exits settle, entries wait."""
    b = bucket(conn, portfolio_id)
    if b is None or b["account_id"] != PRIMARY_ACCOUNT:
        return None
    fam = str(portfolio_id).split("/", 1)[-1]
    pct = BUCKET_CEILINGS_PCT.get(fam)
    if pct is None:
        return None
    firm = round(sum(equity(r) for r in summary(conn, PRIMARY_ACCOUNT)), 2)
    ceiling_rs = round(max(0.0, firm) * pct / 100.0, 2)
    deployed = open_locks(conn, portfolio_id)
    return {"portfolio_id": portfolio_id, "ceiling_pct": pct, "ceiling_rs": ceiling_rs, "firm_equity_rs": firm,
            "deployed_rs": deployed, "deployed_pct": round(deployed / firm * 100.0, 4) if firm > 0 else 0.0,
            "close_only": deployed > ceiling_rs}


def can_allocate_new_trade(conn, portfolio_id: str, margin_rs: float = 0.0) -> bool:
    """False while the bucket's deployed margin plus this entry would exceed its ceiling; never forces an exit."""
    st = ceiling_state(conn, portfolio_id)
    if st is None:
        return True
    return round(st["deployed_rs"] + max(0.0, float(margin_rs or 0.0)), 2) <= st["ceiling_rs"]


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


def latch_bucket_halt(conn, portfolio_id: str, why: str = "", commit: bool = True) -> bool:
    """Arm a bucket's halt latch (idempotent; True if this call armed it).
    Buckets with `max_drawdown_pct IS NULL` (e.g. Shadow Learner, #140) have no latch."""
    b = bucket(conn, portfolio_id)
    if b is None or b.get("max_drawdown_pct") is None or b.get("halted_at"):
        return False
    now = _now_iso()
    reason = str(why or "remote kill-switch halt latched").strip()
    conn.execute("UPDATE portfolios SET halted_at = ?, halt_reason = ? WHERE portfolio_id = ? "
                 "AND halted_at IS NULL", (now, reason, portfolio_id))
    _event(conn, portfolio_id, "ruin_halt_latched", None, reason)
    if commit:
        conn.commit()
    return True


def clear_bucket_halt(conn, portfolio_id: str, why: str = "", who: str = "owner", commit: bool = True) -> dict:
    """Audited door out of a latched bucket halt (mirrors `pm.clear_halt` / `equity_desk.clear_desk_halt`)."""
    b = bucket(conn, portfolio_id)
    if b is None:
        return {"cleared": False, "portfolio_id": portfolio_id,
                "reason": f"refused: unknown or retired bucket {portfolio_id!r}"}
    if not why or not str(why).strip():
        return {"cleared": False, "portfolio_id": portfolio_id,
                "reason": "refused: clearing a bucket halt requires a stated why"}
    if not b.get("halted_at"):
        return {"cleared": False, "portfolio_id": portfolio_id,
                "reason": "no latched bucket halt to clear", "halted": False}
    dd = drawdown_pct(b)
    mdd = b.get("max_drawdown_pct")
    conn.execute("UPDATE portfolios SET halted_at = NULL, halt_reason = NULL WHERE portfolio_id = ?",
                 (portfolio_id,))
    _event(conn, portfolio_id, "ruin_halt_cleared", None,
           f"latched bucket halt cleared by {who} at drawdown {dd:.2f}% "
           + (f"(limit {float(mdd):g}%) " if mdd is not None else "")
           + f"— {why}")
    b_after = bucket(conn, portfolio_id)
    re_halt = _halt(conn, b_after)
    if commit:
        conn.commit()
    still = bool(re_halt)
    return {
        "cleared": True,
        "portfolio_id": portfolio_id,
        "drawdown_pct": dd,
        "why": why,
        "who": who,
        "halted": still,
        "reason": ("cleared, but drawdown is still past the limit — the latch re-armed immediately"
                   if still else "cleared; bucket entries are live again"),
    }


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
    if not can_allocate_new_trade(conn, pid, margin):
        st = ceiling_state(conn, pid)
        state = "close-only" if st["close_only"] else "ceiling"
        reason = (f"{state}: bucket {pid} has Rs.{st['deployed_rs']:,.2f} deployed ({st['deployed_pct']:.2f}% of "
                  f"Rs.{st['firm_equity_rs']:,.2f}); +Rs.{margin:,.2f} would pass its {st['ceiling_pct']:g}% "
                  f"ceiling Rs.{st['ceiling_rs']:,.2f} — exits still settle")
        _event(conn, pid, "ceiling_refused", journal_ref, f"entry rejected ({reason})")
        return dict(verdict, approved=False, reason=reason, close_only=st["close_only"])
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


def allocate_bucket_ceilings(firm_equity_rs: float,
                             requested_by_family: dict = None,
                             halted_families=None,
                             locked_in_halted_by_family: dict = None) -> dict:
    """M4 5-bucket ceiling and precedence allocator (docs/ARCHITECTURE_ROADMAP.md §5).

    Enforces two invariants simultaneously:
      1. CEILING + 100% CAP: Each bucket B1..B5 is bounded by its individual percentage
         ceiling (25% / 20% / 30% / 25% / 15%; sum = 115%), AND total allocation across
         all buckets is hard-capped at <= 100% of `firm_equity_rs` in deterministic
         `BUCKET_PRECEDENCE` order.
      2. HALTED CAPITAL QUARANTINE (NO BORROWING): Any bucket in `halted_families`
         receives 0 new allocation AND its ring-fenced capital (either explicit
         `locked_in_halted_by_family[fam]` or its ceiling reservation) is quarantined
         out of the distributable pool so no active bucket can ever borrow a halted
         bucket's capital.
    """
    firm = round(max(0.0, float(firm_equity_rs or 0.0)), 2)
    halted = {str(f).strip() for f in (halted_families or ()) if str(f).strip()}
    ceilings_rs = {
        fam: round(firm * (pct / 100.0), 2)
        for fam, pct in BUCKET_CEILINGS_PCT.items()
    }
    locked_halted = locked_in_halted_by_family or {}
    quarantined_rs = 0.0
    for fam in BUCKET_PRECEDENCE:
        if fam in halted:
            q = locked_halted.get(fam, ceilings_rs[fam])
            quarantined_rs = round(quarantined_rs + max(0.0, float(q)), 2)
    quarantined_rs = min(firm, quarantined_rs)
    distributable_rs = round(max(0.0, firm - quarantined_rs), 2)

    remaining = distributable_rs
    allocations = {}
    clipped = []
    for fam in BUCKET_PRECEDENCE:
        ceiling = ceilings_rs[fam]
        if fam in halted:
            allocations[fam] = 0.0
            continue
        if requested_by_family is None or fam not in requested_by_family:
            req = ceiling
        else:
            req = max(0.0, float(requested_by_family[fam] or 0.0))
        desired = min(req, ceiling)
        granted = round(min(desired, remaining), 2)
        if granted < desired:
            clipped.append(fam)
        allocations[fam] = granted
        remaining = round(max(0.0, remaining - granted), 2)

    total_allocated = round(sum(allocations.values()), 2)
    return {
        "firm_equity_rs": firm,
        "quarantined_halted_rs": quarantined_rs,
        "distributable_rs": distributable_rs,
        "total_allocated_rs": total_allocated,
        "total_allocated_pct": round((total_allocated / firm) * 100.0, 4) if firm > 0 else 0.0,
        "precedence": list(BUCKET_PRECEDENCE),
        "ceilings_rs": ceilings_rs,
        "allocations": allocations,
        "clipped_by_total_cap": clipped,
        "halted_families": sorted(halted),
    }


def can_borrow_from_bucket(conn, borrower_portfolio_id: str,
                           lender_portfolio_id: str, amount_rs: float = 0.0) -> tuple[bool, str]:
    """Ring-fence firewall: proves no bucket can ever borrow another bucket's halted or active capital."""
    if borrower_portfolio_id == lender_portfolio_id:
        return False, "same_bucket_not_a_transfer"
    lender = bucket(conn, lender_portfolio_id) if conn is not None else None
    if lender is not None and lender.get("halted_at"):
        return (
            False,
            f"halted_bucket_quarantine: {lender_portfolio_id} is halted since {lender['halted_at']} "
            f"— its capital (Rs.{equity(lender):,.2f}) is ring-fenced and cannot be borrowed by {borrower_portfolio_id}",
        )
    return (
        False,
        f"ring_fence_violation: cross-bucket borrowing from {lender_portfolio_id} to "
        f"{borrower_portfolio_id} (Rs.{float(amount_rs or 0.0):,.2f}) is forbidden by M1/M4 isolation",
    )

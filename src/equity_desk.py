"""
src/equity_desk.py — Dept 3: the equity desk's paper-capital ledger
===================================================================

Owner ruling 2026-07-20 ("10,00,000 of paper money only — let's see how
efficiently our system runs the 10 lakhs"): the darling shadow book stops
being zero-capital and starts BUYING with a slice of the firm's simulated
pool. This supersedes decision #77's zero-capital clause for the darling
leg ONLY (the block-VWAP leg stays pure telemetry). The Dept-5-first
authority rule was explicitly waived by the owner for this wiring; the
desk's own ledger is the evidence a later Dept-5 review will judge.

THE FIRM'S CAPITAL MATH (one honest 10L, two desks, two machines):
  * The options desk (VM, brain_map.db) keeps its Phase 6G account minus
    a standing reservation lock `equity_desk_allocation` equal to this
    desk's slice — placed ONCE, ON THE VM, via
    `python3 -m src.equity_desk --reserve-firm-slice`.
  * The equity desk (Mac, data/equity_desk.db) runs THIS ledger with
    starting capital = the same slice (config `equity_desk_capital_rs`,
    default Rs.3,00,000). Firm total stays 10L; nothing counts twice.

MACHINERY: Dept 3's `portfolio_manager` is deliberately conn-generic, so
the desk reuses it wholesale against its own sqlite file — the same
margin_locks / equity_curve / account_events tables, the same 10%
trailing-drawdown risk-of-ruin halt, the same daily 3% circuit breaker,
the same silent margin-exhaustion doctrine. No risk rule is
re-implemented here (single-door principle, decision #40 family).

SIZING (config-tunable): `equity_desk_risk_per_trade_pct` (default 1%) of
desk equity risked against the entry-minus-stop distance, capped at
`equity_desk_max_notional_pct` (default 15%) of desk equity per name; the
notional is locked as delivery cash. Whole shares only.

CONTRACTS:
  * Funding fails CLOSED (an unreachable desk funds nothing) while the
    telemetry entry is ALWAYS logged by the caller — "log the false
    positives" survives the capital era.
  * `equity_shadow_proposer` still imports NOTHING from Dept 3 — the
    composition root (patience_basket.eod_chain) injects fund/settle.
  * P&L settles NET of the 2026 delivery friction stack (STT both sides,
    buy-side stamp duty, exchange + SEBI + GST, flat DP debit on sell).

CLI:
    python3 -m src.equity_desk                       # desk summary
    python3 -m src.equity_desk --sweep               # reconcile orphan locks
    python3 -m src.equity_desk --reserve-firm-slice  # VM ONLY: carve the slice
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import portfolio_manager as pm
from src.config import (EQUITY_DESK_CAPITAL_RS, EQUITY_DESK_ENABLED,
                        EQUITY_DESK_MAX_NOTIONAL_PCT,
                        EQUITY_DESK_RISK_PER_TRADE_PCT)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "equity_desk.db"

# 2026 delivery friction stack (April-2026 rates). The options rates live
# in src/portfolio.py — delivery differs on every line: STT hits BOTH
# sides at 0.1%, stamp duty is 5x the options rate, discount brokerage is
# zero, and the depository debits a flat charge per sell.
STT_RATE_DELIVERY = 0.001          # 0.1% of turnover, buy AND sell
STAMP_DUTY_RATE_BUY = 0.00015      # 0.015% buy side
EXCHANGE_CHARGE_RATE = 0.0000297   # NSE equity transaction charge
SEBI_FEE_RATE = 0.000001           # 0.0001% turnover fee
GST_RATE = 0.18                    # on service charges only
DP_CHARGE_SELL = 16.0              # flat depository debit per sell

FIRM_ALLOCATION_REF = "equity_desk_allocation"
LOCK_PREFIX = "eqd:"


def connect(db_path=None):
    """Open the desk's own DB with the account bootstrapped at the
    CONFIGURED slice. pm.get_account would default a fresh row to the
    options desk's 10L, so the row is inserted here first; an existing
    row is never rewritten (curve continuity beats a config edit)."""
    p = Path(db_path) if db_path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    pm.ensure_schema(conn)
    row = conn.execute("SELECT 1 FROM account_state WHERE id = 1").fetchone()
    if row is None:
        now = datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO account_state (id, starting_capital, realized_pnl, "
            "peak_equity, created_at) VALUES (1, ?, 0, ?, ?)",
            (float(EQUITY_DESK_CAPITAL_RS), float(EQUITY_DESK_CAPITAL_RS), now))
        conn.commit()
    return conn


def delivery_frictions(side: str, price: float, qty: int) -> float:
    """Cost of one executed delivery leg at the 2026 rates."""
    turnover = float(price) * int(qty)
    stt = STT_RATE_DELIVERY * turnover
    stamp = STAMP_DUTY_RATE_BUY * turnover if side.upper() == "BUY" else 0.0
    services = (EXCHANGE_CHARGE_RATE + SEBI_FEE_RATE) * turnover
    dp = DP_CHARGE_SELL if side.upper() == "SELL" else 0.0
    return round(stt + stamp + services * (1 + GST_RATE) + dp, 2)


def size_entry(entry_price: float, stop: float, desk_equity: float,
               risk_pct: float = None) -> dict:
    """Whole-share qty from the risk budget, capped by the notional
    ceiling. qty 0 (with the reason) when nothing responsibly fits.
    `risk_pct` overrides the config default (the adaptive-sizing seam) —
    the notional ceiling below binds regardless, so no boost can escape
    the desk's max risk cap."""
    risk_per_share = float(entry_price) - float(stop)
    if risk_per_share <= 0:
        return {"qty": 0, "notional": 0.0, "reason": "non-positive risk"}
    if risk_pct is None:
        risk_pct = EQUITY_DESK_RISK_PER_TRADE_PCT
    risk_budget = desk_equity * risk_pct / 100.0
    qty = int(risk_budget // risk_per_share)
    cap = desk_equity * EQUITY_DESK_MAX_NOTIONAL_PCT / 100.0
    qty = min(qty, int(cap // float(entry_price)))
    if qty <= 0:
        return {"qty": 0, "notional": 0.0,
                "reason": "sizing zero (risk budget / notional cap)"}
    return {"qty": qty, "notional": round(qty * float(entry_price), 2),
            "reason": "sized"}


def fund_entry(entry: dict, db_path=None) -> dict:
    """The capital_fn seam for run_darling_cycle: size the entry and lock
    its notional through Dept 3's request_entry — halts, daily breaker and
    cash judged at the single door. Fails CLOSED; the caller logs its
    telemetry row regardless of the verdict."""
    if not EQUITY_DESK_ENABLED:
        return {"funded": False, "reason": "equity desk disabled"}
    action = entry.get("kya_kara_action") or {}
    price, stop = action.get("entry_price"), action.get("stop")
    if price is None or stop is None:
        return {"funded": False, "reason": "entry missing price/stop"}
    # Adaptive sizing consult (Directive 2, decision #81): the feedback
    # layer fails OPEN to neutral inside itself; a veto here still lets
    # the caller log the telemetry row — only the money stays home.
    mult = 1.0
    try:
        from src import adaptive_sizing
        verdict = adaptive_sizing.equity_verdict(entry)
        if verdict.get("action") == "veto":
            return {"funded": False,
                    "reason": f"vetoed_by adaptive_sizing: "
                              f"{verdict.get('detail')}"}
        mult = float(verdict.get("multiplier", 1.0)) or 1.0
    except Exception:
        mult = 1.0
    try:
        conn = connect(db_path)
        try:
            sized = size_entry(price, stop, pm.equity(conn),
                               risk_pct=EQUITY_DESK_RISK_PER_TRADE_PCT * mult)
            if sized["qty"] <= 0:
                pm.log_event(conn, "sizing_zero",
                             f"{entry.get('ticker')}: {sized['reason']}")
                return {"funded": False, "reason": sized["reason"]}
            ref = LOCK_PREFIX + str(entry.get("id"))
            gate = pm.request_entry(conn, ref, sized["notional"])
            if not gate["approved"]:
                return {"funded": False, "reason": gate["reason"]}
            return {"funded": True, "qty": sized["qty"],
                    "notional": sized["notional"], "lock_ref": ref,
                    "reason": ("funded" if mult == 1.0
                               else f"funded (sizing x{mult})")}
        finally:
            conn.close()
    except Exception as exc:
        return {"funded": False, "reason": f"desk unavailable ({exc})"}


def settle_exit(entry: dict, exit_event: dict, db_path=None):
    """The settle_fn seam: net delivery P&L (both-side frictions) settles
    into the desk account and the lock is released. None for unfunded
    entries and refs that never locked (release is a safe no-op)."""
    funding = entry.get("funding") or {}
    qty = funding.get("qty")
    price_in = (entry.get("kya_kara_action") or {}).get("entry_price")
    price_out = exit_event.get("exit_price")
    if (not funding.get("funded") or not qty
            or price_in is None or price_out is None):
        return None
    gross = (float(price_out) - float(price_in)) * int(qty)
    pnl_net = round(gross - delivery_frictions("BUY", price_in, qty)
                    - delivery_frictions("SELL", price_out, qty), 2)
    conn = connect(db_path)
    try:
        ref = funding.get("lock_ref") or LOCK_PREFIX + str(entry.get("id"))
        result = pm.release_margin(conn, ref, pnl_net)
        if not result.get("released"):
            return None
        return {"ticker": exit_event.get("ticker"), "lock_ref": ref,
                "qty": int(qty), "pnl_net": pnl_net,
                "reason": exit_event.get("reason"),
                "equity": result.get("equity"),
                "drawdown_pct": result.get("drawdown_pct"),
                "halted": result.get("halted")}
    finally:
        conn.close()


def sweep_orphan_locks(ledger_path=None, db_path=None) -> list:
    """Reconciler: a settle that crashed mid-run leaves an exited position
    still locked. Re-drive settlement for every active eqd: lock whose
    ledger entry already carries an exit."""
    from src import knowledge_graph_logger as kg
    events = kg.read_events(ledger_path)
    entries = {e.get("id"): e for e in events if e.get("event") == "entry"}
    exits = {e.get("id"): e for e in events if e.get("event") == "exit"}
    conn = connect(db_path)
    try:
        active = [r[0] for r in conn.execute(
            "SELECT journal_ref FROM margin_locks WHERE released_at IS NULL "
            "AND journal_ref LIKE ?", (LOCK_PREFIX + "%",)).fetchall()]
    finally:
        conn.close()
    settled = []
    for ref in active:
        eid = ref[len(LOCK_PREFIX):]
        if eid in exits and eid in entries:
            s = settle_exit(entries[eid], exits[eid], db_path=db_path)
            if s:
                settled.append(s)
    return settled


def summary(db_path=None) -> dict:
    conn = connect(db_path)
    try:
        return pm.account_summary(conn)
    finally:
        conn.close()


def broadcast_activity(shadow: dict, broadcast_fn=None, db_path=None) -> bool:
    """ONE card per EOD run, only when money moved (funded entries and/or
    settlements). Quiet days stay silent — abstain = silent, house rule."""
    funded = [e for e in shadow.get("entries") or []
              if (e.get("funding") or {}).get("funded")]
    settlements = shadow.get("settlements") or []
    if not funded and not settlements:
        return False
    try:
        if broadcast_fn is None:
            from src.notifier import fire_broadcast
            broadcast_fn = fire_broadcast
        lines = []
        for e in funded:
            f = e["funding"]
            lines.append(f"BUY {e['ticker']}: {f['qty']} sh "
                         f"≈ Rs.{f['notional']:,.0f}")
        for s in settlements:
            sign = "+" if s["pnl_net"] >= 0 else ""
            lines.append(f"EXIT {s['ticker']} ({s['reason']}): "
                         f"{sign}Rs.{s['pnl_net']:,.2f} net")
        summ = summary(db_path)
        lines.append(f"Desk: equity Rs.{summ['equity']:,.0f}, "
                     f"cash Rs.{summ['available_cash']:,.0f}, "
                     f"dd {summ['drawdown_pct']:.1f}%"
                     + (" — HALTED" if summ["trading_halted"] else ""))
        broadcast_fn({"event": "equity_desk", "ticker": "EQUITY DESK",
                      "date": datetime.now(IST).date().isoformat(),
                      "description": ("💼 Equity desk (paper capital):\n"
                                      + "\n".join(lines))})
        return True
    except Exception as exc:
        print(f"  (equity desk card failed: {exc})")
        return False


def reserve_firm_slice(conn=None) -> dict:
    """VM-side, run once: lock the desk's slice in the OPTIONS account so
    the firm's 10L is never double-counted. Idempotent — an active ref
    re-approves without double-locking (request_entry's own rule)."""
    from src import brain_map
    owns = conn is None
    if conn is None:
        conn = brain_map.connect()
    try:
        return pm.request_entry(conn, FIRM_ALLOCATION_REF,
                                float(EQUITY_DESK_CAPITAL_RS))
    finally:
        if owns:
            conn.close()


if __name__ == "__main__":
    import sys
    if "--reserve-firm-slice" in sys.argv:
        print(f"firm slice reservation: {reserve_firm_slice()}")
    elif "--sweep" in sys.argv:
        swept = sweep_orphan_locks()
        print(f"swept {len(swept)} orphan lock(s)")
        for s in swept:
            print(" ", s)
    else:
        s = summary()
        print("EQUITY DESK (paper) — "
              f"equity Rs.{s['equity']:,.2f} | "
              f"locked Rs.{s['locked_margin']:,.2f} | "
              f"cash Rs.{s['available_cash']:,.2f} | "
              f"dd {s['drawdown_pct']:.2f}% | halted {s['trading_halted']} | "
              f"open locks {s['open_locks']}")

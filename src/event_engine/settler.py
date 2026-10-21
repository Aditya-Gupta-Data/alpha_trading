"""
The VM half, part 2: manage and settle PAPER_EVENTS positions.

    python -m src.event_engine.settler [--force-hours]

VM ONLY. A long option's worst case is the premium already paid, so there is NO stop: the exits are
  pre_expiry   the ONE forced pre-expiry predicate every book obeys (`plan_tracker.in_forced_exit_window`)
  take_profit  the bid reaches `take_profit_multiple` × the entry premium (default 5×, the penny-to-1 aim)
  horizon      the card's horizon (days since opening) has run out
  expired      past expiry with no chain → settled at zero (the premium is lost)
A long is sold at the BID (no bid → 0, it cannot be sold); a stale or crossed quote abstains this tick.
Money moves only through `portfolio_manager.paper_release_margin` on PAPER_EVENTS.
P&L = (exit − entry) × lot size × lots − 2 × `fees_per_order_rs`.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

from src.event_engine import ACCOUNT, IST, settings, store


def pnl_rs(pos: dict, exit_premium: float, fees_per_order: float) -> float:
    gross = (float(exit_premium) - float(pos["entry_premium"])) * int(pos["lot_size"]) * int(pos["lots"])
    return round(gross - 2.0 * float(fees_per_order), 2)


def decide(pos: dict, bid: float | None, today: date, cfg: dict, forced_fn=None) -> str | None:
    """The exit that fires now, or None to hold."""
    expiry = date.fromisoformat(pos["expiry"])
    if today > expiry:
        return "expired"
    if forced_fn is None:
        from src.plan_tracker import in_forced_exit_window as forced_fn
    if forced_fn(pos["underlying"], expiry, today):
        return "pre_expiry"
    if bid is not None and bid >= float(cfg["take_profit_multiple"]) * float(pos["entry_premium"]):
        return "take_profit"
    opened = datetime.fromisoformat(pos["opened_at"]).date()
    if today >= opened + timedelta(days=int(pos["horizon_days"])):
        return "horizon"
    return None


def settle(conn, pos: dict, exit_premium: float, reason: str, cfg: dict, notify_fn=None) -> dict:
    from src import portfolio_manager as pm
    pnl = pnl_rs(pos, exit_premium, cfg["fees_per_order_rs"])
    rel = pm.paper_release_margin(conn, ACCOUNT, pos["journal_ref"], pnl)
    store.close_position(conn, pos["journal_ref"], exit_premium, reason, pnl)
    store.action(conn, pos["event_id"], pos["underlying"], pos["path"], "closed", reason,
                 {"exit_premium": exit_premium, "pnl_net": pnl, "released": rel.get("released")})
    try:
        fn = notify_fn
        if fn is None:
            from src.notifier import fire_broadcast as fn
        fn({"event": "event_trade", "ticker": pos["underlying"], "date": date.today().isoformat(),
            "text": (f"{'🟢' if pnl > 0 else '🔴'} PAPER_EVENTS closed ({pos['path']} strike, {reason}): "
                     f"{pos['underlying']} {pos['strike']:g}{pos['option_type']} {pos['expiry']} "
                     f"{pos['entry_premium']:g} → {exit_premium:g}, P&L Rs.{pnl:+,.0f}")})
    except Exception as exc:
        print(f"  (event_engine: alert not sent: {exc})")
    return {"journal_ref": pos["journal_ref"], "reason": reason, "pnl": pnl}


def run(conn=None, now: datetime = None, force_hours: bool = False, cfg: dict = None,
        chain_fn=None, forced_fn=None, notify_fn=None, node_check=None) -> dict:
    cfg = cfg or settings()
    now = now or datetime.now(IST)
    out = {"status": "ok", "held": [], "closed": [], "skipped": []}
    from src import node_role
    if (node_check or node_role.is_analytics_node)():
        out["status"] = "refused: analytics node — PAPER_EVENTS settles on the VM only (#146)"
        return out
    if not force_hours:
        from src.execution.live_pricer import _market_open
        if not _market_open(now):
            out["status"] = "market closed"
            return out
    if conn is None:
        from src import brain_map
        conn = brain_map.connect()
    store.ensure_schema(conn)
    from src.execution import live_pricer as lp
    if chain_fn is None:
        from src.event_engine.executor import _default_chain as chain_fn
    today = now.date()
    for pos in store.open_positions(conn):
        leg = {"side": "BUY", "option_type": pos["option_type"], "strike": pos["strike"]}
        try:
            chain = chain_fn(pos["underlying"], pos["expiry"])
        except Exception as exc:
            chain = None
            why = f"chain unavailable: {exc}"
        else:
            why = "chain unavailable: empty" if not chain else None
        if chain is None or not chain:
            if today > date.fromisoformat(pos["expiry"]):
                out["closed"].append(settle(conn, pos, 0.0, "expired", cfg, notify_fn))
            else:
                out["skipped"].append({"journal_ref": pos["journal_ref"], "reason": why})
            continue
        bid, stale = lp.crossed_close_price(leg, lp.leg_quote(chain, leg))
        if bid is None:
            out["skipped"].append({"journal_ref": pos["journal_ref"], "reason": stale})
            continue
        reason = decide(pos, bid, today, cfg, forced_fn)
        if reason:
            out["closed"].append(settle(conn, pos, bid, reason, cfg, notify_fn))
        else:
            store.record_mark(conn, pos["journal_ref"], bid)
            out["held"].append({"journal_ref": pos["journal_ref"], "mark": bid})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Event engine, VM half: settle PAPER_EVENTS positions")
    ap.add_argument("--force-hours", action="store_true")
    a = ap.parse_args(argv)
    r = run(force_hours=a.force_hours)
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] event_engine.settler: {json.dumps(r, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

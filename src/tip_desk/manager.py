"""
The VM loop that manages PAPER_TIP_SANDBOX positions (decision #189).

    python -m src.tip_desk.manager            # cron 09:14 Mon–Fri; runs to 15:30, a tick every poll_seconds
    python -m src.tip_desk.manager --once     # one tick now

Marks a long at the BID and a debit spread at (long leg bid − short leg ask) — `live_pricer.crossed_close_price`;
a quote that abstains holds the row this tick. Per row, in order:
  square_off   expiry day at/after `square_off_hhmm` (15:15): out at the mark (no bid → 0)
  expired      a row still open after its expiry (the loop was down): settled at 0, the premium is lost
  stop_loss    the mark is at/below the row's current stop (the tip's SL, raised by the trail)
  targets      tranche A (`book_at_t1`): out at T1. Trailed rows: T1 → stop to cost, T2 → stop to T1, …,
               the last target → out.
Money moves only through `portfolio_manager.paper_release_margin` on PAPER_TIP_SANDBOX.
P&L = (exit − entry) × lot size × lots − the row's fees.
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime

from src.tip_desk import ACCOUNT, IST, now_ist, settings, store

SESSION_END = (15, 30)


def _hhmm(s: str) -> tuple:
    h, m = str(s).split(":")
    return int(h), int(m)


def pnl_rs(pos: dict, exit_ps: float) -> float:
    gross = (float(exit_ps) - float(pos["entry_premium"])) * int(pos["lot_size"]) * int(pos["lots"])
    return round(gross - float(pos.get("fees_rs") or 0.0), 2)


def evaluate(pos: dict, mark, now: datetime, cfg: dict) -> dict:
    """{action: hold | exit | raise, reason, exit_ps, current_sl, targets_hit}. Pure."""
    today = now.date()
    expiry = date.fromisoformat(pos["expiry"])
    out = {"action": "hold", "reason": None, "exit_ps": None, "current_sl": pos.get("current_sl"),
           "targets_hit": int(pos.get("targets_hit") or 0)}
    if today > expiry:
        return dict(out, action="exit", reason="expired", exit_ps=0.0)
    if today == expiry and (now.hour, now.minute) >= _hhmm(cfg["square_off_hhmm"]):
        if mark is None:
            return dict(out, reason="square-off due but no mark this tick")
        return dict(out, action="exit", reason="square_off_expiry_day", exit_ps=float(mark))
    if mark is None:
        return out
    mark = float(mark)
    sl = pos.get("current_sl")
    if sl is not None and mark <= float(sl):
        why = "stop_loss" if int(pos.get("targets_hit") or 0) == 0 else "trailing_stop"
        return dict(out, action="exit", reason=why, exit_ps=mark)
    targets = [float(t) for t in (pos.get("targets") or [])]
    hits = max(out["targets_hit"], sum(1 for t in targets if mark >= t))
    if hits <= out["targets_hit"]:
        return out
    if pos["rule"] == "book_at_t1":
        return dict(out, action="exit", reason="t1_booked", exit_ps=mark, targets_hit=hits)
    if hits >= len(targets):
        return dict(out, action="exit", reason="final_target", exit_ps=mark, targets_hit=hits)
    new_sl = float(pos["entry_premium"]) if hits == 1 else targets[hits - 2]
    cur = float(sl) if sl is not None else None
    return dict(out, action="raise", reason=f"T{hits} crossed", targets_hit=hits,
                current_sl=max(new_sl, cur) if cur is not None else new_sl)


def mark_of(chain: dict, legs: list):
    """(mark per share, reason): the close-side value of the whole structure off one chain."""
    from src.execution import live_pricer as lp
    total = 0.0
    for leg in legs:
        price, why = lp.crossed_close_price(leg, lp.leg_quote(chain, leg))
        if price is None:
            return None, f"{leg['side']} {float(leg['strike']):g}{leg['option_type']}: {why}"
        total += price if leg["side"] == "BUY" else -price
    return round(max(0.0, total), 2), None


def _notify(text: str, notify_fn=None) -> None:
    try:
        fn = notify_fn
        if fn is None:
            from src.notifier import fire_broadcast as fn
        fn({"event": "tip_trade", "ticker": "TIP_SANDBOX", "date": date.today().isoformat(), "text": text})
    except Exception as exc:
        print(f"  (tip_desk: alert not sent: {exc})")


def label(pos: dict) -> str:
    legs = pos["legs"]
    if len(legs) == 1:
        c = f"{float(legs[0]['strike']):g}{legs[0]['option_type']}"
    else:
        c = "/".join(f"{float(l['strike']):g}" for l in legs) + legs[0]["option_type"] + " spread"
    return f"{pos['underlying']} {c} {pos['expiry']}"


def close(conn, pos: dict, exit_ps: float, reason: str, notify_fn=None) -> dict:
    from src import portfolio_manager as pm
    pnl = pnl_rs(pos, exit_ps)
    rel = pm.paper_release_margin(conn, ACCOUNT, pos["journal_ref"], pnl)    # the lock row is the race guard
    if not rel.get("released"):
        store.close_position(conn, pos["journal_ref"], exit_ps, f"{reason} (lock already released — not booked)", pnl)
        return {"journal_ref": pos["journal_ref"], "closed": False, "reason": "already closed"}
    store.close_position(conn, pos["journal_ref"], exit_ps, reason, pnl)
    store.log(conn, "closed", pos["group_id"], reason=reason,
              detail={"journal_ref": pos["journal_ref"], "tranche": pos["tranche"], "lots": pos["lots"],
                      "entry_ps": pos["entry_premium"], "exit_ps": exit_ps, "pnl_net": pnl,
                      "released": rel.get("released")})
    _notify(f"{'🟢' if pnl > 0 else '🔴'} TIP_SANDBOX closed ({reason}, tranche {pos['tranche']}): "
            f"{label(pos)} × {pos['lots']} lot(s) {float(pos['entry_premium']):g} → {exit_ps:g}, "
            f"P&L Rs.{pnl:+,.0f}", notify_fn)
    return {"journal_ref": pos["journal_ref"], "closed": True, "reason": reason, "pnl": pnl}


def _apply(conn, pos: dict, d: dict, notify_fn=None):
    if d["action"] == "exit":
        return close(conn, pos, d["exit_ps"], d["reason"], notify_fn)
    if d["action"] == "raise":
        store.update_levels(conn, pos["journal_ref"], current_sl=d["current_sl"], targets_hit=d["targets_hit"])
        store.log(conn, "stop_raised", pos["group_id"], reason=d["reason"],
                  detail={"journal_ref": pos["journal_ref"], "current_sl": d["current_sl"]})
        _notify(f"🔼 TIP_SANDBOX {label(pos)} (tranche {pos['tranche']}): {d['reason']} — stop now "
                f"{d['current_sl']:g}", notify_fn)
        return {"journal_ref": pos["journal_ref"], "raised": d["current_sl"]}
    return None


def _chains(rows: list, chain_fn):
    from src.tip_desk.resolver import _default_chain
    out = {}
    for key in sorted({(r["underlying"], r["expiry"]) for r in rows}):
        try:
            out[key] = (chain_fn or _default_chain)(*key)
        except Exception as exc:
            out[key] = None
            print(f"  (tip_desk: chain {key} unavailable: {exc})")
    return out


def tick(conn, now: datetime = None, chain_fn=None, cfg: dict = None, notify_fn=None) -> list:
    """One pass over every open row. Returns what happened, row by row."""
    store.ensure_schema(conn)
    now = now or now_ist()
    cfg = cfg or settings()
    rows = store.positions(conn, "open")
    if not rows:
        return []
    chains = _chains([r for r in rows if date.fromisoformat(r["expiry"]) >= now.date()], chain_fn)
    results = []
    for pos in rows:
        chain = chains.get((pos["underlying"], pos["expiry"]))
        mark, why = mark_of(chain, pos["legs"]) if chain else (None, "no chain")
        if mark is not None:
            store.record_mark(conn, pos["journal_ref"], mark)
        d = evaluate(dict(pos), mark, now, cfg)
        r = _apply(conn, pos, d, notify_fn)
        results.append(r or {"journal_ref": pos["journal_ref"], "held": d.get("reason") or why})
    return results


def close_group_now(conn, group_id: str, now: datetime = None, chain_fn=None, market_open_fn=None,
                    notify_fn=None) -> dict:
    """The owner's [Exit Now]: every open tranche of one tip, at the live close-side price."""
    store.ensure_schema(conn)
    now = now or now_ist()
    rows = store.positions(conn, "open", group_id=group_id)
    if not rows:
        return {"ok": False, "reason": "no open position for this tip"}
    if market_open_fn is None:
        from src.market_loop import is_market_open as market_open_fn
    if not market_open_fn(now):
        return {"ok": False, "reason": "market closed — no fill outside 09:15–15:30"}
    chain = _chains(rows, chain_fn).get((rows[0]["underlying"], rows[0]["expiry"]))
    mark, why = mark_of(chain, rows[0]["legs"]) if chain else (None, "chain unavailable")
    if mark is None:
        return {"ok": False, "reason": f"no exit price right now ({why}) — try again"}
    return {"ok": True, "closed": [close(conn, p, mark, "owner_exit", notify_fn) for p in rows]}


def run(stop_at=SESSION_END, now_fn=None, sleep_fn=time.sleep, once: bool = False) -> int:
    from src import brain_map
    from src.market_loop import is_market_open
    cfg = settings()
    if not cfg.get("enabled"):
        print("[tip_desk] disabled in config (tip_desk.enabled) — nothing to do.")
        return 0
    now_fn = now_fn or now_ist
    from src import nse_calendar
    if not nse_calendar.is_trading_day(now_fn()):
        print("[tip_desk] not an NSE trading day — nothing to manage.")
        return 0
    while True:
        now = now_fn()
        if is_market_open(now):
            conn = brain_map.connect()
            try:
                for r in tick(conn, now=now, cfg=cfg):
                    if "held" not in r:
                        print(f"[tip_desk] {now:%H:%M:%S} {r}", flush=True)
            except Exception as exc:
                print(f"[tip_desk] tick failed (held to the next one): {type(exc).__name__}: {exc}", flush=True)
            finally:
                conn.close()
        if once or (now.hour, now.minute) >= stop_at:
            return 0
        sleep_fn(float(cfg["poll_seconds"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manage PAPER_TIP_SANDBOX positions (stops, targets, square-off)")
    ap.add_argument("--once", action="store_true", help="one tick now, then exit")
    args = ap.parse_args(argv)
    from src import deploy_log, node_role
    refusal = node_role.execution_refusal()
    if refusal:
        print(f"[tip_desk] REFUSED to start: {refusal}", flush=True)
        return 3
    deploy_log.record_startup("tip_desk_manager", kind="job")
    return run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())

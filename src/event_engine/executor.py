"""
The VM half, part 1: event cards → auto-executed long OTM options on PAPER_EVENTS.

    python -m src.event_engine.executor [--dry-run] [--force-hours]

VM ONLY (decision #146 — `node_role.EXECUTION_JOBS` keeps it off the Mini PC, and it refuses there).
Owner rulings (2026-10-21, decision #156): no Approve button for THIS book only; long OTM calls/puts
only; ₹5,000 max loss a trade (the premium paid IS the stop); abstain when one lot costs more; both
strike paths (A fixed OTM, B the LLM's price range) open as separate, tagged trades.

Gates, in order, every one named when it refuses: analytics node · `execute_enabled` (a real run) ·
market open · card already seen · card schema/age · the ticker has an option contract in the VM's
universe · an expiry clears the entry floor · the live chain answers · a strike exists · the ask is
quoted (BUY at the ask, the #70 rule) · ≤ ₹5,000 · the paper-account door approves (its halts and cash).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from src.event_engine import ACCOUNT, CARD_DIR, IST, ROOT, settings, store, strikes
from src.event_engine.card import validate

VM_INBOX = ROOT / "data"                       # where the node's ship lane lands cards
INDEX_ALIASES = {"NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50", "BANKNIFTY": "NIFTY BANK",
                 "FINNIFTY": "NIFTY FIN SERVICE", "MIDCPNIFTY": "NIFTY MID SELECT"}


def instrument(ticker: str):
    """(underlying key, lot size) in the VM's option universe, or (None, reason)."""
    from src.options_proposer import EQUITY_OPTION_UNDERLYINGS, LOT_SIZES
    t = str(ticker or "").strip().upper().removesuffix(".NS")
    t = INDEX_ALIASES.get(t, t)
    if t in LOT_SIZES:
        return t, int(LOT_SIZES[t])
    if f"{t}.NS" in EQUITY_OPTION_UNDERLYINGS:
        return f"{t}.NS", int(EQUITY_OPTION_UNDERLYINGS[f"{t}.NS"])
    return None, f"{t} has no option contract in the VM's option universe"


def journal_ref(event_id: str, ticker: str, path: str) -> str:
    return "evt:" + hashlib.sha1(f"{event_id}|{ticker}|{path}".encode()).hexdigest()[:12]


def card_files(dirs=None) -> list:
    out = []
    for d, pattern in (dirs or ((VM_INBOX, "event_card__*.json"), (CARD_DIR, "*.json"))):
        d = Path(d)
        if d.is_dir():
            out.extend(sorted(d.glob(pattern)))
    return out


def _default_expiries(underlying):
    from src.dhan_client import get_expiry_list
    return get_expiry_list(underlying) or []


def _default_chain(underlying, expiry):
    from src.execution.live_pricer import _default_chain as chain
    return chain(underlying, expiry)


def _floor_days(underlying: str) -> int:
    try:
        from src.options_proposer import min_days_to_expiry_for
        return int(min_days_to_expiry_for(underlying))
    except Exception:
        return 7


def execute_suggestion(conn, card: dict, s: dict, today: date, dry_run: bool, cfg: dict,
                       expiries_fn=None, chain_fn=None, notify_fn=None) -> list:
    from src import portfolio_manager as pm
    from src.execution import live_pricer as lp
    eid, ticker = card["event_id"], s["ticker"]
    results = []

    def note(path, kind, reason=None, detail=None):
        results.append({"ticker": ticker, "path": path, "action": kind, "reason": reason, **(detail or {})})
        if not dry_run:
            store.action(conn, eid, ticker, path, kind, reason, detail)

    und, lot = instrument(ticker)
    if und is None:
        note(None, "abstained", lot)
        return results
    ot = strikes.option_type_for(s.get("direction"))
    if ot is None:
        note(None, "abstained", f"direction {s.get('direction')!r}")
        return results
    try:
        exps = (expiries_fn or _default_expiries)(und)
    except Exception as exc:
        exps = []
        note(None, "abstained", f"expiry list unavailable: {exc}")
        return results
    expiry = strikes.choose_expiry(exps, today, s.get("horizon_days") or cfg["horizon_days_default"],
                                   _floor_days(und))
    if expiry is None:
        note(None, "abstained", "no expiry clears the entry floor")
        return results
    try:
        chain = (chain_fn or _default_chain)(und, expiry)
    except Exception as exc:
        chain = None
        note(None, "abstained", f"chain unavailable: {exc}")
        return results
    if not chain or not chain.get("last_price"):
        note(None, "abstained", "chain unavailable: empty")
        return results
    spot = float(chain["last_price"])
    paths = s.get("strike_paths") or {}
    picks = {"fixed": strikes.pick_fixed(chain, spot, ot, float((paths.get("fixed") or {}).get("otm_pct",
                                                                   cfg["fixed_otm_pct"]))),
             "llm": strikes.pick_llm(chain, spot, ot, (paths.get("llm") or {}).get("price_response_pct"))}
    max_loss = float(cfg["max_loss_per_trade_rs"])         # the hard cap; a card cannot raise it
    for path in strikes.PATHS:
        pk = picks[path]
        if not pk["ok"]:
            note(path, "abstained", pk["reason"])
            continue
        if path == "llm" and picks["fixed"]["ok"] and pk["strike"] == picks["fixed"]["strike"]:
            note(path, "same_strike", "path B chose path A's strike — one trade covers both",
                 {"strike": pk["strike"]})
            continue
        leg = {"side": "BUY", "option_type": ot, "strike": pk["strike"]}
        price, why = lp.crossed_open_price(leg, lp.leg_quote(chain, leg))
        if price is None:
            note(path, "abstained", f"{pk['strike']:g}{ot}: {why}")
            continue
        sz = strikes.size_lots(price, lot, max_loss)
        if sz["lots"] < 1:
            note(path, "abstained", sz["reason"])
            continue
        ref = journal_ref(eid, ticker, path)
        detail = {"underlying": und, "expiry": expiry, "strike": pk["strike"], "option_type": ot,
                  "premium": price, "lots": sz["lots"], "lot_size": lot, "premium_total_rs": sz["premium_total_rs"],
                  "rule": pk["rule"], "spot": spot}
        if dry_run:
            note(path, "planned", None, detail)
            continue
        verdict = pm.paper_request_entry(conn, ACCOUNT, ref, required_margin=sz["premium_total_rs"],
                                         lots=sz["lots"])
        if not verdict.get("approved"):
            note(path, "refused", verdict.get("reason"), detail)
            continue
        basis = s.get("basis") or {}
        store.open_position(conn, {
            "journal_ref": ref, "event_id": eid, "underlying": und, "direction": s["direction"], "path": path,
            "option_type": ot, "strike": pk["strike"], "expiry": expiry, "lots": sz["lots"], "lot_size": lot,
            "entry_premium": price, "entry_total_rs": sz["premium_total_rs"], "opened_at": store.now_iso(),
            "horizon_days": int(s.get("horizon_days") or cfg["horizon_days_default"]),
            "materiality_source": basis.get("materiality_source"),
            "response_source": (paths.get("llm") or {}).get("source")})
        note(path, "opened", None, detail)
        try:
            fn = notify_fn
            if fn is None:
                from src.notifier import fire_broadcast as fn
            fn({"event": "event_trade", "ticker": und, "date": today.isoformat(),
                "text": (f"🎯 PAPER_EVENTS opened ({path} strike): BUY {sz['lots']} × {und} "
                         f"{pk['strike']:g}{ot} {expiry} @ {price:g} = Rs.{sz['premium_total_rs']:,.0f} "
                         f"(max loss). Event: {(card.get('source') or {}).get('title') or eid}. "
                         f"Priced-in: {basis.get('priced_in')}; materiality {basis.get('materiality_pct')} % "
                         f"({basis.get('materiality_source')}).")})
        except Exception as exc:
            print(f"  (event_engine: alert not sent: {exc})")
    return results


def run(conn=None, dirs=None, now: datetime = None, dry_run: bool = False, force_hours: bool = False,
        cfg: dict = None, expiries_fn=None, chain_fn=None, notify_fn=None, node_check=None) -> dict:
    cfg = cfg or settings()
    now = now or datetime.now(IST)
    out = {"status": "ok", "cards": 0, "results": []}
    from src import node_role
    if (node_check or node_role.is_analytics_node)():
        out["status"] = "refused: analytics node — PAPER_EVENTS executes on the VM only (#146)"
        return out
    if not dry_run and not cfg.get("execute_enabled"):
        out["status"] = "disabled (event_engine.execute_enabled false)"
        return out
    if not dry_run and not force_hours:
        from src.execution.live_pricer import _market_open
        if not _market_open(now):
            out["status"] = "market closed"
            return out
    if conn is None:
        from src import brain_map
        conn = brain_map.connect()
    store.ensure_schema(conn)
    max_age = int(cfg.get("card_max_age_days", 5))
    for path in card_files(dirs):
        try:
            card = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        eid = card.get("event_id") if isinstance(card, dict) else None
        if not eid or (not dry_run and store.seen(conn, eid)):
            continue
        out["cards"] += 1
        probs = validate(card)
        try:
            age = (now.date() - datetime.fromisoformat(card["generated_at"]).date()).days
        except (KeyError, TypeError, ValueError):
            age = None
        if age is None or age > max_age:
            probs.append(f"card is {age} days old (max {max_age})")
        if probs:
            if not dry_run:
                store.mark_seen(conn, eid, "rejected", "; ".join(probs))
            out["results"].append({"event_id": eid, "rejected": probs})
            continue
        for s in card.get("suggestions") or []:
            out["results"].extend(execute_suggestion(conn, card, s, now.date(), dry_run, cfg,
                                                     expiries_fn, chain_fn, notify_fn))
        if not dry_run:
            store.mark_seen(conn, eid, "executed", f"{len(card.get('suggestions') or [])} suggestion(s)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Event engine, VM half: execute event cards on PAPER_EVENTS")
    ap.add_argument("--dry-run", action="store_true", help="resolve strikes and sizes, write nothing")
    ap.add_argument("--force-hours", action="store_true", help="skip the market-open check")
    a = ap.parse_args(argv)
    r = run(dry_run=a.dry_run, force_hours=a.force_hours)
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] event_engine.executor: {json.dumps(r, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

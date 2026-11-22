"""What the gateway's /api/tipdesk/* endpoints call. Every tip that arrives is written to the append-only ledger,
whatever happens to it. A card is never a fill: only [Confirm Paper] opens a position, re-quoted live at that
moment (the card's price is a preview). Paper only — `mode` other than "paper" is refused (RULE 7)."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.tip_desk import ACCOUNT, EXIT_SIGNAL, IGNORED_KINDS, UNSAFE_REJECT, now_ist, settings, store
from src.tip_desk import manager, parser, resolver, sizing


def _ready(conn) -> None:
    from src import portfolio_manager as pm
    pm.ensure_accounts_schema(conn)
    store.ensure_schema(conn)


def _book(conn) -> dict:
    from src import portfolio_manager as pm
    return {"free_cash_rs": pm.paper_available_cash(conn, ACCOUNT),
            "open_risk_rs": pm.paper_locked_margin(conn, ACCOUNT),
            "equity_rs": pm.paper_equity(conn, ACCOUNT)}


def open_summary(conn) -> list:
    """One item per tip with open tranches (the EXIT_SIGNAL card and /api/tipdesk/open)."""
    by_group = {}
    for p in store.positions(conn, "open"):
        g = by_group.setdefault(p["group_id"], {"group_id": p["group_id"], "label": manager.label(p),
                                                "entry_ps": p["entry_premium"], "lots": 0, "premium_rs": 0.0,
                                                "last_mark": p.get("last_mark"), "tranches": []})
        g["lots"] += int(p["lots"])
        g["premium_rs"] = round(g["premium_rs"] + float(p["entry_total_rs"]), 2)
        g["tranches"].append({"tranche": p["tranche"], "lots": p["lots"], "current_sl": p["current_sl"],
                              "targets": p["targets"], "targets_hit": p["targets_hit"]})
    return list(by_group.values())


def _levels_onto_open(conn, group_id: str, parsed: dict) -> list:
    """SL/targets that arrive after the confirm update the open rows; a stop the trail already raised stays."""
    changed = []
    for p in store.positions(conn, "open", group_id=group_id):
        sl, targets = parsed.get("sl"), parsed.get("targets") or None
        cur = p.get("current_sl")
        new_cur = sl if (sl is not None and int(p.get("targets_hit") or 0) == 0) else None
        if sl is None and targets is None:
            continue
        store.update_levels(conn, p["journal_ref"], sl=sl, targets=targets,
                            current_sl=new_cur if new_cur is not None and new_cur != cur else None)
        changed.append(p["journal_ref"])
    return changed


def ingest(conn, group_id: str, text: str, message_ids: list = None, now: datetime = None, expiries_fn=None,
           chain_fn=None, cfg: dict = None) -> dict:
    _ready(conn)
    cfg = cfg or settings()
    now = now or now_ist()
    group_id = str(group_id)
    parsed = parser.classify(text, now.date())
    kind = parsed["kind"]
    base = {"group_id": group_id, "kind": kind, "reason": parsed.get("reason"), "parsed": parsed}

    def log(event, reason=None, detail=None):
        store.log(conn, event, group_id, kind=kind, reason=reason or parsed.get("reason"), raw_text=text,
                  parsed=parsed, detail=dict(detail or {}, message_ids=message_ids or []))

    if not cfg.get("enabled"):
        log("ignored", "tip desk disabled in config")
        return dict(base, action="ignore", reason="tip desk disabled in config")
    if kind in IGNORED_KINDS:
        log("ignored")
        return dict(base, action="ignore")
    if kind == UNSAFE_REJECT:
        log("rejected_unsafe")
        return dict(base, action="reject")
    if kind == EXIT_SIGNAL:
        positions = open_summary(conn)
        log("exit_signal", detail={"open_tips": len(positions)})
        return dict(base, action="exit_prompt", positions=positions)

    card_id = store.card_id_for(group_id)
    existing = store.get_card(conn, card_id)
    if existing and existing["status"] == "confirmed":
        changed = _levels_onto_open(conn, group_id, parsed)
        log("levels_updated", detail={"journal_refs": changed, "sl": parsed.get("sl"),
                                      "targets": parsed.get("targets")})
        return dict(base, action="levels_updated", card_id=card_id, journal_refs=changed)
    if existing and existing["status"] in ("cancelled", "expired", "filling"):
        log("ignored", f"card already {existing['status']}")
        return dict(base, action="ignore", reason=f"card already {existing['status']}")

    res = resolver.resolve(parsed, now.date(), expiries_fn=expiries_fn, chain_fn=chain_fn)
    payload = {"card_id": card_id, "group_id": group_id, "kind": kind, "parsed": parsed, "resolved": res,
               "quoted_at": now.isoformat(timespec="seconds"),
               "expires_at": (now + timedelta(minutes=int(cfg["card_ttl_minutes"]))).isoformat(timespec="seconds"),
               "book": _book(conn)}
    if not res["ok"]:
        store.save_card(conn, card_id, group_id, "refused", kind, dict(payload, refusal=res["reason"]))
        log("refused_contract", res["reason"], {"resolved": res})
        return dict(base, action="refuse", card_id=card_id, reason=res["reason"], card=payload)
    sz = sizing.size_tip(res["cost_ps"], res["lot_size"], len(res["legs"]), payload["book"]["free_cash_rs"],
                         payload["book"]["open_risk_rs"], cfg)
    st = sizing.stale(res["cost_ps"], parsed.get("entry_low"), parsed.get("entry_high"), cfg["stale_band_pct"])
    payload.update(sizing=sz, stale=st)
    status = "open" if sz["lots"] >= 1 else "refused"
    if sz["lots"] < 1:
        payload["refusal"] = sz["reason"]
    store.save_card(conn, card_id, group_id, status, kind, payload)
    event = ("card_updated" if existing else "carded") if status == "open" else "refused_size"
    log(event, sz["reason"], {"cost_ps": res["cost_ps"], "lots": sz["lots"], "stale": st["stale"],
                              "expiry": res["expiry"], "underlying": res["underlying"]})
    return dict(base, action="card" if status == "open" else "refuse", card_id=card_id, card=payload,
                is_update=existing is not None, reason=sz["reason"] if status != "open" else None)


def _expired(card: dict, now: datetime) -> bool:
    exp = card["payload"].get("expires_at")
    return bool(exp) and now > datetime.fromisoformat(exp)


def confirm(conn, card_id: str, mode: str = "paper", now: datetime = None, chain_fn=None, market_open_fn=None,
            cfg: dict = None, notify_fn=None) -> dict:
    from src import portfolio_manager as pm
    _ready(conn)
    cfg = cfg or settings()
    now = now or now_ist()
    card = store.get_card(conn, card_id)
    if card is None:
        return {"ok": False, "status": "not_found", "reason": f"no card {card_id}"}
    group_id, pl = card["group_id"], card["payload"]

    def refuse(status, reason):
        store.log(conn, "confirm_refused", group_id, kind=card["kind"], reason=reason,
                  detail={"card_id": card_id, "mode": mode})
        return {"ok": False, "status": status, "reason": reason}

    if str(mode).lower() != "paper":
        return refuse("paper_only", "paper only — the system has no order path (RULE 7)")
    if card["status"] != "open":
        return refuse(card["status"], f"card is {card['status']}")
    if _expired(card, now):
        store.set_card_status(conn, card_id, "expired", only_if="open")
        return refuse("expired", f"card expired after {cfg['card_ttl_minutes']} min — paste the tip again")
    if market_open_fn is None:
        from src.market_loop import is_market_open as market_open_fn
    if not market_open_fn(now):
        return refuse("market_closed", "market closed — no fill outside 09:15–15:30")
    res = pl["resolved"]
    if res["expiry"] == now.date().isoformat() and (now.hour, now.minute) >= manager._hhmm(cfg["square_off_hhmm"]):
        return refuse("past_square_off", f"expiry day after {cfg['square_off_hhmm']} — the square-off time")
    try:
        chain = (chain_fn or resolver._default_chain)(res["underlying"], res["expiry"])
    except Exception as exc:
        return refuse("no_quote", f"chain unavailable: {exc}")
    priced = resolver.price_legs(chain, pl["parsed"]["legs"], res["expiry"])
    if not priced["ok"]:
        return refuse("no_quote", priced["reason"])
    book = _book(conn)
    sz = sizing.size_tip(priced["cost_ps"], res["lot_size"], len(priced["legs"]), book["free_cash_rs"],
                         book["open_risk_rs"], cfg)
    if sz["lots"] < 1:
        return refuse("size", sz["reason"])
    if not store.set_card_status(conn, card_id, "filling", only_if="open"):
        return refuse("busy", "card is already being confirmed")
    parsed = pl["parsed"]
    st = sizing.stale(priced["cost_ps"], parsed.get("entry_low"), parsed.get("entry_high"), cfg["stale_band_pct"])
    fees = sizing.split_fees(cfg["fees_per_order_rs"], len(priced["legs"]), sz["n_tranches"])
    plan = sizing.tranches(sz["lots"])
    opened = []
    for (tranche, lots, rule), fee in zip(plan, fees):
        ref = f"tip:{group_id}:{tranche}"
        premium = round(priced["cost_ps"] * res["lot_size"] * lots, 2)
        verdict = pm.paper_request_entry(conn, ACCOUNT, ref, required_margin=round(premium + fee, 2), lots=lots)
        if not verdict.get("approved"):
            for o in opened:                                    # nothing half-filled: give back what was taken
                pm.paper_release_margin(conn, ACCOUNT, o["journal_ref"], 0.0)
                store.close_position(conn, o["journal_ref"], o["entry_premium"], "fill_rolled_back", 0.0)
            store.set_card_status(conn, card_id, "open", only_if="filling")
            return refuse("door_refused", verdict.get("reason"))
        row = {"journal_ref": ref, "card_id": card_id, "group_id": group_id, "tranche": tranche,
               "underlying": res["underlying"], "expiry": res["expiry"],
               "structure": parsed.get("structure") or "long",
               "legs": [{k: l[k] for k in ("side", "strike", "option_type", "price")} for l in priced["legs"]],
               "lots": lots, "lot_size": res["lot_size"], "entry_premium": priced["cost_ps"],
               "entry_total_rs": premium, "fees_rs": fee, "tip_entry_low": parsed.get("entry_low"),
               "tip_entry_high": parsed.get("entry_high"), "stale": 1 if st["stale"] else 0,
               "sl": parsed.get("sl"), "targets": parsed.get("targets") or [], "current_sl": parsed.get("sl"),
               "rule": rule, "opened_at": now.isoformat(timespec="seconds")}
        store.insert_position(conn, row)
        opened.append(row)
    store.set_card_status(conn, card_id, "confirmed", only_if="filling")
    slip = (round(priced["cost_ps"] - float(parsed["entry_high"]), 2)
            if parsed.get("entry_high") is not None else None)
    store.log(conn, "opened", group_id, kind=card["kind"],
              detail={"card_id": card_id, "mode": "paper", "fill_ps": priced["cost_ps"],
                      "card_ps": res["cost_ps"], "slippage_vs_band_top_ps": slip, "stale": st,
                      "lots": sz["lots"], "tranches": [(o["tranche"], o["lots"]) for o in opened],
                      "premium_total_rs": sz["premium_total_rs"], "fees_rs": sz["fees_rs"]})
    manager._notify(f"🎯 TIP_SANDBOX opened (paper): {manager.label(opened[0])} × {sz['lots']} lot(s) @ "
                    f"{priced['cost_ps']:g} = Rs.{sz['premium_total_rs']:,.0f} (+Rs.{sz['fees_rs']:,.0f} fees = "
                    f"max loss){' ⚠️ STALE' if st['stale'] else ''}", notify_fn)
    return {"ok": True, "status": "opened", "card_id": card_id, "fill_ps": priced["cost_ps"], "lots": sz["lots"],
            "premium_total_rs": sz["premium_total_rs"], "max_loss_rs": sz["max_loss_rs"], "stale": st,
            "positions": opened, "book": _book(conn)}


def cancel(conn, card_id: str) -> dict:
    _ready(conn)
    card = store.get_card(conn, card_id)
    if card is None:
        return {"ok": False, "status": "not_found", "reason": f"no card {card_id}"}
    if not store.set_card_status(conn, card_id, "cancelled", only_if="open"):
        return {"ok": False, "status": card["status"], "reason": f"card is {card['status']}"}
    store.log(conn, "cancelled", card["group_id"], kind=card["kind"], detail={"card_id": card_id})
    return {"ok": True, "status": "cancelled", "card_id": card_id}


def exit_now(conn, group_id: str, now: datetime = None, chain_fn=None, market_open_fn=None, notify_fn=None) -> dict:
    _ready(conn)
    out = manager.close_group_now(conn, str(group_id), now=now, chain_fn=chain_fn, market_open_fn=market_open_fn,
                                  notify_fn=notify_fn)
    if not out["ok"]:
        store.log(conn, "exit_refused", str(group_id), reason=out["reason"])
    return out

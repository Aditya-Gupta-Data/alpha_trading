"""Pure text for the #tip-desk replies (the bot renders it; no engine import, no state)."""
from __future__ import annotations


def _rs(v) -> str:
    return f"Rs.{float(v or 0):,.0f}"


def _g(v) -> str:
    return "—" if v is None else f"{float(v):g}"


def contract(res: dict, legs=None) -> str:
    legs = legs or res.get("legs") or []
    if len(legs) == 1:
        c = f"{float(legs[0]['strike']):g} {legs[0]['option_type']}"
    else:
        c = " / ".join(f"{l['side']} {float(l['strike']):g}{l['option_type']}" for l in legs)
    return f"{res.get('underlying')} {c} · expiry {res.get('expiry')}"


TRADING_SYMBOLS = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
                   "NIFTY MID SELECT": "MIDCPNIFTY"}


def trading_symbol(underlying: str) -> str:
    """The name the Dhan app searches by: NIFTY, BANKNIFTY, RELIANCE — not our internal keys."""
    u = str(underlying or "")
    return TRADING_SYMBOLS.get(u, u.removesuffix(".NS"))


def order_line(res: dict, lots: int) -> str:
    qty = int(lots) * int(res.get("lot_size") or 0)
    parts = [f"{l['side']} {trading_symbol(res.get('underlying'))} {float(l['strike']):g}{l['option_type']} "
             f"{res.get('expiry')} "
             f"qty {qty} LIMIT {float(l['price']):g}" for l in res.get("legs") or []]
    return " + ".join(parts)


def trade_card(card: dict) -> dict:
    """{title, lines, stale, can_confirm} for a carded tip (open or refused)."""
    p, res, sz, st = card["parsed"], card["resolved"], card.get("sizing") or {}, card.get("stale") or {}
    band = (f"{_g(p.get('entry_low'))}–{_g(p.get('entry_high'))}" if p.get("entry_low") != p.get("entry_high")
            else _g(p.get("entry_low")))
    live = res.get("cost_ps")
    vs = ""
    if st.get("pct_over") is not None and st.get("vs_band") == "above":
        vs = f" (+{st['pct_over']:g}% over the band)"
    elif st.get("vs_band") == "below":
        vs = " (below the band)"
    title = ("⚠️ STALE PRICE — " if st.get("stale") else "") + f"🎯 Tip card · {card['kind'].replace('_', ' ')}"
    lines = [f"**Contract:** {contract(res)}",
             f"**Tip entry:** {band} · **Live {'ask' if len(res.get('legs') or []) == 1 else 'net debit'}:** "
             f"{_g(live)}{vs}"]
    if st.get("stale"):
        lines.append(f"⚠️ **STALE PRICE:** live is {st['pct_over']:g}% above the tip's top of {_g(p.get('entry_high'))}"
                     " — you would be buying late")
    book = card.get("book") or {}
    if sz.get("lots"):
        after = float(book.get("free_cash_rs") or 0) - float(sz["max_loss_rs"])
        lines.append(f"**Size:** {sz['lots']} lot(s) × {res['lot_size']} = {_rs(sz['premium_total_rs'])} "
                     f"(+{_rs(sz['fees_rs'])} fees) → **max loss {_rs(sz['max_loss_rs'])}**")
        lines.append(f"**TIP_SANDBOX:** free {_rs(book.get('free_cash_rs'))} → {_rs(after)} after · open risk "
                     f"{_rs(book.get('open_risk_rs'))}")
    else:
        lines.append(f"❌ **Not sized:** {card.get('refusal') or sz.get('reason')}")
    tg = " / ".join(_g(t) for t in p.get("targets") or []) or "none given"
    lines.append(f"**SL:** {_g(p.get('sl')) if p.get('sl') is not None else 'none given (the premium is the stop)'}"
                 f" · **Targets:** {tg}")
    if sz.get("lots", 0) >= 2:
        a = sz["lots"] // 2
        lines.append(f"**Plan:** book {a} lot(s) at T1 and move the stop to cost; trail the other "
                     f"{sz['lots'] - a} target by target")
    elif sz.get("lots"):
        lines.append("**Plan:** trail the stop up as each target is crossed; out at the last target")
    if res.get("expiry") and str(card.get("quoted_at", ""))[:10] == res["expiry"]:
        lines.append("⏰ Expires today — squared off automatically at 15:15")
    if sz.get("lots"):
        lines.append(f"📋 Order details: `{order_line(res, sz['lots'])}`")
        lines.append(f"_Paper only. Re-quoted live when you tap Confirm. Valid until "
                     f"{str(card.get('expires_at', ''))[11:16]}._")
    return {"title": title, "lines": lines, "stale": bool(st.get("stale")), "can_confirm": bool(sz.get("lots"))}


def refusal_text(reply: dict) -> str:
    return f"❌ Can't trade this tip: {reply.get('reason')}"


def unsafe_text(reply: dict) -> str:
    why = str(reply.get("reason") or "").removeprefix("unsafe: ")
    return f"⛔ **UNSAFE — not traded:** {why}. Logged for the tipster's record."


def exit_lines(positions: list) -> list:
    if not positions:
        return ["No open TIP_SANDBOX position — nothing to exit."]
    out = []
    for i, p in enumerate(positions, 1):
        stops = ", ".join(f"{t['tranche']}: SL {_g(t['current_sl'])}" for t in p["tranches"])
        out.append(f"**{i}.** {p['label']} · {p['lots']} lot(s) @ {_g(p['entry_ps'])} · last {_g(p.get('last_mark'))}"
                   f" · {stops}")
    return out


def opened_text(reply: dict) -> str:
    stale = " ⚠️ STALE" if (reply.get("stale") or {}).get("stale") else ""
    return (f"✅ **Paper position opened**{stale}: {reply['lots']} lot(s) @ {_g(reply['fill_ps'])} = "
            f"{_rs(reply['premium_total_rs'])} · max loss {_rs(reply['max_loss_rs'])} · "
            f"sandbox free now {_rs((reply.get('book') or {}).get('free_cash_rs'))}")

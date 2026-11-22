"""Parsed tip → a real, live Dhan contract and its crossed price.

Validation is against Dhan itself, through the one data door: the underlying must be in the VM's option
universe (`event_engine.executor.instrument`), the expiry must be in Dhan's live expiry list for it, and every
strike must be present in that expiry's live chain with a quoted side (BUY at the ask, SELL at the bid — the
#70 rule, `live_pricer.crossed_open_price`). Anything missing is a named refusal, never a guess."""
from __future__ import annotations

from datetime import date

BSE_UNDERLYINGS = {"SENSEX", "BANKEX"}


def _default_expiries(underlying):
    from src.dhan_client import get_expiry_list
    return get_expiry_list(underlying) or []


def _default_chain(underlying, expiry):
    from src.execution.live_pricer import _default_chain as chain
    return chain(underlying, expiry)


def instrument(name: str):
    """(underlying key, lot size) or (None, reason)."""
    if not name:
        return None, "no underlying in the tip"
    if str(name).upper() in BSE_UNDERLYINGS:
        return None, f"{name} options trade on BSE — not wired yet (the next build after the tip desk)"
    from src.event_engine.executor import instrument as universe
    und, lot = universe(name)
    if und is None and " " in str(name):
        und2, lot2 = universe(str(name).replace(" ", ""))      # "HDFC BANK" → HDFCBANK
        if und2 is not None:
            return und2, lot2
    return und, lot


def choose_expiry(expiries: list, hint: dict, today: date):
    """(expiry ISO, None) or (None, reason)."""
    live = sorted(e for e in (str(x)[:10] for x in expiries or []) if e >= today.isoformat())
    if not live:
        return None, "Dhan listed no live expiry"
    hint = hint or {"kind": "nearest"}
    kind = hint.get("kind")
    if kind == "nearest":
        return live[0], None
    if kind == "next":
        return (live[1], None) if len(live) > 1 else (None, "no next expiry listed")
    if kind == "date":
        return (hint["value"], None) if hint["value"] in live else \
            (None, f"expiry {hint['value']} is not in Dhan's list ({', '.join(live[:4])}…)")
    if kind == "month":
        inmonth = [e for e in live if int(e[5:7]) == int(hint["value"])]
        return (inmonth[-1], None) if inmonth else (None, "no expiry listed in that month")
    return None, f"could not read the expiry ({hint.get('value')})"


def resolve(parsed: dict, today: date, expiries_fn=None, chain_fn=None) -> dict:
    und, lot = instrument(parsed.get("underlying"))
    if und is None:
        return {"ok": False, "reason": lot}
    try:
        exps = (expiries_fn or _default_expiries)(und)
    except Exception as exc:
        return {"ok": False, "reason": f"expiry list unavailable: {exc}", "underlying": und}
    expiry, why = choose_expiry(exps, parsed.get("expiry_hint"), today)
    if expiry is None:
        return {"ok": False, "reason": why, "underlying": und}
    try:
        chain = (chain_fn or _default_chain)(und, expiry)
    except Exception as exc:
        return {"ok": False, "reason": f"chain unavailable: {exc}", "underlying": und, "expiry": expiry}
    priced = price_legs(chain, parsed.get("legs") or [], expiry)
    if not priced["ok"]:
        return dict(priced, underlying=und, expiry=expiry)
    legs, cost = priced["legs"], priced["cost_ps"]
    return {"ok": True, "underlying": und, "lot_size": int(lot), "expiry": expiry, "legs": legs,
            "cost_ps": cost, "spot": chain.get("last_price")}


def price_legs(chain: dict, legs: list, expiry: str) -> dict:
    """Open-side prices for every leg off one live chain → {ok, legs, cost_ps} or {ok: False, reason}."""
    from src.execution import live_pricer as lp
    if not chain or not chain.get("oc"):
        return {"ok": False, "reason": "chain unavailable: empty"}
    out = []
    for leg in legs:
        q = lp.leg_quote(chain, leg)
        label = f"{leg['side']} {float(leg['strike']):g}{leg['option_type']}"
        if q.get("absent"):
            return {"ok": False, "reason": f"{label}: strike not in the {expiry} chain"}
        price, why = lp.crossed_open_price(leg, q)
        if price is None:
            return {"ok": False, "reason": f"{label}: {why}"}
        out.append({"side": leg["side"], "strike": float(leg["strike"]), "option_type": leg["option_type"],
                    "price": price, "bid": q.get("bid"), "ask": q.get("ask"), "ltp": q.get("ltp")})
    if not out:
        return {"ok": False, "reason": "no contract in the tip"}
    cost = round(sum(l["price"] * (1 if l["side"] == "BUY" else -1) for l in out), 2)
    if len(out) == 2:
        width = abs(out[0]["strike"] - out[1]["strike"])
        if not 0 < cost < width:
            return {"ok": False, "reason": f"net debit {cost:g} is outside 0..{width:g} (no-arbitrage) — quotes off"}
    return {"ok": True, "legs": out, "cost_ps": cost}

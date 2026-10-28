"""
next_gen_engine/execution_algo.py — limit-chasing paper execution
==================================================================

Blueprint Phase 2, Stage-4 execution quality (owner, 2026-07-17). Today's
paper fills (decision #70) assume an instant fill at the touch: BUY at
top_ask, SELL at top_bid — honest, but it concedes the full half-spread
on every leg. A real desk works the order: post at mid, give the market a
chance to come to you, and walk toward the touch as the window runs out.
This module builds that plan — for the PAPER simulation.

    plan_limit_chase(...)  ->  timed ladder of limit prices, mid -> touch
    simulate_chase_fill(.) ->  which step would have filled, given quotes
    sequence_spread_legs(.) -> protective (long) legs FIRST, then shorts

There is NO real order routing anywhere in this system (paper-only,
decision #11 human-in-the-loop). The value is a more truthful slippage
model: fills recorded somewhere between mid and touch instead of always
at the touch, with the concession path logged.

Contract:
  * PURE: quotes in, plan/fill out. No clocks, no network, no threads —
    the 30-second window is expressed as step offsets the caller schedules.
  * Fail-honest: a crossed/empty book refuses to plan ({"error": ...}).
  * Unfilled at window end -> honest MISS (the caller decides: cross the
    spread at the touch, or walk away) — never a phantom mid fill.
  * Tick-aware: prices snap to the exchange tick (NSE options = 0.05).
  * CANONICAL MERGE TARGET: the _leg_fill layer in src/options_proposer.py
    (fill_basis gains a "chase" variant beside "quoted"/"ltp").
"""

DEFAULT_WINDOW_S = 30.0
DEFAULT_STEPS = 6
DEFAULT_TICK = 0.05      # NSE F&O tick size


def _snap(price: float, tick: float, side: str) -> float:
    """Snap to tick, always toward the passive side (a BUY rounds down,
    a SELL rounds up) so snapping never crosses more than intended."""
    steps = price / tick
    snapped = (int(steps) if side == "buy" else -int(-steps)) * tick
    return round(snapped, 2)


def plan_limit_chase(side: str, top_bid: float, top_ask: float,
                     window_s: float = DEFAULT_WINDOW_S,
                     steps: int = DEFAULT_STEPS,
                     tick: float = DEFAULT_TICK) -> dict:
    """The chase ladder: step 0 posts at MID, the final step posts AT the
    touch (ask for a buy, bid for a sell), intermediate steps walk the
    remaining distance linearly. Each rung: {t_offset_s, limit_price}.

    Refuses (error, no plan) on a crossed, locked-to-zero, or missing
    book — chasing garbage quotes would just launder bad data into
    plausible-looking fills."""
    if side not in ("buy", "sell"):
        return {"error": f"unknown side {side!r}"}
    if not top_bid or not top_ask or top_bid <= 0 or top_ask <= 0:
        return {"error": "empty/zero book — no chase plan"}
    if top_bid > top_ask:
        return {"error": f"crossed book (bid {top_bid} > ask {top_ask})"}
    if steps < 2:
        steps = 2
    mid = (top_bid + top_ask) / 2
    touch = top_ask if side == "buy" else top_bid
    rungs = []
    for i in range(steps):
        frac = i / (steps - 1)                    # 0 -> mid, 1 -> touch
        raw = mid + (touch - mid) * frac
        rungs.append({
            "t_offset_s": round(window_s * i / (steps - 1), 2),
            "limit_price": _snap(raw, tick, side),
        })
    return {"side": side, "mid": round(mid, 2), "touch": touch,
            "window_s": window_s, "tick": tick, "rungs": rungs,
            "mode": "PAPER"}


def simulate_chase_fill(plan: dict, quotes_at_steps: list) -> dict:
    """Replay the chase against the book as it stood at each rung.
    `quotes_at_steps[i]` = {"top_bid": ..., "top_ask": ...} at rung i's
    time (short lists = book unchanged after the last known quote).

    A BUY rung fills when its limit >= that moment's ask; a SELL rung
    fills when its limit <= that moment's bid. First rung to fill wins.
    No rung fills -> {"filled": False} and the honest concession the
    caller would pay to cross at the end is reported."""
    if plan.get("error"):
        return {"filled": False, "error": plan["error"]}
    side, rungs = plan["side"], plan["rungs"]
    last_quote = None
    for i, rung in enumerate(rungs):
        q = quotes_at_steps[i] if i < len(quotes_at_steps) else last_quote
        if not q:
            continue
        last_quote = q
        against = q.get("top_ask") if side == "buy" else q.get("top_bid")
        if not against or against <= 0:
            continue
        crosses = (rung["limit_price"] >= against if side == "buy"
                   else rung["limit_price"] <= against)
        if crosses:
            # price improvement vs. paying the touch of the ORIGINAL book
            improvement = (plan["touch"] - against if side == "buy"
                           else against - plan["touch"])
            return {"filled": True, "fill_price": against,
                    "rung": i, "t_offset_s": rung["t_offset_s"],
                    "improvement_vs_touch": round(improvement, 2),
                    "fill_basis": "chase"}
    end_touch = None
    if last_quote:
        end_touch = (last_quote.get("top_ask") if side == "buy"
                     else last_quote.get("top_bid"))
    return {"filled": False, "rungs_tried": len(rungs),
            "cross_now_price": end_touch,
            "note": "window expired unfilled — caller decides: cross at "
                    "the touch or walk away (never auto-filled at mid)"}


def sequence_spread_legs(legs: list) -> list:
    """Protective-leg-first ordering for multi-leg options spreads: BUY
    (long/protective) legs execute before SELL (short) legs, so the book
    is never short-naked mid-execution and SPAN margin sees the hedge
    before the short lands. Within each group the original order is kept
    (stable). Legs are dicts with side "buy"/"sell" (options_proposer's
    leg shape); unknown sides sort with the shorts (conservative: last)."""
    buys = [l for l in legs if str(l.get("side", "")).lower() == "buy"]
    rest = [l for l in legs if str(l.get("side", "")).lower() != "buy"]
    return buys + rest


def work_multi_leg_chase(
    legs: list,
    quotes_by_leg: dict,
    window_s: float = DEFAULT_WINDOW_S,
    steps: int = DEFAULT_STEPS,
    tick: float = DEFAULT_TICK,
) -> dict:
    """Work a multi-leg spread in protective-wings-first order (`sequence_spread_legs`)
    using `plan_limit_chase` + `simulate_chase_fill` (`docs/ARCHITECTURE_ROADMAP.md` §5 Vector 1).

    If any protective `BUY` leg fails to fill within its chase window (or encounters
    an invalid/crossed book), execution halts immediately (`aborted_unhedged_wing`)
    and no subsequent short `SELL` leg is ever submitted — guaranteeing zero
    naked-short exposure at every instant."""
    ordered = sequence_spread_legs(legs)
    results = []
    wing_failed = False

    for idx, leg in enumerate(ordered):
        side = str(leg.get("side", "")).lower()
        leg_key = leg.get("leg_id") or leg.get("strike") or idx
        q_series = quotes_by_leg.get(leg_key) or []
        if wing_failed and side != "buy":
            results.append({
                "leg_key": leg_key,
                "side": side,
                "strike": leg.get("strike"),
                "filled": False,
                "aborted": True,
                "reason": "aborted_unhedged_wing: protective BUY leg did not fill",
            })
            continue
        if not q_series:
            res = {"filled": False, "error": "no quotes for leg"}
        else:
            q0 = q_series[0]
            plan = plan_limit_chase(
                side,
                top_bid=float(q0.get("top_bid") or 0.0),
                top_ask=float(q0.get("top_ask") or 0.0),
                window_s=window_s,
                steps=steps,
                tick=tick,
            )
            res = simulate_chase_fill(plan, q_series)
        entry = {
            "leg_key": leg_key,
            "side": side,
            "strike": leg.get("strike"),
            **res,
        }
        results.append(entry)
        if side == "buy" and not res.get("filled"):
            wing_failed = True

    all_filled = bool(results) and all(r.get("filled") for r in results)
    return {
        "mode": "PAPER",
        "wings_first_verified": True,
        "all_filled": all_filled,
        "aborted_unhedged_wing": wing_failed,
        "legs": results,
    }


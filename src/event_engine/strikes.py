"""
Strike selection, the A/B test the Architect asked for — pure functions, used by the VM executor on
the live chain. Every event suggestion opens BOTH paths as separate trades (paired comparison):

  Path A  "fixed"  the nearest listed strike at least `fixed_otm_pct` (default 3%) out of the money
  Path B  "llm"    the strike nearest the price the card's price-response range implies
                   (spot × (1 + base%)), kept out of the money; abstains without a range

Both are long OTM calls (bullish) or puts (bearish). Sizing is the same for both: as many lots as
keep premium × lot size × lots ≤ `max_loss_per_trade_rs` (₹5,000); the premium paid is the stop.
One lot over the cap → abstain, named. When B lands on A's strike, B is recorded as
`same_strike_as_fixed` and not opened twice.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

PATHS = ("fixed", "llm")


def option_type_for(direction: str) -> str | None:
    return {"bullish": "CE", "bearish": "PE"}.get(direction)


def _strikes(chain: dict) -> list:
    out = []
    for k in ((chain or {}).get("oc") or {}):
        try:
            out.append(float(k))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def otm_strike(strikes: list, spot: float, option_type: str, target: float):
    """The first listed strike at or beyond `target` on the OTM side, and strictly OTM."""
    if option_type == "CE":
        c = [k for k in strikes if k >= target and k > spot]
        return min(c) if c else None
    c = [k for k in strikes if k <= target and k < spot]
    return max(c) if c else None


def pick_fixed(chain: dict, spot: float, option_type: str, otm_pct: float) -> dict:
    target = spot * (1 + otm_pct / 100.0) if option_type == "CE" else spot * (1 - otm_pct / 100.0)
    k = otm_strike(_strikes(chain), spot, option_type, target)
    if k is None:
        return {"ok": False, "reason": f"no listed strike {otm_pct:g}% OTM"}
    return {"ok": True, "strike": k, "target": round(target, 2), "rule": f"fixed {otm_pct:g}% OTM"}


def pick_llm(chain: dict, spot: float, option_type: str, response_pct) -> dict:
    """`response_pct` = the card's [low, base, high] signed % move (an `estimate:llm`)."""
    if not response_pct or len(response_pct) != 3:
        return {"ok": False, "reason": "no price-response range on the card"}
    base = abs(float(response_pct[1]))
    target = spot * (1 + base / 100.0) if option_type == "CE" else spot * (1 - base / 100.0)
    strikes = _strikes(chain)
    side = [k for k in strikes if (k > spot if option_type == "CE" else k < spot)]
    if not side:
        return {"ok": False, "reason": "no OTM strike listed"}
    k = min(side, key=lambda s: (abs(s - target), s if option_type == "CE" else -s))
    return {"ok": True, "strike": k, "target": round(target, 2), "rule": f"llm base {base:g}% move"}


def size_lots(premium: float, lot_size: int, max_loss_rs: float) -> dict:
    """Lots such that premium × lot_size × lots ≤ max_loss_rs. 0 → abstain (named)."""
    try:
        per_lot = float(premium) * int(lot_size)
    except (TypeError, ValueError):
        return {"lots": 0, "reason": "no premium or lot size"}
    if per_lot <= 0:
        return {"lots": 0, "reason": "premium is zero"}
    lots = int(math.floor(float(max_loss_rs) / per_lot + 1e-9))
    if lots < 1:
        return {"lots": 0, "per_lot_rs": round(per_lot, 2),
                "reason": f"one lot costs Rs.{per_lot:,.0f} > the Rs.{float(max_loss_rs):,.0f} cap"}
    return {"lots": lots, "per_lot_rs": round(per_lot, 2), "premium_total_rs": round(per_lot * lots, 2),
            "reason": None}


def choose_expiry(expiries: list, today: date, horizon_days: int, floor_days: int = 7) -> str | None:
    """The first expiry at least max(floor, horizon) days out (capped at 60); when the chain does
    not reach that far, the furthest one beyond the floor. None when nothing clears the floor."""
    days = []
    for e in expiries or []:
        try:
            d = date.fromisoformat(str(e)[:10])
        except ValueError:
            continue
        if (d - today).days >= floor_days:
            days.append(d)
    if not days:
        return None
    days.sort()
    want = today + timedelta(days=max(floor_days, min(int(horizon_days or 0), 60)))
    later = [d for d in days if d >= want]
    return (later[0] if later else days[-1]).isoformat()

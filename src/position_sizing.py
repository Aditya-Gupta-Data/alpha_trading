"""
src/position_sizing.py — FIXED-FRACTIONAL RISK SIZING, the one door (V1.1,
decision #106, 2026-09-23, architect directive).

Replaces the static Rs.10,000 per-trade cap of decision #84. The risk a single
trade may carry is a FRACTION of the specific account's total equity:

    risk_capacity = equity × ACCOUNT_RISK_PER_TRADE_PCT / 100
    lots          = max(1, floor(risk_capacity / max_loss_per_lot))

then bounded by what the account's liquid cash can actually margin
(`floor(available_cash / margin_per_lot)`), which may be zero — margin is the
hard wall, the 1-lot floor is not allowed to breach it.

Every account is sized INDEPENDENTLY on its own equity: the Rs.10L primary and
the Rs.2L shadow account see the same structure and the same percentage and
come out with different lot counts — that is the whole point of the shadow
account, and it no longer inherits the primary's lots as a ceiling.

`floor_applied` is reported honestly: when one lot's max loss already exceeds
the risk capacity the trade still sizes at the 1-lot minimum, but the row says
so. The floor has a HARD CAP (architect ruling 2026-10-16): one lot whose max
loss exceeds ACCOUNT_MAX_LOT_RISK_PCT (2.5%) of the account's equity is
refused, never floored. `hard_cap_pct=None` lifts the cap (the shadow learner,
#140: an unlimited paper budget).

Pure arithmetic, no I/O; the callers supply the equity they mean.
"""
from __future__ import annotations

import math

MIN_LOTS = 1
_CONFIG_CAP = object()


def _config_cap() -> float:
    from src.config import ACCOUNT_MAX_LOT_RISK_PCT
    return ACCOUNT_MAX_LOT_RISK_PCT


def risk_capacity(equity: float, risk_pct: float) -> float:
    """Rupees one trade may put at risk on an account of `equity`."""
    return max(0.0, float(equity)) * max(0.0, float(risk_pct)) / 100.0


def fractional_lots(equity: float, max_loss_per_lot: float, risk_pct: float,
                    margin_per_lot: float = None, available_cash: float = None,
                    min_lots: int = MIN_LOTS, hard_cap_pct=_CONFIG_CAP) -> dict:
    """The sizing verdict for ONE account.

    Returns {lots, by_risk, by_margin, risk_capacity_rs, risk_pct, equity,
    risk_at_lots_rs, floor_applied, hard_cap_pct, hard_capped, reason}. lots 0
    when the loss is unmeasurable, one lot breaches the hard cap, or the
    account cannot margin a single lot."""
    if hard_cap_pct is _CONFIG_CAP:
        hard_cap_pct = _config_cap()
    max_loss = float(max_loss_per_lot or 0.0)
    cap = risk_capacity(equity, risk_pct)
    out = {"lots": 0, "by_risk": 0, "by_margin": None,
           "risk_capacity_rs": round(cap, 2), "risk_pct": float(risk_pct),
           "equity": round(float(equity), 2), "risk_at_lots_rs": 0.0,
           "floor_applied": False, "hard_cap_pct": hard_cap_pct, "hard_capped": False,
           "reason": ""}
    if max_loss <= 0:
        out["reason"] = "unmeasurable max loss"
        return out
    by_risk = int(math.floor(cap / max_loss))
    if by_risk < int(min_lots) and hard_cap_pct is not None:
        ceiling = risk_capacity(equity, hard_cap_pct)
        if max_loss * int(min_lots) > ceiling:
            out.update({"by_risk": by_risk, "hard_capped": True,
                        "reason": (f"1-lot max loss Rs.{max_loss:,.0f} exceeds the {float(hard_cap_pct):g}% "
                                   f"hard cap Rs.{ceiling:,.0f} on Rs.{float(equity):,.0f} equity")})
            return out
    lots = max(int(min_lots), by_risk)
    floor_applied = by_risk < int(min_lots)
    by_margin = None
    if margin_per_lot is not None and float(margin_per_lot) > 0 and available_cash is not None:
        by_margin = int(math.floor(max(0.0, float(available_cash)) / float(margin_per_lot)))
        lots = min(lots, by_margin)
    out.update({"by_risk": by_risk, "by_margin": by_margin,
                "floor_applied": bool(floor_applied and lots >= int(min_lots))})
    if lots <= 0:
        out["reason"] = (f"SPAN margin Rs.{float(margin_per_lot):,.0f}/lot exceeds "
                         f"liquid cash Rs.{float(available_cash):,.0f}")
        return out
    out["lots"] = lots
    out["risk_at_lots_rs"] = round(lots * max_loss, 2)
    out["reason"] = ("sized" if not out["floor_applied"] else
                     f"1-lot floor: max loss Rs.{max_loss:,.0f}/lot exceeds the "
                     f"{float(risk_pct):g}% risk capacity Rs.{cap:,.0f} on "
                     f"Rs.{float(equity):,.0f} equity")
    return out

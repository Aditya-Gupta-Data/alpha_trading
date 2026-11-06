"""Long single-leg options — LONG_CALL / LONG_PUT — for the Shadow Learner ONLY (decision #182).

Entry: the BUY leg of a directional spread the proposer built (bull call → its ATM call,
bear put → its ATM put), at most Rs.15,000 of premium paid per trade (lots cut to fit,
refused when one lot is over). Max loss = the premium; no other account may hold one.

Exits, judged on GAIN % of the premium paid (mark / entry − 1), on the live arm's crossed marks:
  premium_stop     gain <= −50%
  trail_hit        a peak-gain ladder locks a floor (TRAIL_LADDER); out when gain falls below it
  theta_time_stop  half the days to expiry gone and gain still < +20%
  pre_expiry_exit  the shared forced-exit window (plan_tracker)
"""
from __future__ import annotations

from datetime import date

LONG_CALL = "long_call"
LONG_PUT = "long_put"
STRATEGIES = frozenset({LONG_CALL, LONG_PUT})
ACCOUNT = "PAPER_SHADOW_LEARNER"
MAX_PREMIUM_RS = 15_000.0
FROM_SPREAD = {"bull_call_spread": (LONG_CALL, "CE"), "bear_put_spread": (LONG_PUT, "PE")}

PREMIUM_STOP_PCT = -50.0
TRAIL_LADDER = ((50.0, 0.0), (100.0, 50.0), (200.0, 120.0), (300.0, 200.0))   # (peak gain, locked gain)
THETA_HOLD_FRACTION = 0.5
THETA_MIN_GAIN_PCT = 20.0


def is_long_option(strategy) -> bool:
    return strategy in STRATEGIES


def refusal_for_account(strategy, account) -> str | None:
    if is_long_option(strategy) and account != ACCOUNT:
        return f"{strategy} is a Shadow Learner archetype only (#182) — refused for {account}"
    return None


def from_spread(spread: dict, expiry: str = None, entry_spot=None) -> dict | None:
    """The long option on a built directional spread's BUY leg, or None (neutral / malformed)."""
    kind = FROM_SPREAD.get((spread or {}).get("strategy"))
    if kind is None:
        return None
    strategy, opt = kind
    buys = [l for l in spread.get("legs") or []
            if str(l.get("side")).upper() == "BUY" and str(l.get("option_type")).upper() == opt]
    lot = int(spread.get("lot_size") or 0)
    if len(buys) != 1 or lot <= 0:
        return None
    leg = dict(buys[0], side="BUY", option_type=opt)
    prem, strike = float(leg.get("premium") or 0), float(leg["strike"])
    if prem <= 0:
        return None
    return {"strategy": strategy, "direction": "bullish" if strategy == LONG_CALL else "bearish",
            "legs": [leg], "lot_size": lot, "spread_width": 0.0, "net_credit": None,
            "net_debit": round(prem, 2), "max_loss": round(prem * lot, 2),
            "max_profit": round(max(0.0, strike - prem) * lot, 2),
            "margin": {"total_margin": round(prem * lot, 2), "naked_margin": 0.0, "offset_savings": 0.0},
            "lots": 1, "expiry": expiry or spread.get("expiry"),
            "entry_spot": entry_spot if entry_spot is not None else spread.get("entry_spot")}


def premium_paid_rs(premium_ps: float, lot_size: int, lots: int) -> float:
    return round(float(premium_ps) * int(lot_size) * int(lots), 2)


def cap_lots(premium_ps: float, lot_size: int, lots: int, cap: float = MAX_PREMIUM_RS) -> int:
    """The most lots (<= `lots`) whose total premium stays <= cap; 0 when one lot is over it."""
    per_lot = float(premium_ps) * int(lot_size)
    if per_lot <= 0:
        return 0
    return max(0, min(int(lots), int(cap // per_lot)))


def cap_reason(premium_ps: float, lot_size: int) -> str:
    return (f"premium cap: 1 lot costs Rs.{premium_paid_rs(premium_ps, lot_size, 1):,.2f} "
            f"> Rs.{MAX_PREMIUM_RS:,.0f} per trade (#182)")


def bounds(spread: dict, d: float) -> dict:
    """{width_ps, max_profit_ps, max_loss_ps}: the premium is the loss; the strike bounds the value."""
    strike = float(spread["legs"][0]["strike"])
    return {"width_ps": 0.0, "max_profit_ps": round(max(0.0, strike - d), 4), "max_loss_ps": round(d, 4)}


def gain_pct(mark_ps: float, entry_ps: float) -> float:
    return (float(mark_ps) / float(entry_ps) - 1.0) * 100.0 if float(entry_ps) > 0 else 0.0


def lock_for(peak_gain_pct, lock_pct=None):
    """The locked gain floor for a peak gain; only rises (a stored lock is kept). None while unarmed."""
    lock = None if lock_pct is None else float(lock_pct)
    if peak_gain_pct is None:
        return lock
    for arm_at, floor in TRAIL_LADDER:
        if float(peak_gain_pct) >= arm_at and (lock is None or floor > lock):
            lock = floor
    return lock


def theta_due(opened_on: date, expiry: date, today: date) -> bool:
    total = (expiry - opened_on).days
    return total > 0 and (today - opened_on).days >= THETA_HOLD_FRACTION * total


def exit_signal(gain: float, lock, opened_on: date, expiry: date, today: date) -> str:
    """'hold' or the long-option exit that fires (the forced pre-expiry window is the caller's)."""
    if gain <= PREMIUM_STOP_PCT:
        return "premium_stop"
    if lock is not None and gain < float(lock):
        return "trail_hit"
    if gain < THETA_MIN_GAIN_PCT and theta_due(opened_on, expiry, today):
        return "theta_time_stop"
    return "hold"

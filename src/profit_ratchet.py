"""
src/profit_ratchet.py — THE ASYMMETRIC PROFIT RATCHET for directional options
spreads (decision #110, 2026-09-24, architect: "edge is entirely in the
exit — cut losses without compromising positives").

A bull call / bear put no longer takes profit at a fixed 65% of max profit.
Its capture (modeled profit ÷ max profit, in %) is watched; the ladder ARMS
and STEPS a locked floor behind the peak capture since entry:

    peak ≥ 40%  → lock  0%   (breakeven: the trade can no longer lose)
    peak ≥ 60%  → lock 30%
    peak ≥ 80%  → lock 50%
    peak ≥ 90%  → lock 70%

The lock only ever rises (it is a function of the PEAK, and the persisted
lock is a floor). EXIT = `ratchet_hit` when capture falls strictly below the
locked level; a spread that never arms rides to the pre-expiry exit; one that
reaches full capture rides to the pre-expiry exit too (theta harvest done).
Neutral structures (condor / butterfly) are NOT on this ladder: they keep
the 65% static take (they earn theta, not a fat tail). Max loss stays the
structure itself (#105). FORWARD-LOOKING: bars before
RATCHET_EFFECTIVE_DATE feed the peak (a fact) but can never trigger the exit
(the Issue 31 rule). Pure; the tracker and the live bridge share it.
"""
from __future__ import annotations

DEFAULT_LADDER = ((40.0, 0.0), (60.0, 30.0), (80.0, 50.0), (90.0, 70.0))


def ladder() -> tuple:
    try:
        from src.config import RATCHET_LADDER
        rungs = tuple((float(a), float(b)) for a, b in RATCHET_LADDER)
        return tuple(sorted(rungs)) or DEFAULT_LADDER
    except Exception:
        return DEFAULT_LADDER


def is_directional(spread: dict) -> bool:
    from src.strategy_router import family_of
    return family_of(str((spread or {}).get("strategy") or "")) == "directional"


def locked_pct(peak_capture_pct: float | None, rungs: tuple = None) -> float | None:
    """The locked floor for a given peak capture; None while unarmed."""
    if peak_capture_pct is None:
        return None
    lock = None
    for arm_at, lock_at in (rungs or ladder()):
        if float(peak_capture_pct) >= arm_at:
            lock = lock_at
    return lock


def ratchet_hit(capture_pct: float | None, lock: float | None) -> bool:
    """THE one exit predicate: armed and strictly below the lock."""
    if lock is None or capture_pct is None:
        return False
    return float(capture_pct) < float(lock)


def state(peak_capture_pct: float | None, persisted_lock: float | None = None,
          rungs: tuple = None) -> dict:
    lock = locked_pct(peak_capture_pct, rungs)
    if persisted_lock is not None:
        lock = persisted_lock if lock is None else max(float(lock), float(persisted_lock))
    return {"peak_capture_pct": (round(float(peak_capture_pct), 2)
                                 if peak_capture_pct is not None else None),
            "locked_pct": lock, "armed": lock is not None}


def walk(captures: list, effective_date: str, persisted_lock: float | None = None,
         rungs: tuple = None) -> dict:
    """captures = [(day, capture_pct), ...] oldest first (post-entry bars).
    Returns the final state plus `hit_day` / `hit_capture` for the FIRST bar
    on/after `effective_date` where capture < lock. Every bar feeds the
    peak; only eligible bars may fire."""
    peak, lock, hit_day, hit_capture = None, persisted_lock, None, None
    for day, cap in captures:
        cap = float(cap)
        peak = cap if peak is None else max(peak, cap)
        st = state(peak, lock, rungs)
        lock = st["locked_pct"]
        if hit_day is None and day >= effective_date and ratchet_hit(cap, lock):
            hit_day, hit_capture = day, cap
            break
    st = state(peak, lock, rungs)
    st.update({"hit_day": hit_day, "hit_capture_pct": hit_capture})
    return st

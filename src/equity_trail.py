"""
src/equity_trail.py — V1.2 SMART EXITS for the equity desk (decision #107,
2026-09-23): the ATR TRAILING STOP on a funded long darling, as pure
arithmetic over the underlying's daily bars.

    trail_t = max(trail_{t-1}, highest_high_since_entry_t − MULT × ATR_N(t))
    floor   = the plan's hard stop (the trail starts there, never below it)

One-way ratchet: the level can only rise. It is recomputed by walking the
bars from the entry day forward, so a shrinking ATR or a lower high can never
lower it (the `current=` argument of `plan_tracker.atr_trailing_stop` holds
the max). The live quote joins the walk as today's provisional high, so an
intraday spike lifts the trail before the daily bar closes. EXIT when the
price is BELOW the trail (a touch is not a break).

The two primitives are the tracker's own (`atr_from_bars`,
`atr_trailing_stop`) — the options-side equity path (`plan.trailing`) and the
desk now share ONE ratchet. Below `atr_n + 1` bars there is no ATR and no
trail: the position keeps its static target — a guess never moves a stop.
"""
from __future__ import annotations

from datetime import date, timedelta

from src.plan_tracker import atr_from_bars, atr_trailing_stop

LOOKBACK_DAYS = 45          # calendar days of bars fetched before the entry
_BARS_CACHE: dict = {}      # (security_id, start, today) -> bars; one fetch per session


def compute_trail(entry_date: str, hard_stop: float, bars: list,
                  atr_mult: float, atr_n: int, live_price: float = None) -> dict:
    """bars = [(day, low, high, close), ...] oldest first, INCLUDING history
    before the entry (for the ATR). Returns {armed, trail, extreme, atr,
    bars_since_entry}; armed False when no ATR could be measured."""
    bars = sorted((b for b in (bars or []) if b and b[0]), key=lambda b: b[0])
    ratchet, extreme, atr, since = None, None, None, 0
    floor = float(hard_stop) if hard_stop is not None else None
    for i, (day, _low, high, _close) in enumerate(bars):
        if day < entry_date:
            continue
        since += 1
        extreme = float(high) if extreme is None else max(extreme, float(high))
        a = atr_from_bars(bars[:i + 1], atr_n)
        if a is None:
            continue
        atr = a
        ratchet = atr_trailing_stop(extreme, atr, atr_mult, floor=floor,
                                    current=ratchet, bullish=True)
    if live_price is not None and atr is not None:
        extreme = float(live_price) if extreme is None else max(extreme, float(live_price))
        ratchet = atr_trailing_stop(extreme, atr, atr_mult, floor=floor,
                                    current=ratchet, bullish=True)
    return {"armed": ratchet is not None,
            "trail": round(ratchet, 2) if ratchet is not None else None,
            "extreme": extreme, "atr": round(atr, 4) if atr is not None else None,
            "bars_since_entry": since}


def trail_hit(trail: float | None, price: float | None) -> bool:
    """THE one predicate: strictly below the trail."""
    if trail is None or price is None:
        return False
    return float(price) < float(trail)


def bars_for(security_id, entry_date: str, bars_fn=None, today: date = None) -> list:
    """Daily bars from LOOKBACK_DAYS before entry, cached per session so a
    60-second desk cycle costs one historical call per position per day."""
    today = today or date.today()
    start = (date.fromisoformat(entry_date[:10]) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    key = (str(security_id), start, today.isoformat())
    if key in _BARS_CACHE:
        return _BARS_CACHE[key]
    if bars_fn is None:
        from src.dhan_client import get_ohlc_since_by_id as bars_fn
    try:
        raw = bars_fn(security_id, start) or []
    except Exception:
        raw = []
    bars = [(b["date"], b["low"], b["high"], b["close"]) for b in raw
            if isinstance(b, dict) and b.get("date") is not None]
    if bars:
        _BARS_CACHE[key] = bars
    return bars


def trail_for_position(entry: dict, live_price: float = None, bars_fn=None,
                       id_fn=None, today: date = None,
                       atr_mult: float = None, atr_n: int = None) -> dict:
    """The desk's door: resolve the darling's scrip id, fetch its bars, run
    the ratchet. Never raises; `armed False` with a named reason when the
    trail cannot be measured (no id, no bars, too few bars)."""
    from src.config import EQUITY_TRAIL_ATR_MULT, EQUITY_TRAIL_ATR_N, EQUITY_TRAIL_ENABLED
    atr_mult = EQUITY_TRAIL_ATR_MULT if atr_mult is None else float(atr_mult)
    atr_n = EQUITY_TRAIL_ATR_N if atr_n is None else int(atr_n)
    out = {"armed": False, "trail": None, "extreme": None, "atr": None,
           "bars_since_entry": 0, "atr_mult": atr_mult, "atr_n": atr_n, "reason": ""}
    if not EQUITY_TRAIL_ENABLED:
        out["reason"] = "equity_trail_disabled"
        return out
    action = entry.get("kya_kara_action") or {}
    entry_date = str(entry.get("as_of") or "")[:10]
    if not entry_date or action.get("stop") is None:
        out["reason"] = "entry missing as_of/stop"
        return out
    try:
        if id_fn is None:
            from src.equity_desk import security_id_for as id_fn
        sid = id_fn(entry.get("ticker"))
    except Exception:
        sid = None
    if not sid:
        out["reason"] = "no_scrip_master_id"
        return out
    bars = bars_for(sid, entry_date, bars_fn=bars_fn, today=today)
    if not bars:
        out["reason"] = "no_daily_bars"
        return out
    t = compute_trail(entry_date, action.get("stop"), bars, atr_mult, atr_n,
                      live_price=live_price)
    out.update(t)
    out["reason"] = "armed" if t["armed"] else f"atr_needs_{atr_n + 1}_bars"
    return out

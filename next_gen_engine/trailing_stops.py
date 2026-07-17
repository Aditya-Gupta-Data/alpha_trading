"""
next_gen_engine/trailing_stops.py — Wilder-ATR trailing stops (advisory)
=========================================================================

Blueprint Phase 1 (owner, 2026-07-17); the roadmap's long-queued "ATR
trailing stops" item. Volatility-scaled exit levels for open positions:
wide when the instrument is noisy, tight when it is quiet, and RATCHETING
— a trailing stop only ever moves in the position's favour.

Math: Wilder's ATR (true range smoothed with alpha = 1/period), the
textbook chandelier-style trail:

    long:  stop = highest_close_since_entry − multiplier × ATR
    short: stop = lowest_close_since_entry  + multiplier × ATR

Contract:
  * PURE: bars in, levels out. Bars are dicts with high/low/close (the
    exact shape dhan_client.get_daily_ohlc returns). No I/O, no state.
  * Ratchet enforced in update_trailing_stop: pass the previous stop and
    the new level can only tighten, never widen.
  * ADVISORY, like every exit signal in this system (decision #68's
    "advisory-only exits"): the caller alerts, a human/auto-approve path
    decides. Nothing here closes anything.
  * CANONICAL MERGE TARGETS: atr() -> src/indicators.py (beside sma/rsi);
    the advisory sweep over open positions -> src/live_bridge.py next to
    the existing profit-take/exit alerts.
"""

DEFAULT_PERIOD = 14
DEFAULT_MULTIPLIER = 2.0


def true_ranges(bars: list) -> list:
    """TR per bar (from the 2nd bar on): max(H−L, |H−prevC|, |L−prevC|)."""
    out = []
    for prev, cur in zip(bars, bars[1:]):
        h, l = float(cur["high"]), float(cur["low"])
        pc = float(prev["close"])
        out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def atr(bars: list, period: int = DEFAULT_PERIOD) -> float | None:
    """Wilder-smoothed ATR from daily OHLC bars (oldest first). None when
    history is too short (< period+1 bars) — abstain, never guess."""
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    value = sum(trs[:period]) / period          # seed: simple average
    for tr in trs[period:]:                     # Wilder: alpha = 1/period
        value = (value * (period - 1) + tr) / period
    return round(value, 4)


def trailing_stop_level(bars_since_entry: list, side: str = "long",
                        period: int = DEFAULT_PERIOD,
                        multiplier: float = DEFAULT_MULTIPLIER) -> dict:
    """The raw chandelier level from the bars since entry (oldest first,
    ideally with `period` bars of pre-entry history prepended for a stable
    ATR seed). NULL-honest: not enough history -> {"stop": None, error}."""
    a = atr(bars_since_entry, period)
    if a is None:
        return {"stop": None, "atr": None,
                "error": f"insufficient history ({len(bars_since_entry)} "
                         f"bars, need {period + 1})"}
    closes = [float(b["close"]) for b in bars_since_entry]
    if side == "long":
        anchor = max(closes)
        stop = anchor - multiplier * a
    else:
        anchor = min(closes)
        stop = anchor + multiplier * a
    return {"stop": round(stop, 2), "atr": a, "anchor_close": round(anchor, 2),
            "side": side, "multiplier": multiplier, "period": period}


def update_trailing_stop(prev_stop: float | None, bars_since_entry: list,
                         side: str = "long",
                         period: int = DEFAULT_PERIOD,
                         multiplier: float = DEFAULT_MULTIPLIER) -> dict:
    """The ratchet: recompute the level and combine with the previous stop
    so the trail only ever tightens (max for longs, min for shorts). An
    uncomputable new level keeps the previous stop — a data gap must never
    LOOSEN protection."""
    level = trailing_stop_level(bars_since_entry, side, period, multiplier)
    new = level.get("stop")
    if new is None:
        return {**level, "stop": prev_stop, "ratcheted": False,
                "note": "level uncomputable — previous stop retained"}
    if prev_stop is not None:
        combined = max(prev_stop, new) if side == "long" else min(prev_stop, new)
    else:
        combined = new
    return {**level, "stop": round(combined, 2),
            "ratcheted": prev_stop is not None and combined != new
            or prev_stop is None}


def stop_hit(stop: float | None, last_price: float | None,
             side: str = "long") -> bool | None:
    """Advisory trigger check. None (abstain) when either input is missing."""
    if stop is None or last_price is None:
        return None
    return last_price <= stop if side == "long" else last_price >= stop

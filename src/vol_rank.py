"""
src/vol_rank.py — HISTORICAL VOLATILITY RANK (decision #109, 2026-09-23).

The desk has no IV history (decision #36: chains are only being captured
since 08-07), so the volatility edge is measured on REALIZED vol, which the
close history already carries:

    rv_t   = stdev(log returns over the last WINDOW sessions) × √252
    rank   = (rv_now − min(rv over LOOKBACK)) / (max − min) × 100     (HV Rank)
    pctile = share of the LOOKBACK rolling rvs at or below rv_now × 100 (HV %ile)

Both are reported on every proposal as a DIAGNOSTIC. The #109 entry gate on
this number was withdrawn by decision #110 (entries are not filtered on it).
Pure; no I/O. Not enough history = None everywhere, never a guessed rank.
"""
from __future__ import annotations

import math

ANNUALISE = math.sqrt(252.0)


def realized_vol(closes: list, window: int) -> float | None:
    """Annualised close-to-close volatility of the LAST `window` returns."""
    if closes is None or len(closes) < window + 1:
        return None
    tail = [float(x) for x in closes[-(window + 1):]]
    if any(x <= 0 for x in tail):
        return None
    rets = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * ANNUALISE


def hv_rank(closes: list, window: int = 14, lookback: int = 252) -> dict:
    """{available, rank, percentile, current, low, high, n_window, window,
    lookback}. `n_window` = how many rolling rvs the range was built from;
    below max(20, lookback // 4) the rank is not trustworthy and
    `available` is False."""
    out = {"available": False, "rank": None, "percentile": None, "current": None,
           "low": None, "high": None, "n_window": 0, "window": window, "lookback": lookback}
    if not closes or len(closes) < window + 2:
        return out
    closes = [float(x) for x in closes]
    series = []
    start = max(window, len(closes) - lookback)
    for end in range(start, len(closes) + 1):
        rv = realized_vol(closes[:end], window)
        if rv is not None:
            series.append(rv)
    if not series:
        return out
    cur = series[-1]
    lo, hi = min(series), max(series)
    out.update({"current": round(cur, 4), "low": round(lo, 4), "high": round(hi, 4),
                "n_window": len(series),
                "percentile": round(100.0 * sum(1 for v in series if v <= cur) / len(series), 1),
                "rank": round(100.0 * (cur - lo) / (hi - lo), 1) if hi > lo else 50.0})
    out["available"] = len(series) >= max(20, lookback // 4)
    return out

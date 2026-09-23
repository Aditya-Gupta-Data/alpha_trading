"""Shared cohort metrics — small, pure, tested."""
from __future__ import annotations

import math


def per_trade_sharpe(rs: list) -> float | None:
    """mean(R) / stdev(R) over the cohort's realized R multiples (the
    critic's own per-trade Sharpe, not annualised). None below 2 trades or
    at zero dispersion."""
    xs = [float(x) for x in rs if x is not None]
    if len(xs) < 2:
        return None
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return round(mean / math.sqrt(var), 4) if var > 0 else None


def max_drawdown(rs: list) -> float | None:
    """Deepest peak-to-trough fall of the cumulative-R path, in R (≥ 0).
    None for an empty cohort."""
    xs = [float(x) for x in rs if x is not None]
    if not xs:
        return None
    peak = cum = 0.0
    worst = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return round(worst, 4)


def summarize(rs: list) -> dict:
    xs = [float(x) for x in rs if x is not None]
    return {"n": len(xs),
            "mean_r": round(sum(xs) / len(xs), 4) if xs else None,
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 4) if xs else None,
            "sharpe": per_trade_sharpe(xs), "max_drawdown_r": max_drawdown(xs)}

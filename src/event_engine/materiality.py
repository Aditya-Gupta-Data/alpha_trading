"""
Step 7, the materiality gate — pure arithmetic in our code, never the model's number.

effect_pct = revenue_effect_cr / annual_revenue_cr × 100, for low, base and high. The result is
tagged with the weakest input source (any LLM input → `estimate:llm`). It PASSES when:
  * a full range exists (ranges are mandatory — no suggestion from a single point)
  * the range does not straddle zero (low and high on the same side), and
  * |base| ≥ `materiality_threshold_pct` (default 2% of annual revenue).
A failing exposure is still recorded on the card — the engine learns which events mattered.
"""
from __future__ import annotations

from src.event_engine import TAG_COMPUTED, settings, worst_tag


def assess(exposure: dict, threshold_pct: float = None) -> dict:
    threshold = float(settings()["materiality_threshold_pct"] if threshold_pct is None else threshold_pct)
    out = {"ticker": exposure.get("ticker"), "threshold_pct": threshold, "passes_gate": False,
           "effect_pct": None, "source": TAG_COMPUTED, "reason": None}
    effect, revenue = exposure.get("revenue_effect_cr"), exposure.get("annual_revenue")
    if exposure.get("direction") == "none":
        out["reason"] = "not exposed"
        return out
    if not effect:
        out["reason"] = "no revenue-effect range"
        return out
    if not revenue or not revenue.get("value") or float(revenue["value"]) <= 0:
        out["reason"] = "no annual revenue to divide by"
        return out
    rev = float(revenue["value"])
    pct = [round(100.0 * float(x) / rev, 4) for x in effect]
    out["effect_pct"] = pct
    out["source"] = worst_tag(exposure.get("revenue_effect_source"), revenue.get("source"))
    lo, base, hi = pct
    if lo < 0 < hi:
        out["reason"] = f"range straddles zero ({lo:+.2f}% to {hi:+.2f}%)"
    elif abs(base) < threshold:
        out["reason"] = f"|{base:+.2f}%| of revenue is below the {threshold:g}% gate"
    else:
        out["passes_gate"] = True
    return out

"""
Step 7, the materiality gate — pure arithmetic in our code, never the model's number.

effect_pct = revenue_effect_cr / annual_revenue_cr × 100, for low, base and high. The result is
tagged with the weakest input source (any LLM input → `estimate:llm`). It PASSES when:
  * a full range exists (ranges are mandatory — no suggestion from a single point)
  * the range does not straddle zero (low and high on the same side), and
  * |base| ≥ `materiality_threshold_pct` (default 2% of annual revenue).
An INDEX has no revenue (2026-10-23): its effect is the model's own price-response range, judged by
the same three rules (`basis` "price_response", tagged with that range's source).
A failing exposure is still recorded on the card — the engine learns which events mattered.
"""
from __future__ import annotations

from src.event_engine import TAG_COMPUTED, settings, worst_tag


INDEX_NAMES = {"BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}


def is_index(ticker) -> bool:
    t = str(ticker or "").strip().upper().removesuffix(".NS")
    return t.startswith("NIFTY") or t in INDEX_NAMES


def _judge(out: dict, pct: list, threshold: float, unit: str) -> dict:
    lo, base, hi = pct
    if lo < 0 < hi:
        out["reason"] = f"range straddles zero ({lo:+.2f}% to {hi:+.2f}%)"
    elif abs(base) < threshold:
        out["reason"] = f"|{base:+.2f}%| of {unit} is below the {threshold:g}% gate"
    else:
        out["passes_gate"] = True
    return out


def assess(exposure: dict, threshold_pct: float = None) -> dict:
    threshold = float(settings()["materiality_threshold_pct"] if threshold_pct is None else threshold_pct)
    out = {"ticker": exposure.get("ticker"), "threshold_pct": threshold, "passes_gate": False,
           "effect_pct": None, "source": TAG_COMPUTED, "basis": "revenue", "reason": None}
    effect, revenue = exposure.get("revenue_effect_cr"), exposure.get("annual_revenue")
    if exposure.get("direction") == "none":
        out["reason"] = "not exposed"
        return out
    no_revenue = not revenue or not revenue.get("value")
    if is_index(exposure.get("ticker")) or (no_revenue and "index" in str(exposure.get("segment") or "").lower()):
        response = exposure.get("price_response_pct")
        out["basis"] = "price_response"
        if not response:
            out["reason"] = "index: no price-response range"
            return out
        out["effect_pct"] = [round(float(x), 4) for x in response]
        out["source"] = worst_tag(exposure.get("price_response_source"))
        return _judge(out, out["effect_pct"], threshold, "price response")
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
    return _judge(out, pct, threshold, "revenue")

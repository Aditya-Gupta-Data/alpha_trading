"""
Steps 2-6, node side: the mechanics and who is exposed — from the LLM, tagged by source.

Owner ruling (conflict b, 2026-10-21): the engine derives its own exposure estimates with the LLM
and may use them to pass the gates for PAPER_EVENTS. The code's job is honesty about provenance:

  * a mechanic is `reported` ONLY when the model quotes a sentence that the code then finds in the
    source text (whitespace/case-insensitive); otherwise it is downgraded to `estimate:llm`
  * every exposure number from the model is `estimate:llm`
  * a company's annual revenue comes from `config/event_company_financials.json` when present
    (`reported`, with that file's source string) and from the model otherwise (`estimate:llm`)
  * ranges are mandatory: an exposure without a low/base/high revenue effect is kept but cannot pass
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.event_engine import ROOT, TAG_LLM, TAG_REPORTED

FINANCIALS_PATH = ROOT / "config" / "event_company_financials.json"

EXPOSURE_PROMPT = (
    "You are an equity analyst for Indian listed companies. From the POLICY TEXT, extract the "
    "mechanics and the exposure of NSE-listed companies. Return ONLY JSON: "
    "{\"mechanics\": [{\"rule\": str, \"before\": number|null, \"after\": number|null, \"unit\": str, "
    "\"quote\": the exact sentence from the text that states it}], "
    "\"exposures\": [{\"ticker\": NSE symbol (e.g. \"M&M\", \"MARUTI\", \"NIFTY 50\"), \"segment\": str, "
    "\"direction\": \"positive\"|\"negative\"|\"none\", \"route\": str, "
    "\"revenue_effect_cr\": [low, base, high] in INR crore per year (signed), "
    "\"annual_revenue_cr\": number|null, \"price_response_pct\": [low, base, high] signed % move the "
    "stock should make if the market priced the mechanics fully, \"horizon_days\": int, "
    "\"reasoning\": short str}]}. Use ranges, never single points. Say \"none\" for companies the "
    "policy does not reach."
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _triple(v):
    """A clean [low, base, high] of floats sorted low→high, or None."""
    try:
        t = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    if len(t) != 3:
        return None
    lo, base, hi = sorted(t)
    return [lo, base, hi]


def load_financials(path=None) -> dict:
    try:
        d = json.loads(Path(path or FINANCIALS_PATH).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k).upper(): v for k, v in (d.get("companies") or {}).items() if isinstance(v, dict)}


def extract(item: dict, llm, financials: dict = None) -> dict:
    """{ok, reason, mechanics[], exposures[], flags[]}. Never raises."""
    text = item.get("text") or ""
    raw = llm.ask(EXPOSURE_PROMPT, f"POLICY TEXT:\n{text[:9000]}")
    if not isinstance(raw, dict):
        return {"ok": False, "reason": getattr(llm, "last_skip", None) or "exposure extraction unavailable",
                "mechanics": [], "exposures": [], "flags": []}
    fin = load_financials() if financials is None else financials
    flags, body = [], _norm(text)
    mechanics = []
    for m in raw.get("mechanics") or []:
        if not isinstance(m, dict):
            continue
        quote = str(m.get("quote") or "")
        found = bool(quote) and _norm(quote) in body
        if quote and not found:
            flags.append(f"quote not found in the text for mechanic {str(m.get('rule'))[:60]!r}: tagged estimate:llm")
        mechanics.append({"rule": str(m.get("rule") or "")[:200], "before": m.get("before"),
                          "after": m.get("after"), "unit": str(m.get("unit") or "")[:20],
                          "quote": quote[:400], "source": TAG_REPORTED if found else TAG_LLM})
    exposures = []
    for e in raw.get("exposures") or []:
        if not isinstance(e, dict) or not e.get("ticker"):
            continue
        ticker = str(e["ticker"]).strip().upper().removesuffix(".NS")
        direction = e.get("direction") if e.get("direction") in ("positive", "negative", "none") else "none"
        rev = fin.get(ticker)
        if rev and rev.get("annual_revenue_cr"):
            revenue = {"value": float(rev["annual_revenue_cr"]), "source": TAG_REPORTED,
                       "ref": str(rev.get("source") or "config/event_company_financials.json")}
        elif e.get("annual_revenue_cr"):
            try:
                revenue = {"value": float(e["annual_revenue_cr"]), "source": TAG_LLM, "ref": "model"}
            except (TypeError, ValueError):
                revenue = None
        else:
            revenue = None
        effect = _triple(e.get("revenue_effect_cr"))
        response = _triple(e.get("price_response_pct"))
        if effect is None and direction != "none":
            flags.append(f"{ticker}: no revenue-effect range — cannot pass the materiality gate")
        try:
            horizon = max(1, min(365, int(e.get("horizon_days") or 0))) or None
        except (TypeError, ValueError):
            horizon = None
        exposures.append({"ticker": ticker, "segment": str(e.get("segment") or "")[:120],
                          "direction": direction, "route": str(e.get("route") or "")[:60],
                          "revenue_effect_cr": effect, "revenue_effect_source": TAG_LLM if effect else None,
                          "annual_revenue": revenue,
                          "price_response_pct": response, "price_response_source": TAG_LLM if response else None,
                          "horizon_days": horizon, "reasoning": str(e.get("reasoning") or "")[:300]})
    return {"ok": True, "reason": None, "mechanics": mechanics, "exposures": exposures, "flags": flags}

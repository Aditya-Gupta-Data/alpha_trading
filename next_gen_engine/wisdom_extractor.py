"""
next_gen_engine/wisdom_extractor.py — qualitative text -> backtestable params
==============================================================================

Blueprint Phase 3 (owner, 2026-07-17). A pipeline that reads a qualitative
economic artifact — a macro memo, an earnings-call transcript, an analyst
thesis — and distils it into a STRICT, machine-usable trading frame the
rest of the engine can act on or backtest.

    raw text  ->  LLM (via the text_intelligence manager)  ->  validated
                  {target_sector, timeframe_days, fundamental_filters,
                   volatility_regime, direction, thesis, confidence}

ARCHITECTURE CONSTRAINT (owner, explicit): DO NOT spin up a new LLM client.
Every call goes through `src.text_intelligence.get_extractor()` — the
decision-#74 manager whose backend (ollama / claude) is a CONFIG choice.
This module only OWNS the prompt and the schema validation; the transport,
the budget cap, the incremental hash ledger and the fail-open network
posture all belong to that manager. The `extractor` arg is injectable
exactly like news_parser.parse_headline, so tests never touch a network.

Contract:
  * STRICT SCHEMA: the LLM's free-form JSON is coerced into a fixed frame.
    Unknown sectors, out-of-range horizons and bad enums are dropped to
    None/[] — a wisdom frame NEVER carries a value the backtester can't
    trust. `_valid` reports which fields survived.
  * FAIL-OPEN: no extractor reachable / junk output / empty text -> a
    honest abstain frame ({"ok": False, "reason": ...}), never an
    exception. Same posture as the whole ingestion layer.
  * PURE around the seam: given an injected extractor, extract_wisdom is
    deterministic and offline-testable.
  * CANONICAL MERGE TARGET: this is a NEW capability (no src/ equivalent).
    At deploy it lands as `src/ingestion/wisdom_extractor.py` beside
    news_parser, scheduled like the other text passes, writing frames to
    the data lake for the thematic-playbook layer to consume. It must
    stay a text_intelligence CLIENT, never grow its own model call.
"""
import json

# The controlled vocabularies the backtester understands. Kept in sync with
# config/sector_universe.json + src/regime.py at merge time (a single source
# is a merge-time TODO, deliberately duplicated here while this is staging).
KNOWN_SECTORS = {"IT", "FINANCIALS", "PHARMA", "AUTO", "FMCG", "METAL",
                 "ENERGY", "BATTERY_EV"}
VOL_REGIMES = {"low", "mid", "high", "crisis"}
DIRECTIONS = {"bullish", "bearish", "neutral"}

MIN_TIMEFRAME_DAYS = 1
MAX_TIMEFRAME_DAYS = 365

_SYSTEM_PROMPT = (
    "You are a systematic macro analyst. Read the economic text and extract "
    "ONLY what it actually supports into a JSON trading frame. Do not invent "
    "specifics the text does not imply — prefer null over a guess. Respond "
    "with a single JSON object, no prose, using EXACTLY these keys:\n"
    '  "target_sector": one of '
    "[IT, FINANCIALS, PHARMA, AUTO, FMCG, METAL, ENERGY, BATTERY_EV] or null\n"
    '  "direction": one of [bullish, bearish, neutral]\n'
    '  "timeframe_days": integer horizon the thesis plays out over, or null\n'
    '  "volatility_regime": one of [low, mid, high, crisis] or null\n'
    '  "fundamental_filters": array of short screen strings '
    '(e.g. "debt_to_equity<1", "revenue_growth>15%"), or []\n'
    '  "thesis": one sentence capturing the core claim\n'
    '  "confidence": float 0..1 for how strongly the text supports the frame'
)


def _coerce_frame(raw: dict) -> dict:
    """Validate the LLM's JSON into the fixed schema. Every field is
    independently sanitised; a bad field drops to its null/empty default
    rather than voiding the whole frame, and `_valid` records what
    survived so the caller can weight the frame honestly."""
    frame = {"target_sector": None, "direction": None, "timeframe_days": None,
             "volatility_regime": None, "fundamental_filters": [],
             "thesis": None, "confidence": None}
    valid = {}

    sector = str(raw.get("target_sector") or "").strip().upper()
    if sector in KNOWN_SECTORS:
        frame["target_sector"] = sector
    valid["target_sector"] = frame["target_sector"] is not None

    direction = str(raw.get("direction") or "").strip().lower()
    if direction in DIRECTIONS:
        frame["direction"] = direction
    valid["direction"] = frame["direction"] is not None

    tf = raw.get("timeframe_days")
    try:
        tf = int(tf)
        if MIN_TIMEFRAME_DAYS <= tf <= MAX_TIMEFRAME_DAYS:
            frame["timeframe_days"] = tf
    except (TypeError, ValueError):
        pass
    valid["timeframe_days"] = frame["timeframe_days"] is not None

    vol = str(raw.get("volatility_regime") or "").strip().lower()
    if vol in VOL_REGIMES:
        frame["volatility_regime"] = vol
    valid["volatility_regime"] = frame["volatility_regime"] is not None

    filters = raw.get("fundamental_filters")
    if isinstance(filters, list):
        frame["fundamental_filters"] = [
            str(f).strip() for f in filters if str(f).strip()][:10]
    valid["fundamental_filters"] = bool(frame["fundamental_filters"])

    thesis = raw.get("thesis")
    if isinstance(thesis, str) and thesis.strip():
        frame["thesis"] = thesis.strip()[:300]

    conf = raw.get("confidence")
    try:
        conf = float(conf)
        frame["confidence"] = round(min(1.0, max(0.0, conf)), 2)
    except (TypeError, ValueError):
        pass

    frame["_valid"] = valid
    # actionable = we know WHAT to trade and WHICH WAY (the minimum a
    # backtest needs); everything else refines it.
    frame["actionable"] = valid["target_sector"] and valid["direction"]
    return frame


def extract_wisdom(text: str, extractor=None, source: str = None) -> dict:
    """Distil one qualitative artifact into a validated wisdom frame.

    Returns {"ok": True, "frame": {...}, "source": ...} on success or
    {"ok": False, "reason": ...} on any abstain — never raises. `extractor`
    is injectable (a text_intelligence backend); the default resolves the
    configured manager lazily so importing this module costs nothing."""
    if not text or not str(text).strip():
        return {"ok": False, "reason": "empty text"}
    if extractor is None:
        from src.text_intelligence import get_extractor
        extractor = get_extractor()
    try:
        if not extractor.is_reachable():
            return {"ok": False, "reason": "extractor unreachable "
                    "(configured backend offline / no credits)"}
        raw = extractor.chat_json(_SYSTEM_PROMPT, str(text))
    except Exception as e:                       # transport belongs to the
        return {"ok": False, "reason": f"extractor error: {e}"}  # manager
    if not isinstance(raw, dict):
        return {"ok": False, "reason": "no parseable JSON from the model"}
    frame = _coerce_frame(raw)
    return {"ok": True, "frame": frame, "source": source}


if __name__ == "__main__":
    # Manual smoke test uses whatever backend config selects (ollama by
    # default). python3 -m next_gen_engine.wisdom_extractor
    demo = ("With crude sustainably above $95 and the rupee under pressure, "
            "upstream energy names should see multi-quarter margin expansion; "
            "we favour low-debt producers over the next two quarters.")
    print(json.dumps(extract_wisdom(demo, source="demo_memo"), indent=2))

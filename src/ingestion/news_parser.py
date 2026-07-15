"""
src/ingestion/news_parser.py — LLM semantic event parser (Phase 7)
==================================================================

Raw text (a headline, a social-sentiment blurb, a broker note line) -> ONE
strict five-key trading signal frame:

    {"target_entity":        "NIFTY 50" | "CRUDE" | "USDINR" | "HDFCBANK" | ...
     "event_classification": "macro_liquidity" | "geopolitical_shock" | ...
     "directional_bias":     -1.0 .. 1.0,
     "horizon_impact":       "SHORT" | "MEDIUM" | "LONG",
     "confidence_score":     0.0 .. 1.0}

Built on the Phase 10B LocalExtractor (src/local_parser.py), so all the
guardrails it carries hold here too: the ONLY network I/O is the local
Ollama endpoint, the module is a pure text-to-JSON transformer with zero
market-data capability, and every failure mode (Ollama not installed, not
running, answering garbage) returns None quietly — nothing raises,
nothing blocks, nothing is guessed.

Lenient in, strict out: the local model may answer with fences, word
biases ("bullish"), sloppy horizons ("short-term") or entity spellings
("Brent", "Bank Nifty", "TCS.NS") — the coercion layer normalizes all of
that, and the returned dict always has exactly the five schema keys with
in-range values, or is None.

Entity canonicalization is shared vocabulary with the macro tracker and
the resonance engine: indexes resolve through dhan_client's own alias
table to "NIFTY 50"/"NIFTY BANK", macro spellings collapse to the
MACRO_METRICS names, and stock symbols lose their ".NS" suffix.

Try it (needs a running local Ollama):
    python3 -m src.ingestion.news_parser "Crude spikes 4% as OPEC cuts supply"
"""

import json
import re

from src import brain_map
from src import dhan_client as dc
from src.ingestion.macro_tracker import MACRO_METRICS
from src.local_parser import LocalExtractor

ALLOWED_HORIZONS = ("SHORT", "MEDIUM", "LONG")

_HORIZON_SYNONYMS = {
    "SHORT": "SHORT", "SHORT_TERM": "SHORT", "ST": "SHORT",
    "NEAR": "SHORT", "NEAR_TERM": "SHORT", "IMMEDIATE": "SHORT",
    "DAYS": "SHORT", "INTRADAY": "SHORT",
    "MEDIUM": "MEDIUM", "MEDIUM_TERM": "MEDIUM", "MID": "MEDIUM",
    "MID_TERM": "MEDIUM", "MT": "MEDIUM", "WEEKS": "MEDIUM",
    "LONG": "LONG", "LONG_TERM": "LONG", "LT": "LONG",
    "STRUCTURAL": "LONG", "SECULAR": "LONG", "MONTHS": "LONG",
    "YEARS": "LONG",
}

# Spellings a model or a headline uses -> the canonical macro metric names
# the tracker and the resonance engine key on.
_MACRO_ENTITY_ALIASES = {
    "CRUDE": "CRUDE", "CRUDE OIL": "CRUDE", "CRUDEOIL": "CRUDE",
    "OIL": "CRUDE", "BRENT": "CRUDE", "WTI": "CRUDE",
    "GOLD": "GOLD_WORLD", "GOLD WORLD": "GOLD_WORLD",
    "GOLD SPOT": "GOLD_WORLD", "SPOT GOLD": "GOLD_WORLD",
    "GOLD_WORLD": "GOLD_WORLD", "XAU": "GOLD_WORLD", "XAUUSD": "GOLD_WORLD",
    "GOLD INDIA": "GOLD_INDIA", "GOLD_INDIA": "GOLD_INDIA",
    "MCX GOLD": "GOLD_INDIA", "GOLD MCX": "GOLD_INDIA",
    "USDINR": "USDINR", "USD INR": "USDINR", "USD/INR": "USDINR",
    "USD-INR": "USDINR", "RUPEE": "USDINR", "INR": "USDINR",
    "DOLLAR RUPEE": "USDINR",
}

# Word biases small models emit despite being asked for a number.
_BIAS_WORDS = {
    "very bullish": 0.9, "strongly bullish": 0.9, "bullish": 0.5,
    "mildly bullish": 0.25, "positive": 0.5,
    "neutral": 0.0, "mixed": 0.0, "unclear": 0.0,
    "mildly bearish": -0.25, "negative": -0.5, "bearish": -0.5,
    "strongly bearish": -0.9, "very bearish": -0.9,
}

_SIGNAL_PROMPT = (
    "You are a semantic event parser for an Indian-market trading system. "
    "Read the user's raw text (a news headline or market chatter) and "
    "return ONLY one JSON object — no prose, no markdown fences — with "
    "exactly these keys:\n"
    '{"target_entity": string, "event_classification": string, '
    '"directional_bias": number, "horizon_impact": string, '
    '"confidence_score": number}\n'
    "Rules:\n"
    "- target_entity: the single instrument or macro variable most "
    "directly moved — an index (NIFTY, BANKNIFTY), an NSE stock symbol "
    "(HDFCBANK), or a macro variable (CRUDE, GOLD, USDINR).\n"
    "- event_classification: a short snake_case category, e.g. "
    "macro_liquidity, geopolitical_shock, currency_depreciation, "
    "earnings_surprise, supply_shock, policy_change.\n"
    "- directional_bias: -1.0 (strongly bearish for the entity) to 1.0 "
    "(strongly bullish for the entity).\n"
    "- horizon_impact: SHORT (1-5 days), MEDIUM (weeks), or LONG "
    "(structural).\n"
    "- confidence_score: 0.0 to 1.0 — how clearly the text supports the "
    "reading.\n"
    "If the text contains no market-relevant event, use confidence_score "
    "0.0 and directional_bias 0.0."
)


def canonicalize_entity(text) -> str | None:
    """Any entity spelling -> the canonical name the rest of Phase 7 keys
    on: a macro metric ("CRUDE", "USDINR", "GOLD_WORLD", "GOLD_INDIA"),
    a dhan_client index name ("NIFTY 50", "NIFTY BANK", "INDIA VIX"), or
    a bare uppercase stock symbol ("HDFCBANK"). None for empty input."""
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(text).strip().upper())
    if not cleaned:
        return None
    if cleaned.endswith(".NS"):
        cleaned = cleaned[:-3]
    if cleaned in _MACRO_ENTITY_ALIASES:
        return _MACRO_ENTITY_ALIASES[cleaned]
    # Reuse dhan_client's own alias table so "NIFTY"/"^NSEI"/"BANKNIFTY"
    # resolve to the exact index names the journal and the impact matrix
    # use — one alias vocabulary across the whole engine.
    no_space = cleaned.replace(" ", "")
    for spelling in (cleaned, no_space):
        if spelling in dc._ALIASES:
            return dc._ALIASES[spelling]
    if cleaned in dc.SECURITY_ID_MAP:
        return cleaned[:-3] if cleaned.endswith(".NS") else cleaned
    if "BANK" in no_space and "NIFTY" in no_space:
        return "NIFTY BANK"
    return cleaned[:40]


def _coerce_bias(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _BIAS_WORDS.get(str(raw).strip().lower(), 0.0)
    return round(max(-1.0, min(1.0, value)), 3)


def _coerce_confidence(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5   # answered but unusable — mid confidence, documented
    if value > 1.0 and value <= 100.0:
        value /= 100.0   # models sometimes answer in percent
    return round(max(0.0, min(1.0, value)), 3)


def _coerce_horizon(raw) -> str:
    word = re.sub(r"[^A-Z]+", "_", str(raw or "").strip().upper()).strip("_")
    return _HORIZON_SYNONYMS.get(word, "SHORT")


def _coerce_signal(raw) -> dict | None:
    """LLM output (dict) -> the strict five-key frame, or None. Strict on
    output: exactly the schema keys, entity canonicalized, numbers
    clamped, horizon in ALLOWED_HORIZONS."""
    if not isinstance(raw, dict):
        return None
    entity = canonicalize_entity(raw.get("target_entity"))
    if not entity:
        return None   # a signal with no target can't be cross-referenced
    classification = brain_map._normalize_tag(
        raw.get("event_classification") or "") or "unclassified"
    return {
        "target_entity": entity,
        "event_classification": classification[:60],
        "directional_bias": _coerce_bias(raw.get("directional_bias")),
        "horizon_impact": _coerce_horizon(raw.get("horizon_impact")),
        "confidence_score": _coerce_confidence(raw.get("confidence_score")),
    }


def parse_headline(text, extractor=None) -> dict | None:
    """The Phase 7 entry point: raw text -> one validated signal frame, or
    None when the text is empty, the LLM is unavailable, or the answer
    can't be coerced into the schema. Never raises.

    The extractor now defaults to the Data Department's text-intelligence
    MANAGER (decision #74) — a config choice between the local Ollama
    backend and the cloud Claude backend the VM can reach. An injected
    extractor (tests, an explicit LocalExtractor) is used as-is, so this
    is byte-identical to the old direct-Ollama path when config says
    "ollama" or a backend is passed."""
    if not text or not str(text).strip():
        return None
    if extractor is None:
        from src.text_intelligence import get_extractor
        extractor = get_extractor()
    raw = extractor.chat_json(_SIGNAL_PROMPT, str(text))
    return _coerce_signal(raw)


def parse_many(texts, extractor=None) -> list:
    """Batch convenience: a list of texts -> the list of frames that
    parsed (failures silently dropped — same contract per item)."""
    if extractor is None:
        from src.text_intelligence import get_extractor
        extractor = get_extractor()
    frames = []
    for text in texts or []:
        frame = parse_headline(text, extractor=extractor)
        if frame is not None:
            frames.append(frame)
    return frames


if __name__ == "__main__":
    import sys
    sample = " ".join(sys.argv[1:]) or (
        "Rupee slides to record low as crude extends rally on supply fears")
    ex = LocalExtractor()
    if not ex.is_reachable():
        print(f"Ollama not reachable at {ex.base_url} — install/start it "
              "first (see src/local_parser.py's docstring).")
        sys.exit(1)
    print(f"Parsing via {ex.model} at {ex.base_url}:\n  input: {sample}")
    print(f"  frame: {json.dumps(parse_headline(sample, extractor=ex))}")

"""
Alpha Trading -- Phase 6 core loop: the post-mortem analyst
============================================================

When the plan tracker resolves a paper trade (stop hit / target hit /
time stop), this module asks Google Gemini to compare the ORIGINAL PLAN
(the thesis, trigger and levels we journaled at entry) with the ACTUAL
EXECUTION (what prices really did) and write a short, structured
post-mortem:

    {
      "variance_analysis":    what we planned vs. what actually happened,
      "unexpected_variables": macro shifts / volatility / volume anomalies
                              the plan didn't anticipate,
      "future_guardrails":    what to watch out for next time this pattern
                              archetype fires
    }

plan_tracker.py stores that JSON on the trade's `outcomes` row in the
Brain Map (data/brain_map.db, keyed by the journal short_id), so future
pattern queries can read not just the numbers but the lesson.

Like news_processor.py, this is a DELIBERATELY DETACHED utility: it
imports no core trading code, reads only its own .env, and NEVER raises
to its caller -- any failure (no GEMINI_API_KEY, network, quota,
unparseable reply) returns None, and a missing post-mortem is a normal,
expected state. Trade resolution must never block on an LLM.
"""

import json
import os
import ssl
import urllib.request
from pathlib import Path

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Same "-latest" alias reasoning as news_processor.py: pinned Gemini model
# names get deprecated and start 404ing; the alias tracks Google's current
# cheap flash-tier model.
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
HTTP_TIMEOUT = 20  # seconds

_FIELDS = ("variance_analysis", "unexpected_variables", "future_guardrails")

_POST_MORTEM_PROMPT = (
    "You are ADiTrader's post-mortem analyst on an Indian (NSE) equities "
    "desk. Below are a swing trade's ORIGINAL PLAN (the thesis and levels "
    "set at entry) and its ACTUAL EXECUTION (what the market really did). "
    "Write a short, honest post-mortem.\n\n"
    "Return ONLY a JSON object with exactly these fields:\n"
    '  "variance_analysis": 1-3 sentences comparing what we planned vs. '
    "what actually happened (thesis, exit trigger, expected vs. realized "
    "R-multiple).\n"
    '  "unexpected_variables": 1-3 sentences naming anything the plan did '
    "not anticipate (macro shifts, sudden volatility, volume anomalies, "
    'gaps); say "none observed" if nothing stands out.\n'
    '  "future_guardrails": 1-3 actionable sentences on what to watch out '
    "for next time this specific pattern archetype fires.\n"
    "No commentary outside the JSON.\n\n"
)


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _call_gemini(prompt: str, api_key: str) -> dict:
    """Single call returning parsed JSON. Raises on any failure so the
    caller can degrade to no-post-mortem."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
        body = json.loads(resp.read())
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def _coerce_post_mortem(raw) -> dict | None:
    """Coerce a model reply into the strict 3-field schema, or None when
    there's nothing usable in it."""
    if not isinstance(raw, dict):
        return None
    out = {f: " ".join(str(raw.get(f) or "").split()) for f in _FIELDS}
    if not any(out.values()):
        return None
    return {f: out[f] or "n/a" for f in _FIELDS}


def generate_post_mortem(initial_plan: dict, actual_execution: dict) -> dict | None:
    """The public entry point: plan + reality in, the 3-field post-mortem
    dict out -- or None on ANY failure. Never raises."""
    try:
        _load_env()
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Post-mortem analyst: GEMINI_API_KEY not set — skipping post-mortem.")
            return None
        user_text = (
            "ORIGINAL PLAN:\n" + json.dumps(initial_plan, indent=2) +
            "\n\nACTUAL EXECUTION:\n" + json.dumps(actual_execution, indent=2)
        )
        raw = _call_gemini(_POST_MORTEM_PROMPT + user_text, api_key)
        return _coerce_post_mortem(raw)
    except Exception as e:
        print(f"Post-mortem analyst: Gemini call failed ({e}) — skipping post-mortem.")
        return None

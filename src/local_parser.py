"""
Alpha Trading — Phase 10B: the local LLM Episodic Event Frame extractor
=======================================================================

Turns unstructured text (a news headline, a Discord message, a journal
snippet) into ONE strict JSON event frame and records it in the Brain Map
(data/brain_map.db `events` table) — using a LOCAL Ollama model so this
"light work" burns zero Gemini tokens and works fully offline.

STRICT GUARDRAILS (DECISIONS.md #30):
  * This module is a pure text-to-JSON transformer. It has ZERO
    dependencies on market data: no dhan_client, no price polling, no
    rules evaluation. An LLM is never used for market monitoring — that
    stays pure Python in src/rules.py.
  * The ONLY network I/O allowed is to the local Ollama endpoint
    (OLLAMA_BASE_URL, default http://localhost:11434/v1).
  * Brain Map writes go through src/brain_map.py's existing idempotent
    helpers — brain_map itself stays standalone and network-free (the
    integration lives HERE, same pattern as the episode snapshot call).

Fail-safe like every other optional dependency in this project: Ollama
not installed / not running / returning garbage means extract() returns
None with a printed note — nothing raises, nothing blocks.

Setup (one-time, on the Mac):
  1. Install Ollama: https://ollama.com/download  (or `brew install ollama`)
  2. Pull a small model:  ollama pull llama3
  3. Optional .env overrides:
       OLLAMA_BASE_URL="http://localhost:11434/v1"   (this is the default)
       OLLAMA_MODEL="llama3"                          (this is the default)

Try it from the project folder (needs Ollama running):

    python3 -m src.local_parser "Reliance wins arbitration case over gas dues"
"""

import json
import os
import re
from datetime import date
from pathlib import Path

from src import brain_map

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_env()

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "llama3"
REQUEST_TIMEOUT_SECONDS = 60  # local models on a laptop can be slow

# Event types the extractor is allowed to emit; anything else coerces to
# "news". Keeps the events table's vocabulary small and clusterable.
ALLOWED_EVENT_TYPES = {"news", "earnings", "macro", "corporate_action",
                       "chat", "journal"}

_SYSTEM_PROMPT = (
    "You are an Episodic Event Frame extractor for a trading journal. "
    "Read the user's raw text and return ONLY one JSON object — no prose, "
    "no markdown fences, no explanations — with exactly these keys:\n"
    '{"event_type": string, "tag": string, "sentiment": integer, '
    '"entities": [string, ...]}\n'
    "Rules:\n"
    "- event_type: one of news, earnings, macro, corporate_action, chat, journal.\n"
    "- tag: a short snake_case pattern key naming WHAT HAPPENED "
    "(e.g. earnings_beat, block_deal, rate_hike, arbitration_win).\n"
    "- sentiment: -1 (bearish), 0 (neutral), or 1 (bullish) for the "
    "instruments involved.\n"
    "- entities: the proper nouns involved — companies, tickers, people, "
    "institutions. Empty list if none.\n"
    "If the text contains no market-relevant event, use event_type "
    '"chat", tag "no_event", sentiment 0.'
)

# Phase 6D — causal triple extraction for the knowledge graph. Predicate is
# constrained to a small closed vocabulary so the graph stays queryable.
CAUSAL_PREDICATES = {"RESULTS_IN", "PRECEDES", "INDICATES", "CONTRADICTS"}

_CAUSAL_PROMPT = (
    "You are a causal-analysis engine for a trading journal. The user gives "
    "you a summary of REVIEWED trade outcomes and their post-mortems. Extract "
    "the causal links they support as (Subject -> Predicate -> Object) "
    "triples. The Predicate MUST be EXACTLY one of: RESULTS_IN, PRECEDES, "
    "INDICATES, CONTRADICTS.\n"
    "Example: 'Iron Condor' RESULTS_IN 'Loss' when 'VIX > 20'.\n"
    "Return ONLY a JSON object — no prose, no markdown fences — of this exact "
    "shape:\n"
    '{"triples": [{"subject": string, "predicate": string, "object": string, '
    '"condition": string|null}]}\n'
    "Rules:\n"
    "- subject / object: short concept names — a strategy (Iron Condor), a "
    "regime, a market condition, or an outcome (Loss / Win).\n"
    "- predicate: EXACTLY one of the four above, uppercase.\n"
    "- condition: the qualifying context if the link only holds under it "
    "(e.g. 'VIX > 20'), else null.\n"
    "- Only extract links genuinely supported by the outcomes given. Invent "
    'nothing. If none, return {"triples": []}.'
)


class LocalExtractor:
    """OpenAI-compatible client for a local Ollama server. Pure
    text-to-JSON — carries no market-data capability whatsoever."""

    def __init__(self, base_url: str = None, model: str = None,
                 timeout: float = REQUEST_TIMEOUT_SECONDS):
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    def is_reachable(self) -> bool:
        """True if the local Ollama server answers at all. Never raises."""
        try:
            import httpx
            resp = httpx.get(f"{self.base_url}/models", timeout=3)
            return resp.status_code < 500
        except Exception:
            return False

    def _chat(self, text_payload: str, system_prompt: str = None) -> str:
        """One chat-completions call; returns the raw content string, or
        None on any failure (server down, HTTP error, unexpected shape)."""
        try:
            import httpx
        except ImportError as e:
            print(f"  (local parser skipped: httpx not installed: {e})")
            return None
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt or _SYSTEM_PROMPT},
                {"role": "user", "content": str(text_payload)},
            ],
            # Recent Ollama versions honor this on the OpenAI-compat
            # endpoint; older ones ignore it (the fence-stripping below
            # covers those).
            "response_format": {"type": "json_object"},
        }
        try:
            resp = httpx.post(f"{self.base_url}/chat/completions",
                              json=payload, timeout=self.timeout)
            if resp.status_code >= 300:
                print(f"  (local parser: Ollama HTTP {resp.status_code})")
                return None
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  (local parser: Ollama call failed: {e})")
            return None

    def extract_event_json(self, text_payload: str) -> dict:
        """Raw text -> validated Episodic Event Frame
        {"event_type": str, "tag": str, "sentiment": int, "entities": list}
        or None when Ollama is unavailable / returns something unusable.
        Never raises."""
        if not text_payload or not str(text_payload).strip():
            return None
        content = self._chat(text_payload)
        if content is None:
            return None
        return _coerce_event(content)

    def chat_json(self, system_prompt: str, user_text: str):
        """Generic fail-safe JSON call for the other Phase 10B jobs (e.g.
        the sleep-phase consolidator): any system prompt, same local-only
        endpoint and error handling, returns parsed JSON (dict/list) or
        None. Never raises."""
        content = self._chat(user_text, system_prompt=system_prompt)
        if content is None:
            return None
        try:
            return json.loads(_strip_fences(content))
        except (ValueError, TypeError):
            print("  (local parser: model did not return valid JSON)")
            return None

    def extract_causal_triples(self, summarized_text: str) -> list:
        """Phase 6D: a summary of REVIEWED trade outcomes (+ post-mortems) ->
        a list of validated causal triples for the knowledge graph:

            [{"subject": str, "predicate": str, "object": str,
              "condition": str-or-None}, ...]

        `predicate` is constrained to CAUSAL_PREDICATES; subject/object are
        normalized to the same snake_case tags the Brain Map clusters on, so
        an edge like 'Iron Condor' -> 'Loss' becomes iron_condor -> loss and
        matches the strategy/regime nodes the proposer queries. Returns []
        when Ollama is unavailable or returns nothing usable. Never raises."""
        if not summarized_text or not str(summarized_text).strip():
            return []
        raw = self.chat_json(_CAUSAL_PROMPT, str(summarized_text))
        return _coerce_triples(raw)


def _strip_fences(content: str) -> str:
    """Small local models love ```json fences despite instructions."""
    content = content.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
    if match:
        return match.group(1)
    return content


def _coerce_event(content) -> dict:
    """LLM output (string or dict) -> the strict event frame, or None.
    Lenient on input (fences, string sentiments, stray types), strict on
    output: the returned dict always has exactly the four schema keys with
    the right types, tag normalized the way the Brain Map clusters."""
    if isinstance(content, str):
        try:
            content = json.loads(_strip_fences(content))
        except (ValueError, TypeError):
            print("  (local parser: model did not return valid JSON)")
            return None
    if not isinstance(content, dict):
        return None

    event_type = str(content.get("event_type") or "").strip().lower()
    if event_type not in ALLOWED_EVENT_TYPES:
        event_type = "news"

    tag = brain_map._normalize_tag(content.get("tag") or "")
    if not tag:
        return None  # an event with no pattern key can't be clustered

    raw_sent = content.get("sentiment", 0)
    try:
        sentiment = int(raw_sent)
    except (ValueError, TypeError):
        word = str(raw_sent).strip().lower()
        sentiment = {"bullish": 1, "positive": 1,
                     "bearish": -1, "negative": -1}.get(word, 0)
    sentiment = max(-1, min(1, sentiment))

    raw_entities = content.get("entities") or []
    if not isinstance(raw_entities, list):
        raw_entities = [raw_entities]
    entities = [str(e).strip()[:80] for e in raw_entities if str(e).strip()][:20]

    return {"event_type": event_type, "tag": tag,
            "sentiment": sentiment, "entities": entities}


def _coerce_triples(raw) -> list:
    """LLM causal output -> a clean list of validated triples. Lenient on
    input (drops non-dict items, junk predicates, empty subject/object),
    strict on output: predicate is upper-cased and must be in
    CAUSAL_PREDICATES, subject/object are normalized tags, condition is a
    trimmed string or None."""
    if not isinstance(raw, dict):
        return []
    triples = raw.get("triples")
    if not isinstance(triples, list):
        return []
    out = []
    for t in triples:
        if not isinstance(t, dict):
            continue
        predicate = str(t.get("predicate") or "").strip().upper()
        if predicate not in CAUSAL_PREDICATES:
            continue
        subject = brain_map._normalize_tag(t.get("subject") or "")
        obj = brain_map._normalize_tag(t.get("object") or "")
        if not subject or not obj:
            continue
        raw_cond = t.get("condition")
        condition = (str(raw_cond).strip()[:120]
                     if raw_cond and str(raw_cond).strip() else None)
        out.append({"subject": subject, "predicate": predicate,
                    "object": obj, "condition": condition})
    return out


def process_unstructured_input(conn, text: str, ticker: str = "MARKET",
                               event_date: str = None,
                               extractor: LocalExtractor = None) -> int:
    """The Phase 10B pipeline: raw text -> local LLM event frame -> one
    idempotent row in the Brain Map `events` table. Returns the event id,
    or None when extraction failed (Ollama down, unusable output).

    `conn` is an open brain_map connection (pass ':memory:' in tests);
    `ticker` scopes the event to an instrument, defaulting to the
    market-wide bucket; dedupe follows brain_map's usual
    (date, ticker, event_type, tag, source) key so re-parsing the same
    day's text never double-inserts."""
    extractor = extractor or LocalExtractor()
    frame = extractor.extract_event_json(text)
    if frame is None:
        return None
    sentiment_text = {1: "positive", -1: "negative"}.get(frame["sentiment"], "neutral")
    return brain_map._get_or_create_event(
        conn,
        date=event_date or date.today().isoformat(),
        ticker=ticker,
        event_type=frame["event_type"],
        tag=frame["tag"],
        sentiment=sentiment_text,
        entities={"entities": frame["entities"],
                  "sentiment_int": frame["sentiment"],
                  "raw_text": str(text)[:500]},
        source="local_parser",
    )


if __name__ == "__main__":
    import sys
    sample = " ".join(sys.argv[1:]) or "TCS beats Q1 earnings estimates on strong deal wins"
    ex = LocalExtractor()
    if not ex.is_reachable():
        print(f"Ollama not reachable at {ex.base_url} — install/start it first "
              "(see this file's docstring).")
        sys.exit(1)
    print(f"Extracting via {ex.model} at {ex.base_url}:\n  input: {sample}")
    print(f"  frame: {ex.extract_event_json(sample)}")

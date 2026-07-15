"""
src/text_intelligence.py — the Data Department's text→structured-JSON manager
=============================================================================

Decision #74. The one seam every "turn raw text into a JSON signal frame"
caller goes through, so the BACKEND is a config choice, not a code change.
Born from the 2026-07-15 news-parser reality check: the Phase-7 news parser
was hard-wired to Ollama (`LocalExtractor`), which the VM deliberately does
not run — so it could never execute in production. This manager lets the
same parser run on a cloud LLM the VM CAN reach.

THE CONTRACT — every backend exposes the LocalExtractor-shaped surface, so
callers (news_parser.parse_headline) are byte-identical whichever backend
is chosen:
    .chat_json(system: str, user: str) -> dict | None   # lenient JSON out
    .is_reachable() -> bool
    .base_url / .model                                  # for diagnostics

Backends (config `text_intelligence_backend`):
    "ollama"  -> src.local_parser.LocalExtractor        (DEFAULT — unchanged;
                 local only, the Mac's path, byte-identical to before)
    "claude"  -> ClaudeExtractor (this file)            (raw httpx to the
                 Anthropic Messages API — the cloud path the VM can reach)

WHY raw httpx and not the `anthropic` SDK: the codebase already calls Gemini
(`news_processor`) and Ollama (`local_parser`) over raw httpx and keeps the
backend dependency-light — this matches that convention (httpx is already a
dep; no new SDK).

⚠️ BILLING REALITY (flagged 2026-07-15): the Claude API is billed on API
credits (console.anthropic.com), SEPARATE from any Claude Max/Pro
SUBSCRIPTION — a subscription does not fund these programmatic calls.
Therefore the default backend stays "ollama" (zero surprise cost); flip
config to "claude" only after confirming API credits are loaded. Two guards
make the cloud path safe regardless:
  * BUDGET CAP — `text_intelligence_daily_call_cap` (default 200) counted in
    logs/text_intel_calls.jsonl; once spent, further single calls defer to
    the next day (fail-safe, never an exception).
  * INCREMENTAL — `already_processed`/`mark_processed` hash each text so a
    daily cron re-parses only NEW items, never the whole history (the
    "lightweight incremental" Phase-2 requirement).

TWO MODES:
  * SINGLE (daily/incremental)  — `get_extractor().chat_json(...)`, one call.
  * BATCH  (Phase-1 bulk history) — `submit_batch`/`poll_batch`/
    `fetch_batch_results` drive Anthropic's Message Batches API (50% cheaper,
    async, up to 100k requests) for a one-off historical backfill.

Model default `claude-opus-4-8` (config `text_intelligence_model`); set
`claude-haiku-4-5` for cheap high-volume extraction. Every network seam is
injectable (`post_fn`/`get_fn`) so tests never touch the API. Fail-open
everywhere: an absent key, a dead network, or junk output returns None.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.json"
CALL_LEDGER = ROOT / "logs" / "text_intel_calls.jsonl"
PROCESSED_LEDGER = ROOT / "logs" / "text_intel_processed.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_BATCH_URL = "https://api.anthropic.com/v1/messages/batches"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_BACKEND = "ollama"
DEFAULT_DAILY_CALL_CAP = 200
HTTP_TIMEOUT = 30


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _env(key: str) -> str | None:
    """Read one key from the process env, falling back to a direct .env
    scan (the same self-contained pattern notifier/news_processor use — no
    dotenv dependency)."""
    import os
    val = os.environ.get(key)
    if val:
        return val
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _extract_json(text: str) -> dict | None:
    """Lenient text -> dict: strip markdown fences, take the first {...}
    span, json.loads it. None on anything unparseable (same tolerance the
    LocalExtractor grants a local model)."""
    if not text:
        return None
    s = str(text).strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        out = json.loads(s[start:end + 1])
        return out if isinstance(out, dict) else None
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------- Claude backend

class ClaudeExtractor:
    """LocalExtractor-shaped Anthropic Messages API client (raw httpx).
    `chat_json` is the generic contract; `submit_batch`/`poll_batch`/
    `fetch_batch_results` add the bulk path. `post_fn`/`get_fn` are the
    injectable network seams (tests pass fakes; production uses httpx)."""

    def __init__(self, api_key: str = None, model: str = None,
                 post_fn=None, get_fn=None):
        self.api_key = api_key if api_key is not None else _env("ANTHROPIC_API_KEY")
        self.model = model or _config().get("text_intelligence_model", DEFAULT_MODEL)
        self.base_url = ANTHROPIC_URL
        self._post_fn = post_fn
        self._get_fn = get_fn

    # --- network seams (lazy httpx; overridable in tests) ---------------
    def _post(self, url: str, body: dict) -> dict | None:
        if self._post_fn is not None:
            return self._post_fn(url, body)
        try:
            import httpx
            resp = httpx.post(url, headers=self._headers(),
                              json=body, timeout=HTTP_TIMEOUT)
            if resp.status_code >= 300:
                print(f"  (text_intelligence: HTTP {resp.status_code})")
                return None
            return resp.json()
        except Exception as e:  # ImportError / network / decode
            print(f"  (text_intelligence: request failed: {e})")
            return None

    def _get(self, url: str) -> dict | None:
        if self._get_fn is not None:
            return self._get_fn(url)
        try:
            import httpx
            resp = httpx.get(url, headers=self._headers(), timeout=HTTP_TIMEOUT)
            if resp.status_code >= 300:
                return None
            return resp.json()
        except Exception as e:
            print(f"  (text_intelligence: GET failed: {e})")
            return None

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key or "",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json"}

    def _params(self, system: str, user: str) -> dict:
        # No temperature/top_p/thinking — removed on Opus 4.8/4.7 (400) and
        # unwanted for a deterministic extraction anyway. JSON is coaxed by
        # the prompt + lenient parse (chat_json stays schema-agnostic).
        return {"model": self.model, "max_tokens": 1024,
                "system": (system or "") + " Return ONLY one JSON object — "
                          "no prose, no markdown fences.",
                "messages": [{"role": "user", "content": str(user)}]}

    @staticmethod
    def _text_of(message: dict) -> str | None:
        for block in (message or {}).get("content") or []:
            if block.get("type") == "text":
                return block.get("text")
        return None

    # --- the LocalExtractor contract ------------------------------------
    def is_reachable(self) -> bool:
        """A key is present. (A real ping would cost a call; the request
        path already fails open, so key-presence is the honest cheap check
        callers gate on — mirrors LocalExtractor.is_reachable's intent.)"""
        return bool(self.api_key)

    def chat_json(self, system: str, user: str) -> dict | None:
        if not self.api_key or not user or not str(user).strip():
            return None
        data = self._post(self.base_url, self._params(system, user))
        if not isinstance(data, dict):
            return None
        if data.get("stop_reason") == "refusal":  # safety decline — no content
            return None
        return _extract_json(self._text_of(data))

    # --- BATCH (Phase-1 bulk historical) --------------------------------
    def submit_batch(self, items: list) -> str | None:
        """items = [(custom_id, system, user), ...] -> batch id, or None.
        One request per item; results keyed by custom_id (never order)."""
        requests = [{"custom_id": str(cid),
                     "params": self._params(system, user)}
                    for cid, system, user in (items or [])]
        if not requests:
            return None
        data = self._post(ANTHROPIC_BATCH_URL, {"requests": requests})
        return data.get("id") if isinstance(data, dict) else None

    def poll_batch(self, batch_id: str) -> dict:
        """{"status", "results_url", "counts"} — status "ended" means the
        results are ready. Fail-open to an in-progress-looking shape."""
        data = self._get(f"{ANTHROPIC_BATCH_URL}/{batch_id}")
        if not isinstance(data, dict):
            return {"status": "unknown", "results_url": None, "counts": {}}
        return {"status": data.get("processing_status", "unknown"),
                "results_url": data.get("results_url"),
                "counts": data.get("request_counts") or {}}

    def fetch_batch_results(self, results_url: str) -> dict:
        """results JSONL -> {custom_id: dict_or_None}. Succeeded rows parse
        their message text; errored/expired/canceled rows map to None."""
        out = {}
        raw = self._get(results_url)
        # httpx results endpoint returns JSONL text; the injectable seam may
        # hand back a pre-parsed list. Normalize both.
        lines = (raw if isinstance(raw, list)
                 else str(raw or "").splitlines())
        for line in lines:
            rec = line if isinstance(line, dict) else _json_line(line)
            if not isinstance(rec, dict):
                continue
            cid = str(rec.get("custom_id"))
            result = rec.get("result") or {}
            if result.get("type") == "succeeded":
                out[cid] = _extract_json(self._text_of(result.get("message")))
            else:
                out[cid] = None
        return out


def _json_line(line) -> dict | None:
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------- backend selection

def get_extractor(backend: str = None, config: dict = None, **kwargs):
    """The manager's front door: return a LocalExtractor-shaped backend by
    config (`text_intelligence_backend`, default "ollama" — byte-identical
    to before). Unknown names fall back to ollama, loudly."""
    cfg = config if config is not None else _config()
    name = (backend or cfg.get("text_intelligence_backend", DEFAULT_BACKEND)).lower()
    if name == "claude":
        return ClaudeExtractor(model=cfg.get("text_intelligence_model"), **kwargs)
    if name != "ollama":
        print(f"  (text_intelligence: unknown backend '{name}', using ollama)")
    from src.local_parser import LocalExtractor
    return LocalExtractor()


# ------------------------------------------------- budget + incremental

def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def calls_today(day: str = None, ledger: Path = None) -> int:
    day = day or _today_ist()
    ledger = ledger or CALL_LEDGER
    try:
        n = 0
        for line in ledger.read_text().splitlines():
            rec = _json_line(line)
            if rec and str(rec.get("ts", "")).startswith(day):
                n += 1
        return n
    except OSError:
        return 0


def within_daily_budget(cap: int = None, day: str = None,
                        ledger: Path = None, config: dict = None) -> bool:
    cfg = config if config is not None else _config()
    cap = cap if cap is not None else int(
        cfg.get("text_intelligence_daily_call_cap", DEFAULT_DAILY_CALL_CAP))
    return calls_today(day, ledger) < cap


def record_call(day: str = None, ledger: Path = None) -> None:
    ledger = ledger or CALL_LEDGER
    try:
        ledger.parent.mkdir(exist_ok=True)
        with open(ledger, "a") as f:
            f.write(json.dumps({"ts": (day or _today_ist())
                                + "T" + datetime.now(IST).time().isoformat(
                                    timespec="seconds")}) + "\n")
    except OSError:
        pass


def _hash(text: str) -> str:
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()[:16]


def already_processed(text: str, ledger: Path = None) -> bool:
    ledger = ledger or PROCESSED_LEDGER
    h = _hash(text)
    try:
        for line in ledger.read_text().splitlines():
            if (_json_line(line) or {}).get("h") == h:
                return True
    except OSError:
        pass
    return False


def mark_processed(text: str, ledger: Path = None) -> None:
    ledger = ledger or PROCESSED_LEDGER
    try:
        ledger.parent.mkdir(exist_ok=True)
        with open(ledger, "a") as f:
            f.write(json.dumps({"h": _hash(text), "at": _today_ist()}) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    import sys
    text = " ".join(sys.argv[1:]) or "Crude spikes 4% as OPEC cuts supply"
    ex = get_extractor()
    print(f"backend: {type(ex).__name__} @ {getattr(ex, 'base_url', '?')} "
          f"(reachable: {ex.is_reachable()})")
    from src.ingestion.news_parser import parse_headline
    print("frame:", json.dumps(parse_headline(text, extractor=ex)))

"""
The event engine's one LLM door (node side). It goes through `src.text_intelligence.get_extractor`,
whose default on the Mini PC is the FreeLLMAPI router (#148) — the free API keys, never a local model.

Free tiers are the real resource limit, so: one call per (prompt, text) pair, cached on disk by
content hash; a daily cap (`event_engine.llm_daily_call_cap`, 100) and a per-run share
(`llm_run_call_cap`, 50: the 08:40 run cannot starve the 21:40 one); when a cap is spent or the router
is down, the caller gets None and the pipeline abstains with a named reason. Only public news text
is ever sent — never positions, the book or P&L.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.event_engine import LOGS_DIR, STATE_DIR, settings, today_ist


class EventLLM:
    def __init__(self, extractor=None, cache_dir=None, ledger=None, cap: int = None, run_cap: int = None):
        self._extractor = extractor
        self.cache_dir = Path(cache_dir or STATE_DIR / "llm_cache")
        self.ledger = Path(ledger or LOGS_DIR / "event_engine_llm_calls.jsonl")
        self.cap = int(settings().get("llm_daily_call_cap", 100) if cap is None else cap)
        self.run_cap = int(settings().get("llm_run_call_cap", 50) if run_cap is None else run_cap)
        self.calls_run = 0
        self.last_skip = None

    def _client(self):
        if self._extractor is None:
            from src.text_intelligence import get_extractor
            self._extractor = get_extractor()
        return self._extractor

    def calls_today(self) -> int:
        day = today_ist().isoformat()
        try:
            return sum(1 for ln in self.ledger.read_text().splitlines() if f'"{day}"' in ln)
        except OSError:
            return 0

    def ask(self, system: str, user: str):
        """Parsed JSON (dict/list) or None. Never raises."""
        key = hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:24]
        cached = self.cache_dir / f"{key}.json"
        try:
            return json.loads(cached.read_text())
        except (OSError, ValueError):
            pass
        if self.calls_run >= self.run_cap:
            self.last_skip = f"LLM run cap {self.run_cap} spent (the next run has its own share)"
            return None
        if self.calls_today() >= self.cap:
            self.last_skip = f"LLM daily cap {self.cap} spent"
            return None
        try:
            out = self._client().chat_json(system, user)
        except Exception as exc:                           # a broken router never breaks the run
            self.last_skip = f"LLM call failed: {type(exc).__name__}: {str(exc)[:120]}"
            return None
        self.calls_run += 1
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger, "a") as f:
                f.write(json.dumps({"day": today_ist().isoformat(), "key": key}) + "\n")
        except OSError:
            pass
        if out is None:
            self.last_skip = "LLM returned no usable JSON"
            return None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(out))
        except OSError:
            pass
        return out

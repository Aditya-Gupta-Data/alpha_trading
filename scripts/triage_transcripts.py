#!/usr/bin/env python3
# MANUAL OFFLINE TOOL
"""
scripts/triage_transcripts.py — Semantic friction counter (AltData Pillar 4, Mini PC)
====================================================================================

Reads earnings-call transcripts from `data/lake/transcripts/`, asks the FreeLLMAPI
router (`src.local_parser.LocalExtractor`, the one LLM door) to count five fixed
friction concepts, and merges one row per transcript into
`data/lake/alt_data/semantic_friction.csv` (key: ticker, date, quarter).

Counts are evidence-backed: the model must quote the sentence behind every hit, and
a hit counts only if its quote is found verbatim in the transcript. Its own integer is
kept as `llm_claimed_total` for comparison. Non-JSON or off-schema replies are retried
per chunk; a transcript with any chunk that never parses is skipped (no partial row).

Input files:
  * `<TICKER>_<YYYY-MM-DD>_<QUARTER>.txt` (e.g. `TCS_2026-10-09_Q2FY27.txt`), or
  * `.csv` with columns `ticker, date, quarter, text` (rows sharing a key are joined).

Runs on the Mini PC, where the router listens on localhost:3001. Transcript text leaves
the host for the router's free-tier providers (#148). Capture-only: nothing on the
trade path reads the output.

    python3 scripts/triage_transcripts.py [--dir data/lake/transcripts] [--dry-run] [--force]
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingestion.alt_data.base_scraper import BaseAltDataScraper, now_ist_iso  # noqa: E402

TRANSCRIPTS_DIR = ROOT / "data" / "lake" / "transcripts"
PROMPT_VERSION = "friction-v1"
CHUNK_CHARS = 12_000
MAX_ATTEMPTS = 3
MAX_QUOTES_PER_CONCEPT = 25

CONCEPTS: dict[str, str] = {
    "inventory_buildup": "inventory piling up: excess stock, channel inventory high, destocking, slow-moving inventory",
    "freight_delays": "logistics slowing: freight, shipping or container delays, port congestion, longer transit times",
    "price_hikes": "the company raising its own selling prices or taking price increases",
    "supply_chain_disruption": "inputs not arriving: raw-material shortages, supplier outages, component or chip shortages",
    "demand_softening": "demand weakening: slowdown, muted or soft demand, lower volumes, customers deferring orders",
}

SYSTEM_PROMPT = (
    "You are a quantitative noun-counter for earnings-call transcripts. You do NOT judge "
    "sentiment, tone or outlook. You only find statements that mention each concept below "
    "and quote them.\n"
    "Concepts:\n"
    + "".join(f"- {k}: {v}\n" for k, v in CONCEPTS.items())
    + "Rules:\n"
    "- A hit is one sentence in the text that states the concept is happening, has happened "
    "or is expected for this company or its industry. A sentence that only denies the "
    "concept (\"we see no inventory buildup\") is NOT a hit. Questions are NOT hits.\n"
    "- Count each sentence at most once per concept.\n"
    "- For every hit, copy the sentence EXACTLY as written in the text, character for character.\n"
    "- Return ONLY one JSON object, no prose and no markdown fences, with exactly this shape:\n"
    '{"counts": {"inventory_buildup": int, "freight_delays": int, "price_hikes": int, '
    '"supply_chain_disruption": int, "demand_softening": int}, '
    '"evidence": {"inventory_buildup": [string], "freight_delays": [string], '
    '"price_hikes": [string], "supply_chain_disruption": [string], "demand_softening": [string]}}\n'
    "- counts[k] must equal the number of quotes in evidence[k]. If a concept has no hits, "
    "use 0 and []."
)

RETRY_SUFFIX = (
    "\n\nYour previous reply was not valid JSON in the required shape. Reply with ONLY the "
    "JSON object described in the system prompt."
)

COLUMNS = (
    "ticker", "date", "quarter", *CONCEPTS.keys(),
    "llm_claimed_total", "chunks", "chars", "source_file", "model", "prompt_version", "as_of",
)

_FILENAME_RE = re.compile(r"^(?P<ticker>[A-Za-z0-9&.\-]+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<quarter>[A-Za-z0-9]+)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm(text: str) -> str:
    """Lowercase, unify quotes/dashes, collapse whitespace: the space quotes are matched in."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraph breaks into chunks of at most `limit` chars (hard-split a longer paragraph)."""
    chunks: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if buf and len(buf) + 2 + len(para) > limit:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def validate_reply(reply: Any) -> dict[str, Any] | None:
    """Return the reply if it has the exact counts/evidence shape, else None."""
    if not isinstance(reply, dict):
        return None
    counts, evidence = reply.get("counts"), reply.get("evidence")
    if not isinstance(counts, dict) or not isinstance(evidence, dict):
        return None
    for k in CONCEPTS:
        c = counts.get(k)
        if isinstance(c, bool) or not isinstance(c, int) or c < 0:
            return None
        quotes = evidence.get(k)
        if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
            return None
    return reply


def verified_counts(reply: dict[str, Any], chunk: str) -> dict[str, int]:
    """Count each concept's distinct quotes that appear verbatim (normalised) in `chunk`."""
    haystack = _norm(chunk)
    out: dict[str, int] = {}
    for k in CONCEPTS:
        seen: set[str] = set()
        for q in reply["evidence"][k][:MAX_QUOTES_PER_CONCEPT]:
            nq = _norm(q)
            if len(nq) >= 12 and nq in haystack:
                seen.add(nq)
        out[k] = len(seen)
    return out


def load_transcripts(directory: Path) -> list[dict[str, str]]:
    """Read `.txt` (key from the filename) and `.csv` (ticker,date,quarter,text) transcripts."""
    docs: dict[tuple[str, str, str], dict[str, str]] = {}
    if not directory.is_dir():
        return []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() == ".txt":
            m = _FILENAME_RE.match(path.stem)
            if not m:
                continue
            key = (m["ticker"].upper(), m["date"], m["quarter"].upper())
            text = path.read_text(encoding="utf-8", errors="replace")
            _add(docs, key, text, path.name)
        elif path.suffix.lower() == ".csv":
            reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8", errors="replace")))
            for row in reader:
                ticker = (row.get("ticker") or "").strip().upper()
                day = (row.get("date") or "").strip()
                quarter = (row.get("quarter") or "").strip().upper()
                text = row.get("text") or ""
                if ticker and quarter and _DATE_RE.match(day) and text.strip():
                    _add(docs, (ticker, day, quarter), text, path.name)
    return list(docs.values())


def _add(docs: dict, key: tuple[str, str, str], text: str, source: str) -> None:
    if key in docs:
        docs[key]["text"] += "\n\n" + text
    else:
        docs[key] = {"ticker": key[0], "date": key[1], "quarter": key[2], "text": text, "source_file": source}


class SemanticFrictionTriage(BaseAltDataScraper):
    """Transcript friction counter; reuses the alt-data CSV merge, outage log and fail-open `run()`."""

    dataset_name = "semantic_friction"
    default_columns = COLUMNS
    primary_key_columns = ("ticker", "date", "quarter")

    def __init__(
        self,
        transcripts_dir: Path | str | None = None,
        llm: Callable[[str, str], Any] | None = None,
        model: str | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        force: bool = False,
        chunk_chars: int = CHUNK_CHARS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.transcripts_dir = Path(transcripts_dir) if transcripts_dir is not None else TRANSCRIPTS_DIR
        self._llm = llm
        self.model = model
        self.max_attempts = max(1, int(max_attempts))
        self.force = bool(force)
        self.chunk_chars = max(1_000, int(chunk_chars))

    def _ask(self, system: str, user: str) -> Any:
        if self._llm is None:
            from src.local_parser import LocalExtractor
            extractor = LocalExtractor()
            self.model = self.model or getattr(extractor, "model", None)
            self._llm = extractor.chat_json
        return self._llm(system, user)

    def ask_chunk(self, chunk: str) -> dict[str, Any] | None:
        """One chunk -> a schema-valid reply, retrying non-JSON/off-schema answers; None if all fail."""
        user = chunk
        for attempt in range(1, self.max_attempts + 1):
            try:
                reply = validate_reply(self._ask(SYSTEM_PROMPT, user))
            except Exception as exc:
                self.log_warning("LLM-ERROR", f"attempt {attempt}: {type(exc).__name__}: {exc}")
                reply = None
            if reply is not None:
                return reply
            self.log_warning("BAD-JSON", f"attempt {attempt}/{self.max_attempts}: reply not in the friction schema")
            user = chunk + RETRY_SUFFIX
        return None

    def triage(self, doc: dict[str, str]) -> dict[str, Any] | None:
        """Count one transcript; None (abstain) when any chunk never parses."""
        chunks = chunk_text(doc["text"], self.chunk_chars)
        if not chunks:
            return None
        totals = dict.fromkeys(CONCEPTS, 0)
        claimed = 0
        for i, chunk in enumerate(chunks, 1):
            reply = self.ask_chunk(chunk)
            if reply is None:
                self.log_warning(
                    "TRANSCRIPT-SKIPPED",
                    f"{doc['ticker']} {doc['date']} {doc['quarter']}: chunk {i}/{len(chunks)} never parsed",
                )
                return None
            for k, n in verified_counts(reply, chunk).items():
                totals[k] += n
            claimed += sum(reply["counts"][k] for k in CONCEPTS)
        return {
            "ticker": doc["ticker"], "date": doc["date"], "quarter": doc["quarter"], **totals,
            "llm_claimed_total": claimed, "chunks": len(chunks), "chars": len(doc["text"]),
            "source_file": doc["source_file"], "model": self.model or "", "prompt_version": PROMPT_VERSION,
            "as_of": now_ist_iso(),
        }

    def fetch_raw(self, **kwargs: Any) -> list[dict[str, str]]:
        docs = load_transcripts(self.transcripts_dir)
        if self.force:
            return docs
        done = {self._row_key(r, self.primary_key_columns) for r in self.read_existing_rows()}
        return [d for d in docs if (d["ticker"], d["date"], d["quarter"]) not in done]

    def run(self, dry_run: bool = False, **kwargs: Any) -> dict[str, Any]:
        """Skip the LLM entirely when every transcript is already in the CSV."""
        if not self.transcripts_dir.is_dir():
            self.log_warning("NO-TRANSCRIPTS", f"{self.transcripts_dir} does not exist")
            return {"ok": False, "status": "degraded", "dataset": self.dataset_name,
                    "reason": "no_transcript_dir", "rows_added": 0, "dry_run": bool(dry_run),
                    "as_of": now_ist_iso()}
        try:
            pending = self.fetch_raw()
        except OSError:
            pending = None
        if pending == []:
            return {"ok": True, "status": "ok", "dataset": self.dataset_name, "reason": "nothing_new",
                    "rows_added": 0, "rows_total": len(self.read_existing_rows()),
                    "dry_run": bool(dry_run), "as_of": now_ist_iso()}
        return super().run(dry_run=dry_run, **kwargs)

    def parse(self, raw: list[dict[str, str]], **kwargs: Any) -> list[dict[str, Any]]:
        rows = []
        for doc in raw:
            row = self.triage(doc)
            if row is not None:
                rows.append(row)
        return rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Count friction concepts in earnings transcripts via the LLM router")
    ap.add_argument("--dir", default=None, help="Transcript directory (default data/lake/transcripts)")
    ap.add_argument("--dry-run", action="store_true", help="Count but do not write the CSV")
    ap.add_argument("--force", action="store_true", help="Re-count transcripts already in the CSV")
    args = ap.parse_args()
    triage = SemanticFrictionTriage(transcripts_dir=args.dir, force=args.force)
    print(json.dumps(triage.run(dry_run=args.dry_run), indent=2, default=str))

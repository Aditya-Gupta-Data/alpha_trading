"""
src/knowledge_graph_logger.py — append-only telemetry event store
=================================================================

Owner directive 2026-07-17 (Shadow Equity Engine, Task 2): a durable,
greppable record of every shadow decision AND its full rationale, so the
failures can later be mined into the knowledge graph ("logging the false
positives is exactly how we train a better model later").

Deliberately a plain JSONL ledger, not a brain_map.db table: the Brain Map
is the OPTIONS engine's memory and feeds query_similar_events / the
forecast layer — mixing zero-capital equity telemetry into it would skew
those reads. A later, explicit ingest (tagged mode=PAPER_TELEMETRY) can
fold resolved shadow outcomes in once there's enough to learn from; until
then this ledger is the substrate.

Contract: append-only, one JSON object per line, IST timestamps, fail-open
(an unwritable disk returns the event with _persisted=False rather than
raising — telemetry must never take down a trading loop). Default ledger:
logs/equity_shadow_journal.jsonl (gitignored runtime data, same convention
as the advisory ledgers).

THE LEARNING FRAME (owner's four questions, 2026-07-17 — "as long as the
money is paper, every trade is a learning opportunity"):

  entry event:
    kyu_trigger      WHY  — the exact alpha signal: setup name, a human-
                     readable `signal` line, block_vwap, accumulation flag,
                     net_value_rs, the trigger block deals themselves.
    kaise_context    HOW  — the market at entry: India VIX, sector verdict
                     (name + bullish + SMA detail), NIFTY trend read.
    kya_kara_action  WHAT — side, entry_price, stop, target,
                     simulated_risk_pct. Always paper: every event also
                     carries mode="PAPER_TELEMETRY" + capital_allocated=0.

  exit event (same id as its entry):
    kya_sikha_autopsy  LEARNED — an automatic rule-based `category`
                     ("Gap-down shock…", "Stop-loss hit: sector dragged it
                     down", "VWAP defense failed: institutional floor broke
                     (trap)", "Target hit…", "Time stop…"), r_multiple,
                     held_days, below_block_vwap, sector_at_exit,
                     vix_at_exit.

The schema is CONSTRUCTED in src/equity_shadow_proposer.py (evaluate_entry
/ track_open_shadows / categorize_failure); this module stays the dumb,
durable store so future engines can log other frames beside it.
"""
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "logs" / "equity_shadow_journal.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))


def new_id() -> str:
    """8-hex event id (matches the journal's short_id ergonomics)."""
    return secrets.token_hex(4)


def log_event(event: dict, path=None) -> dict:
    """Append one telemetry event. Stamps `ts` (IST, seconds) if absent.
    Returns the event with `_persisted` set honestly — callers that care
    can see a failed write; nothing ever raises."""
    p = Path(path) if path else DEFAULT_PATH
    event = dict(event)
    event.setdefault("ts", datetime.now(IST).isoformat(timespec="seconds"))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(event) + "\n")
        event["_persisted"] = True
    except OSError:
        event["_persisted"] = False
    return event


FUNDING_REVOKED_EVENT = "funding_revoked"
TELEMETRY_MODE = "PAPER_TELEMETRY"


def apply_corrections(events: list) -> list:
    """The append-only ledger's CORRECTIONS, applied on read (decision #123,
    audit D11): an entry whose id later carries a `funding_revoked` event was
    logged as funded but its lock was never taken (refused, or a crash
    between the append and the lock) — every reader sees it as the
    zero-capital telemetry row it really is. The lines on disk never change."""
    revoked = {e.get("id"): e for e in events if e.get("event") == FUNDING_REVOKED_EVENT}
    if not revoked:
        return events
    out = []
    for e in events:
        if e.get("event") == "entry" and e.get("id") in revoked:
            why = revoked[e["id"]].get("reason")
            e = dict(e, mode=TELEMETRY_MODE, capital_allocated=0,
                     funding=dict(e.get("funding") or {}, funded=False,
                                  reason=f"revoked: {why}" if why else "revoked"))
        out.append(e)
    return out


def read_events_strict(path=None) -> tuple:
    """(events, clean) — clean is False when the file is missing or
    unreadable, or ANY line failed to parse. For a caller that must not act
    on a degraded read (the desk's no-entry orphan sweep)."""
    p = Path(path) if path else DEFAULT_PATH
    try:
        text = p.read_text()
    except OSError:
        return [], False
    out, clean = [], True
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            clean = False
    return apply_corrections(out), clean


def read_events(path=None) -> list:
    """Every parseable event, in file order, corrections applied. Missing
    file / junk lines degrade to fewer events, never an exception."""
    return read_events_strict(path)[0]


def open_positions(events=None, path=None) -> dict:
    """{ticker: entry_event} for entries with no matching exit (paired by
    id). Pass `events` to avoid a re-read when the caller already has them."""
    events = read_events(path) if events is None else events
    exited = {e.get("id") for e in events if e.get("event") == "exit"}
    out = {}
    for e in events:
        if (e.get("event") == "entry" and e.get("ticker")
                and e.get("id") not in exited):
            out[e["ticker"]] = e
    return out

"""
src/event_engine — the policy-event engine, version 1.1 (decision #156; method: docs/EVENT_PLAYBOOK.md).

TWO HALVES ON TWO MACHINES (decision #146: the GCP VM is the only execution node):

  Mini PC (analytics)                                    GCP VM (execution)
  ──────────────────                                     ──────────────────
  emit.py      news/inbox → detect → exposure            executor.py  card → expiry, chain, strike
               → materiality → priced_in → card                       (path A fixed OTM, path B LLM range)
               → ships data/event_cards/*.json to the VM              → lots ≤ ₹5,000 max loss → PAPER_EVENTS
                                                         settler.py   mark at the bid → take profit /
                                                                      horizon / pre-expiry exit → release

The VM never calls the LLM; the node never trades. Every number carries a source tag:
`reported` (with a source quote that the code found in the text), `estimate:llm` (the model said
so) or `estimate:computed` (our own arithmetic). Owner ruling: LLM estimates MAY pass the gates for
PAPER_EVENTS, so forward samples accumulate; the tags let the Proving Court split results later.

PAPER_EVENTS is ISOLATED: it is on the existing paper-account door (one settlement path for money,
RULE 7) but never a shadow of the primary's proposals, and auto-execution exists ONLY here — never
for PAPER_10L or PAPER_2L_LIVE.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ACCOUNT = "PAPER_EVENTS"
CARD_SCHEMA = "event_card/1.1"
CARD_DIR = ROOT / "data" / "event_cards"          # the node writes here and ships; the VM reads here
INBOX_DIR = ROOT / "data" / "event_inbox"         # hand-dropped primary texts (owner, other jobs)
STATE_DIR = ROOT / "data" / "event_engine"        # node-side caches and the seen-items ledger
LOGS_DIR = ROOT / "logs"

TAG_REPORTED = "reported"
TAG_LLM = "estimate:llm"
TAG_COMPUTED = "estimate:computed"
IST = timezone(timedelta(hours=5, minutes=30))


def settings() -> dict:
    """The live config block (config.json `event_engine`, defaults in src.config)."""
    from src import config
    return dict(config.EVENT_ENGINE)


def today_ist():
    return datetime.now(IST).date()


def worst_tag(*tags) -> str:
    """The weakest source among a derived number's inputs: any LLM input makes it an LLM estimate;
    only all-reported inputs stay reported; anything else is our own computed estimate."""
    tags = [t for t in tags if t]
    if TAG_LLM in tags:
        return TAG_LLM
    if tags and all(t == TAG_REPORTED for t in tags):
        return TAG_REPORTED
    return TAG_COMPUTED

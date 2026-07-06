"""
Phase 3: the trade journal.

Every proposal — approved OR rejected — gets one line in data/journal.jsonl,
including the engine's signal and the user's own reasoning ("why"). Later,
src/review.py scores these entries against what the price actually did, so
over time we learn which signals and which of the user's instincts hold up.

Phase 4A: each entry also carries structured fields — `risk_levers`
(stop-loss % and position size) and `pattern_tags` (the chart patterns the
user saw) — so later phases can evaluate outcomes by pattern, not just by
free-text "why". These are additive: older journal lines that predate them
simply don't have the keys, and readers must tolerate their absence.

Phase 6: each entry also carries a stable `short_id` (8-char uuid hex), the
key the Brain Map (src/brain_map.py) uses to reference journal rows. Same
additive rule: older lines lack it, and readers fall back to a composite
key (see brain_map.journal_ref_for) rather than crash.
"""

import json
import uuid
from datetime import date
from pathlib import Path

from src.config import DEFAULT_STOP_LOSS_PCT, DEFAULT_INVESTMENT_SIZE

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JOURNAL_PATH = DATA_DIR / "journal.jsonl"

# Phase 4B plan fields copied into the journal when the proposal carries them
# (strategy.propose_plans does; older/simpler callers may not).
_PLAN_KEYS = (
    "variant", "entry_rule", "stop_loss", "target", "risk_reward",
    "max_loss_rs", "invalidation", "rationale",
)


def log(entry: dict) -> None:
    # Safety net for any caller that builds an entry without new_entry():
    # every line that lands in the journal must carry a stable short_id.
    entry.setdefault("short_id", uuid.uuid4().hex[:8])
    DATA_DIR.mkdir(exist_ok=True)
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_all() -> list:
    if not JOURNAL_PATH.exists():
        return []
    entries = []
    with open(JOURNAL_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def rewrite_all(entries: list) -> None:
    """Used by review.py to fill in outcomes on old entries."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(JOURNAL_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def new_entry(
    proposal: dict,
    decision: str,
    why: str,
    sl_pct: float = None,
    size: float = None,
    pattern_tags: list = None,
) -> dict:
    """Build one journal record.

    `sl_pct`, `size`, and `pattern_tags` are optional: when a caller omits
    them (e.g. an older, non-interactive code path), the risk levers fall
    back to the config.json defaults and pattern_tags to an empty list, so
    every entry is structurally complete regardless of how it was created.
    """
    return {
        "short_id": uuid.uuid4().hex[:8],
        "date": date.today().isoformat(),
        "action": proposal["action"],
        "ticker": proposal["ticker"],
        "shares": proposal["shares"],
        "price": round(proposal["price"], 2),
        "signal": proposal["signal"],
        "decision": decision,  # "approved" or "rejected"
        "why": why,
        "risk_levers": {
            "sl_pct": DEFAULT_STOP_LOSS_PCT if sl_pct is None else sl_pct,
            "size": DEFAULT_INVESTMENT_SIZE if size is None else size,
        },
        "pattern_tags": pattern_tags or [],
        "plan": {k: proposal[k] for k in _PLAN_KEYS if k in proposal} or None,
        "outcome": None,  # filled in later by review.py
    }

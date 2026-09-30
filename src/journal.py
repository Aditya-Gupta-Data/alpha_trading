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

WRITES (audit Chunk 1, D1/D2 — decision #122). Three processes write this
file (the scheduler's loops, the api service's hourly tracker and
approvals, cron jobs). Every write now happens under ONE cross-process
lock (`locked()`, an flock on journal.lock beside the file, re-entrant
within a thread) and every whole-file write is ATOMIC (temp file, fsync,
os.replace), so a crash can never leave a half-written journal and an
append can never land on a file that is being replaced. The lock alone
does not make a STALE copy safe: a caller that read the journal minutes
ago must write only the rows it changed, re-read fresh under the lock —
`update_entry` / `update_matching` / `mutate_all` — never `rewrite_all`
of its old list.
"""

import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.config import DEFAULT_STOP_LOSS_PCT, DEFAULT_INVESTMENT_SIZE

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JOURNAL_PATH = DATA_DIR / "journal.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))

# A writer that cannot get the lock in this long raises instead of hanging
# the clock forever behind a stuck process (the holders do seconds of work).
LOCK_TIMEOUT_SECONDS = 120.0

_tls = threading.local()


class JournalLockTimeout(TimeoutError):
    """The journal lock stayed held past LOCK_TIMEOUT_SECONDS."""


def _lock_path() -> Path:
    # Beside the journal itself (== DATA_DIR/journal.lock in production),
    # so a test that points JOURNAL_PATH at a tmp dir locks there too.
    return JOURNAL_PATH.parent / "journal.lock"


@contextmanager
def locked(timeout: float = None):
    """Hold the journal's cross-process write lock. Re-entrant within one
    thread (a settlement that holds it may call update_entry inside);
    another thread or process blocks until it is free, or raises
    JournalLockTimeout after `timeout` seconds."""
    depth = getattr(_tls, "depth", 0)
    if depth:
        _tls.depth = depth + 1
        try:
            yield
        finally:
            _tls.depth -= 1
        return
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    limit = LOCK_TIMEOUT_SECONDS if timeout is None else float(timeout)
    with open(path, "a") as lock:
        deadline = time.monotonic() + limit
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JournalLockTimeout(
                        f"journal lock {path} still held after {limit:g}s")
                time.sleep(0.05)
        _tls.depth = 1
        try:
            yield
        finally:
            _tls.depth = 0
            fcntl.flock(lock, fcntl.LOCK_UN)


def lock_held() -> bool:
    """True while THIS thread holds the journal lock."""
    return getattr(_tls, "depth", 0) > 0


def row_key(entry: dict) -> str:
    """A row's identity: its short_id, else the composite fallback older
    lines use (the same key as brain_map.journal_ref_for)."""
    if entry.get("short_id"):
        return entry["short_id"]
    return f"{entry.get('date')}|{entry.get('ticker')}|{entry.get('action')}|{entry.get('price')}"

# Phase 4B plan fields copied into the journal when the proposal carries them
# (strategy.propose_plans does; older/simpler callers may not).
_PLAN_KEYS = (
    "variant", "entry_rule", "stop_loss", "target", "risk_reward",
    "max_loss_rs", "invalidation", "rationale",
    # M4A (2026-09-11, decision #96): the tracker's ATR trail and tranche
    # ladder are read from these two keys. Before this line, `trailing` was
    # silently dropped here and the trail could never fire on a real row.
    # No live proposer stamps either key yet — additive and inert.
    "trailing", "tranches",
)


def log(entry: dict) -> None:
    # Safety net for any caller that builds an entry without new_entry():
    # every line that lands in the journal must carry a stable short_id.
    entry.setdefault("short_id", uuid.uuid4().hex[:8])
    # Additive (2026-07-10, ledger Issue 8): a full IST timestamp alongside
    # the day-only `date`. The market loop's cooldown registry rebuilds
    # itself from this across restarts — the journal IS the persistence,
    # no new state file. Older lines lack it; readers must tolerate that.
    entry.setdefault("created_at",
                     datetime.now(IST).isoformat(timespec="seconds"))
    with locked():
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())


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


def get_entry(key: str) -> dict | None:
    """A FRESH read of one row by short_id (or composite row_key)."""
    return next((e for e in read_all() if row_key(e) == key), None)


def _atomic_write(entries: list) -> None:
    path = JOURNAL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def rewrite_all(entries: list) -> None:
    """Replace the whole file with `entries`, atomically, under the lock.

    Only for a caller that read those entries UNDER THE SAME LOCK (the
    helpers below) or an offline tool. A live caller holding an older copy
    must use update_entry / update_matching / mutate_all instead — a stale
    whole-file write reverts whatever another process wrote since (D1)."""
    with locked():
        _atomic_write(entries)


def update_matching(match_fn, mutate_fn) -> dict | None:
    """update_entry for any identity: the first FRESH row where
    `match_fn(row)` is true gets `mutate_fn(row)`; False from mutate_fn
    aborts (nothing written, None returned)."""
    with locked():
        entries = read_all()
        target = next((e for e in entries if match_fn(e)), None)
        if target is None:
            return None
        if mutate_fn(target) is False:
            return None
        rewrite_all(entries)
        return target


def update_entry(short_id: str, mutate_fn) -> dict | None:
    """Race-safe read-modify-write of ONE journal entry (decision #69).

    Two processes now resolve trades — the api service's hourly tracker
    sweep and the scheduler's live loop (intraday square-off). A naive
    read → long work → rewrite_all() from either can clobber the other's
    outcome with a stale in-memory copy. This helper is the shared safe
    write: hold the journal lock, re-read the file FRESH, apply
    `mutate_fn(entry)` to the matching row only, and rewrite atomically —
    so every writer merges against the latest truth.

    mutate_fn receives the fresh entry dict and mutates it in place;
    return False from it to abort (nothing written, returns None) — the
    validate-then-write seam for "someone else already resolved this".
    Returns the updated entry, or None (not found / aborted)."""
    return update_matching(lambda e: e.get("short_id") == short_id, mutate_fn)


def mutate_all(fn):
    """Under the lock: read every row FRESH, call `fn(entries)` (mutate in
    place, no slow work inside), and rewrite only when it returns truthy.
    Returns fn's result."""
    with locked():
        entries = read_all()
        result = fn(entries)
        if result:
            rewrite_all(entries)
        return result


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

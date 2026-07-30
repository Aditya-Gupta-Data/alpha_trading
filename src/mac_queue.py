"""The Mac Handover Queue — the VM's outbox for work it must not do.

Edge-to-Cloud asynchronous architecture (owner directive 2026-07-30):

  * the VM (1 GB e2-micro) NEVER runs data-heavy or compute-heavy work;
  * the VM's nightly pipeline NEVER depends on the Mac being online.

Those two rules only compose if the VM has somewhere to PUT the heavy work
it declines. Without a queue, a declined task is a log line that scrolls
away — the run stays up (good) but the reason evaporates (bad), and the
owner finds out weeks later that the artifact was never rebuilt.

So: when a nightly task detects that heavy compute is required, it fails
open, SKIPS the work, and appends one request here. The VM keeps ticking
either way — nothing in the pipeline blocks on this file, and nothing
waits for the Mac to answer. The queue is a message, not a dependency.

This is deliberately NOT a new detector. It is a sink wired to detection
the VM already does — today that is `macro_nightly`'s `require_cache=True`
cache-miss branch (owner directive 2026-07-23: the VM is a dumb executor
that abstains fast rather than grinding for 30 minutes). New producers
should hook existing abstention points the same way.

APPEND-ONLY by house rule: `resolve()` writes a closing row, it never
rewrites or deletes history — so "what did the VM ask for, and when" stays
answerable after the fact. Idempotent per (task, day): a nightly cron that
re-detects the same condition every night for a week leaves ONE row per
day, not a flood, and `pending()` collapses repeats to the latest state.

Every function fails open and returns a neutral value: queue bookkeeping
must never be the thing that breaks the pipeline it is protecting.
"""

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT / "data" / "mac_pending_tasks.jsonl"

# Known task ids — kept small and explicit so the copy-paste line the owner
# gets is a real instruction, not an opaque slug.
REBUILD_MACRO_ARTIFACTS = "rebuild_macro_artifacts"
DEEP_HISTORY_BACKFILL = "deep_history_backfill"

HUMAN = {
    REBUILD_MACRO_ARTIFACTS: (
        "rebuild the macro templates/playbooks/strategies artifacts on the "
        "Mac and ship them to the VM"),
    DEEP_HISTORY_BACKFILL: (
        "run the deep historical index/deal backfill on the Mac"),
}


def _rows(path=None) -> list:
    p = Path(path) if path else QUEUE_PATH
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def enqueue(task: str, reason: str, detail: str = None, *,
            today=None, path=None) -> dict:
    """Record that the VM declined heavy work. Idempotent per (task, day).

    Returns {"created": bool} and NEVER raises — a queue write that fails
    must not take down the nightly run it is reporting on."""
    try:
        day = (today or date.today()).isoformat()
        if path is None and os.environ.get("PYTEST_CURRENT_TEST"):
            # Same muzzle doctrine as the DB seams: tests inject `path=`,
            # so a forgotten fixture can never post fake work to the real
            # queue and send the owner chasing a task that never existed.
            return {"created": False, "muzzled": True}
        for r in _rows(path):
            if r.get("task") == task and r.get("raised_on") == day:
                return {"created": False}
        p = Path(path) if path else QUEUE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"task": task, "reason": reason, "detail": detail,
               "raised_on": day, "status": "pending",
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with open(p, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        return {"created": True}
    except Exception:
        return {"created": False}


def resolve(task: str, *, today=None, path=None) -> dict:
    """Close a task by APPENDING a done row (history is never rewritten)."""
    try:
        if path is None and os.environ.get("PYTEST_CURRENT_TEST"):
            return {"created": False, "muzzled": True}
        p = Path(path) if path else QUEUE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"task": task, "status": "done",
               "raised_on": (today or date.today()).isoformat(),
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with open(p, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        return {"created": True}
    except Exception:
        return {"created": False}


def pending(path=None) -> list:
    """Open requests, newest state per task. A task resolved after its last
    pending row is closed and does not appear."""
    try:
        latest = {}
        for r in _rows(path):
            t = r.get("task")
            if t:
                latest[t] = r
        return [r for r in latest.values() if r.get("status") == "pending"]
    except Exception:
        return []


def describe(rows: list = None, path=None) -> str:
    """The literal line the owner pastes to Claude on the Mac."""
    rows = pending(path) if rows is None else rows
    if not rows:
        return ""
    names = ", ".join(HUMAN.get(r.get("task"), str(r.get("task")))
                      for r in rows)
    return ("Claude, please execute the following from the VM queue: "
            f"{names}.")

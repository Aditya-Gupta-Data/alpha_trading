"""
src/deploy_log.py — which update went live when
===============================================

Born from the observation-week problem: deploys are manual (`git pull` +
`systemctl restart` on the VM), so when an issue shows up there is no
trustworthy record of what code was actually running at the time — was
the bug introduced by the last deploy, or was it there all along?

The record is written by the services THEMSELVES at startup, not by a
human remembering to note it: every time a long-running process comes up
it appends one line to `logs/deploy_log.jsonl` (git-ignored, so each
machine keeps its own history — the VM's file is the production record):

    {"ts": "<IST iso>", "service": "api_server", "sha": "62a56e1",
     "subject": "ops 2026-07-12: fix NSE 403s ...", "committed": "<iso>",
     "dirty": false, "event": "deploy"}

`event` is "deploy" when the sha differs from that service's previous
entry (new code went live) and "restart" when it doesn't — bare restarts
matter too when correlating issues. `dirty` flags uncommitted edits in
the working tree (a VM hotpatch that never became a commit is exactly
the kind of thing that makes "what was live?" unanswerable later).

Fail-open by design: logging must never take a service down, so every
step (git lookup included) degrades to "unknown" rather than raising.

Wired into the two Python systemd services: `src/api_server.py` (gateway
+ engine, in its lifespan) and `src/discord_bot.py` (main block).

Inspect the history:  python3 -m src.deploy_log
Cross-reference with `docs/observation_week_ledger.md` — every ledger
issue can now cite the sha that was live when it happened.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_PATH = _REPO_ROOT / "logs" / "deploy_log.jsonl"


def _git_state(repo_root: Path) -> dict:
    """Current HEAD (short sha, subject, commit date) + dirty flag.
    Degrades to 'unknown' on any failure — never raises."""
    state = {"sha": "unknown", "subject": "", "committed": "", "dirty": False}
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1",
             "--format=%h\x1f%s\x1f%cI"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            sha, subject, committed = out.stdout.strip().split("\x1f")
            state.update(sha=sha, subject=subject, committed=committed)
        porcelain = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if porcelain.returncode == 0:
            state["dirty"] = bool(porcelain.stdout.strip())
    except Exception:
        pass
    return state


def _last_sha_for(service: str, log_path: Path) -> str | None:
    """sha of the given service's most recent entry, or None."""
    try:
        last = None
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("service") == service:
                    last = entry.get("sha")
        return last
    except OSError:
        return None


def record_startup(service: str, *, repo_root: Path = _REPO_ROOT,
                   log_path: Path = _LOG_PATH) -> dict | None:
    """Append this process's startup (with the code version it runs) to
    the deploy log. Returns the entry, or None if even best-effort
    logging failed — callers never need to handle errors."""
    try:
        git = _git_state(repo_root)
        previous = _last_sha_for(service, log_path)
        entry = {
            "ts": datetime.now(IST).isoformat(timespec="seconds"),
            "service": service,
            "sha": git["sha"],
            "subject": git["subject"],
            "committed": git["committed"],
            "dirty": git["dirty"],
            "event": "restart" if previous == git["sha"] else "deploy",
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        print(f"[deploy-log] {service} up on {entry['sha']}"
              f"{' (dirty tree)' if entry['dirty'] else ''}"
              f" — {entry['event']}")
        return entry
    except Exception:
        return None


def _print_history(log_path: Path = _LOG_PATH,
                   repo_root: Path = _REPO_ROOT) -> None:
    if not log_path.exists():
        print(f"No deploy log yet at {log_path} — it appears after the "
              "first service startup running this code.")
        return
    entries = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    print(f"{'when (IST)':20} {'service':12} {'event':8} {'sha':9} subject")
    print("-" * 100)
    for e in entries:
        ts = e.get("ts", "?")[:19].replace("T", " ")
        sha = e.get("sha", "?") + ("*" if e.get("dirty") else "")
        print(f"{ts:20} {e.get('service', '?'):12} {e.get('event', '?'):8} "
              f"{sha:9} {e.get('subject', '')}")
    print("-" * 100)
    print("* = working tree had uncommitted edits at startup")
    head = _git_state(repo_root)
    print(f"repo HEAD now: {head['sha']} {head['subject']}"
          f"{' (dirty)' if head['dirty'] else ''}")


if __name__ == "__main__":
    _print_history()

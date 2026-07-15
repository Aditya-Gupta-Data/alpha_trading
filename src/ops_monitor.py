"""
Alpha Trading — the ops monitor: nightly log sweep & problem ledger
====================================================================

Observability for the first unattended week (and after): every daemon
and cron job in this project fails SOFT by design — errors become
printed notes in `logs/*.log` and the pipeline keeps going. That's the
right trading behavior and terrible visibility: nobody reads six log
files nightly. This module does.

One run (`python3 -m src.ops_monitor`, cron 20:30 IST):

  1. SWEEP — scans every `logs/*.log` for problem-shaped lines
     (errors, tracebacks, failures, dead feeds, fail-open notes).
     Incremental: a state file remembers how far each log was read, so
     a problem is reported exactly ONCE, the night it appears — never
     re-reported from a growing file. Repeated identical lines within
     one sweep collapse to `xN`.
  2. LEDGER — every finding is appended to `logs/problems.jsonl`
     (one JSON object per line: when found, which log, the line) — the
     single place to review the week's issues.
  3. HEARTBEATS — checks each scheduled job's log was touched today
     (weekday-aware): token renewal, suggestions, master scheduler,
     alerts, sleep phase. A job that silently never ran is a worse
     problem than one that logged an error.
  4. CARD — posts a terse health card to Discord (fail-safe, and
     muzzled under pytest by the Phase 6J guard like everything else).

Pure-Python + stdlib; every input (logs dir, clock, notifier)
injectable, so the whole surface tests offline.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
STATE_PATH = LOGS_DIR / ".ops_monitor_state.json"
PROBLEMS_PATH = LOGS_DIR / "problems.jsonl"

# Problem-shaped text. Deliberately includes the codebase's own
# fail-open vocabulary ("unavailable", "skipped", "failed") — those
# soft notes ARE the week's find-the-problems signal.
PROBLEM_PATTERNS = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|died|fault|"
    r"unusable|unavailable|corrupt|refused|denied|timed?\s*out|"
    r"muzzled \[test env\]|risk-of-ruin|margin exhaustion)\b")

# Zero-valued failure counters inside healthy stats dicts — e.g. the
# sleep phase's "{'ingested': 3, ..., 'failed': 0}" — are NOT problems
# (2026-07-14 false alarm: a clean ingestion line tripped the card via
# the literal word "failed"). Scrub them before the problem test so any
# NONZERO count still fires.
ZERO_STAT_PATTERNS = re.compile(
    r"(?i)['\"]?(failed|errors?|failures?)['\"]?\s*[:=]\s*0\b")


def is_problem_line(text: str) -> bool:
    return bool(PROBLEM_PATTERNS.search(ZERO_STAT_PATTERNS.sub("", text)))

# What should have written its log today (name -> weekdays-only flag).
# This default is the VM's schedule (the engine machine). Any deployment
# can override per-machine via OPS_EXPECTED_JOBS, a comma list of
# "name.log:flag" where flag 1/true = weekdays-only, 0 = daily — e.g.
#   OPS_EXPECTED_JOBS="renew_token.log:0,master_scheduler.log:1"
EXPECTED_JOBS = {
    "renew_token.log": False,        # daily 07:00 IST
    "sleep_phase.log": False,        # daily 20:00 IST (decay-only w/o Ollama)
    "suggest.log": True,             # Mon-Fri 08:00 IST
    "main.log": True,                # Mon-Fri 15:35 IST
    "master_scheduler.log": True,    # Mon-Fri 09:10 IST
    "chain_archiver.log": True,      # Mon-Fri 15:40 IST (Phase-0 capture)
    "deals_tracker.log": False,      # daily 19:30 IST (EOD bulk/block pull)
    "daily_archiver.log": False,     # daily 19:45 IST (perishable snapshots)
    "earnings_calendar.log": False,  # daily 19:20 IST (results dates)
    "flows_tracker.log": False,      # daily 19:35 IST (FII/DII cash flows)
    "news_processor.log": False,     # daily 19:10 IST (Gemini news sentiment)
    "rss_ingester.log": False,       # daily 18:50 IST (official-RSS news pull)
}


def _expected_jobs_from_env() -> dict | None:
    """Parse OPS_EXPECTED_JOBS, or None when unset/empty (use default)."""
    raw = os.environ.get("OPS_EXPECTED_JOBS", "").strip()
    if not raw:
        return None
    jobs = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, flag = item.partition(":")
        jobs[name.strip()] = flag.strip().lower() in ("1", "true", "yes")
    return jobs or None

MAX_CARD_PROBLEMS = 12               # Discord card stays readable
MAX_LINE_CHARS = 180


def _load_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def sweep_logs(logs_dir: Path = LOGS_DIR, state: dict = None) -> tuple:
    """Scan every *.log for NEW problem lines since the last sweep.

    Returns (problems, new_state): problems is a list of
    {"log", "line", "count"}; new_state maps filename -> byte offset
    already examined. A truncated/rotated file (smaller than its stored
    offset) is re-read from the start rather than silently skipped."""
    state = dict(state or {})
    problems = []
    for path in sorted(Path(logs_dir).glob("*.log")):
        if path.name == "ops_monitor.log":
            continue  # never scan our own output — the card quotes
                      # problem lines, which would re-match every night
        offset = int(state.get(path.name, 0))
        try:
            size = path.stat().st_size
            if size < offset:
                offset = 0  # rotated/truncated — start over
            with open(path, "r", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
                state[path.name] = f.tell()
        except Exception as e:
            problems.append({"log": path.name, "count": 1,
                             "line": f"(ops_monitor could not read: {e})"})
            continue
        seen: dict = {}
        for line in chunk.splitlines():
            text = line.strip()
            if not text or not is_problem_line(text):
                continue
            key = text[:MAX_LINE_CHARS]
            if key in seen:
                seen[key]["count"] += 1
            else:
                entry = {"log": path.name, "line": key, "count": 1}
                seen[key] = entry
                problems.append(entry)
    return problems, state


def check_heartbeats(logs_dir: Path = LOGS_DIR, now: datetime = None,
                     expected: dict = None) -> list:
    """Which scheduled jobs did NOT touch their log today? Weekday-only
    jobs are excused on weekends. Returns a list of human lines."""
    now = now or datetime.now()
    today = now.date()
    missing = []
    for name, weekdays_only in (expected or _expected_jobs_from_env()
                                or EXPECTED_JOBS).items():
        if weekdays_only and today.weekday() >= 5:
            continue
        path = Path(logs_dir) / name
        try:
            fresh = datetime.fromtimestamp(path.stat().st_mtime).date() == today
        except OSError:
            fresh = False
        if not fresh:
            missing.append(f"{name} — did not run today")
    return missing


def record_problems(problems: list, when: str,
                    problems_path: Path = PROBLEMS_PATH) -> None:
    """Append every finding to the week's single problem ledger."""
    if not problems:
        return
    problems_path.parent.mkdir(parents=True, exist_ok=True)
    with open(problems_path, "a") as f:
        for p in problems:
            f.write(json.dumps(dict(p, found=when)) + "\n")


def system_telemetry(meminfo_path: str = "/proc/meminfo",
                     loadavg_path: str = "/proc/loadavg",
                     disk_path: str = None) -> dict:
    """Host resource readings for the health card (Phase-0 rule: the VM
    resize is TRIGGER-gated, so pressure must be a measured fact on the
    nightly card, not a vibe). Pure /proc + shutil reads; any missing
    field reads None (e.g. on macOS) — never raises."""
    out = {"mem_total_mb": None, "mem_available_mb": None, "mem_used_pct": None,
           "swap_total_mb": None, "swap_used_mb": None,
           "load_1m": None, "disk_free_gb": None, "disk_used_pct": None}
    try:
        fields = {}
        for line in Path(meminfo_path).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                fields[parts[0][:-1]] = int(parts[1])   # kB
        if "MemTotal" in fields:
            out["mem_total_mb"] = round(fields["MemTotal"] / 1024)
        if "MemAvailable" in fields and fields.get("MemTotal"):
            out["mem_available_mb"] = round(fields["MemAvailable"] / 1024)
            out["mem_used_pct"] = round(
                100 * (1 - fields["MemAvailable"] / fields["MemTotal"]))
        if "SwapTotal" in fields:
            out["swap_total_mb"] = round(fields["SwapTotal"] / 1024)
            out["swap_used_mb"] = round(
                (fields["SwapTotal"] - fields.get("SwapFree", 0)) / 1024)
    except (OSError, ValueError):
        pass
    try:
        out["load_1m"] = float(Path(loadavg_path).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        import shutil
        usage = shutil.disk_usage(disk_path or str(ROOT))
        out["disk_free_gb"] = round(usage.free / 1e9, 1)
        out["disk_used_pct"] = round(100 * usage.used / usage.total)
    except OSError:
        pass
    return out


def telemetry_line(t: dict) -> str:
    """One human line for the card. Absent readings render as '?' — an
    absent number is never faked."""
    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "?"
    return (f"🖥 mem {fmt(t.get('mem_used_pct'), '%')} used "
            f"({fmt(t.get('mem_available_mb'))}MB free) · "
            f"swap {fmt(t.get('swap_used_mb'))}MB · "
            f"load {fmt(t.get('load_1m'))} · "
            f"disk {fmt(t.get('disk_free_gb'))}GB free "
            f"({fmt(t.get('disk_used_pct'), '%')})")


def build_card(problems: list, missing: list, when: str,
               telemetry: dict = None) -> str:
    """The terse nightly health card."""
    total = sum(p["count"] for p in problems)
    if not problems and not missing:
        card = (f"✅ **Ops sweep {when}** — all jobs ran, "
                "no problem lines in any log.")
        if telemetry:
            card += "\n" + telemetry_line(telemetry)
        return card
    lines = [f"🩺 **Ops sweep {when}** — {total} problem line(s), "
             f"{len(missing)} silent job(s):"]
    for m in missing:
        lines.append(f"• ⏰ {m}")
    for p in problems[:MAX_CARD_PROBLEMS]:
        n = f" x{p['count']}" if p["count"] > 1 else ""
        lines.append(f"• `{p['log']}`{n}: {p['line'][:120]}")
    if len(problems) > MAX_CARD_PROBLEMS:
        lines.append(f"…and {len(problems) - MAX_CARD_PROBLEMS} more — "
                     "see logs/problems.jsonl")
    if telemetry:
        lines.append(telemetry_line(telemetry))
    return "\n".join(lines)


def run_sweep(logs_dir: Path = LOGS_DIR, state_path: Path = STATE_PATH,
              problems_path: Path = PROBLEMS_PATH, now: datetime = None,
              notify_fn=None) -> dict:
    """The full nightly pass. Returns a summary dict (also printed)."""
    now = now or datetime.now()
    when = now.strftime("%Y-%m-%d %H:%M")
    problems, new_state = sweep_logs(logs_dir, _load_state(state_path))
    missing = check_heartbeats(logs_dir, now)
    record_problems(problems, when, problems_path)
    _save_state(state_path, new_state)

    telemetry = system_telemetry()
    card = build_card(problems, missing, when, telemetry=telemetry)
    print(card, flush=True)
    if notify_fn is None:
        def notify_fn(text):
            import asyncio
            from src.notifier import send_discord_message
            try:
                return asyncio.run(send_discord_message(text))
            except Exception as e:
                print(f"  (ops card notify failed: {e})")
                return False
    try:
        notify_fn(card)
    except Exception as e:
        print(f"  (ops card notify failed: {e})")
    return {"problem_lines": sum(p["count"] for p in problems),
            "distinct_problems": len(problems),
            "silent_jobs": len(missing), "when": when,
            "telemetry": telemetry}


if __name__ == "__main__":
    summary = run_sweep()
    print(json.dumps(summary))

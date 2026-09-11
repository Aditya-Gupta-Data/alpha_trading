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
  4. STALENESS (2026-08-05) — runs `staleness_guard.scan()` over every
     registered live-path data artifact and appends a 🚨 block to the card
     naming anything that stopped updating, and which components the guard
     SELF-DISABLED as a result. A job whose log looks healthy while its
     output file quietly froze is invisible to steps 1-3; that is exactly
     how `sector_index_bars.json` fed a live veto for 20 days.
  5. CARD — posts a terse health card to Discord (fail-safe, and
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

# Logs that a sweep must NEVER read, because they are themselves REPORTS that
# quote problem-shaped lines — sweeping them makes each day's card re-flag the
# previous day's card, an infinite echo. ops_monitor.log has always been here;
# ceo_brief.log joined 2026-07-20 after its first card quoted its own prior
# run's rendered issues (e.g. a corporate_events crash surfacing "from
# ceo_brief.log"). Both the 20:30 ops sweep and the 16:30 brief share this
# exclusion because both write a card full of problem text to their own log.
REPORT_LOGS = {"ops_monitor.log", "ceo_brief.log"}

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
#
# TWO SHAPES, because our own summary lines use both word orders:
#   COUNTER FIRST  "'failed': 0"          (JSON/dict stats)
#   COUNT FIRST    "0 failed",
#                  "0 window(s) failed"   (backfill completion summaries)
# The second shape is why the 2026-07-20 CEO brief cried "63 problem
# lines" — 40 of them were successful backfills announcing "0 failed".
# One optional noun may sit between the count and the word, so
# "0 window(s) failed" is scrubbed but "0 rows written, upload failed"
# is NOT (the genuine failure survives and still fires).
#
# THIRD SHAPE (2026-07-27 false alarm): macro_nightly writes its clean
# runs as '"failed": []' — an EMPTY LIST, not a zero count. Two of them
# reached the CEO brief as "problems". A non-empty list ('"failed":
# ["TCS.NS"]') still fires because only the literal empty brackets are
# scrubbed.
ZERO_STAT_PATTERNS = re.compile(
    r"(?i)(?:"
    r"['\"]?(?:failed|errors?|failures?)['\"]?\s*[:=]\s*0\b"
    r"|"
    r"['\"]?(?:failed|errors?|failures?)['\"]?\s*[:=]\s*\[\s*\]"
    r"|"
    r"\b0\s+(?:[\w()\-]+\s+)?(?:failed|failures?|errors?)\b"
    r")")


# Dhan API error codes are problems in their own right, whatever words sit
# around them (2026-09-11, ledger Issue 26: the DH-902 lines said "failure"
# and were swept, but they were buried among twelve other problem lines and
# nothing named them). Any DH-9xx code now (a) counts as a problem line and
# (b) the AUTH/DATA-ACCESS subset below is raised as a RED ALARM at the top
# of the card, outside the MAX_CARD_PROBLEMS cap.
DHAN_CODE_PATTERN = re.compile(r"\b(DH-9\d\d)\b")
AUTH_CODES = {
    "DH-901": "access token invalid or expired",
    "DH-902": ("Dhan DATA API subscription lapsed or not active — renew the "
               "plan at Dhan; a fresh token will NOT fix this"),
    "DH-903": "account or segment inactive at Dhan",
    "DH-906": "invalid token (as observed in this repo since 2026-07-09)",
}


def is_problem_line(text: str) -> bool:
    return bool(PROBLEM_PATTERNS.search(ZERO_STAT_PATTERNS.sub("", text))
                or DHAN_CODE_PATTERN.search(text))


# --------------------------------------------------------------------------
# RED ALARMS (2026-09-11, ledger Issues 26 + 27). Three conditions that the
# log sweep + heartbeats could not see, because each one leaves the logs
# looking "touched" and the counters looking clean:
#   * an AUTH / DATA-ACCESS refusal from Dhan (the desk is blind, every job
#     still "ran")
#   * ZERO-CAPTURE sessions — the 15-minute tracker wrote a line every slot
#     and every line said captured: 0 (three sessions, 09-07 → 09-09, and the
#     daily health command said all_ok=True throughout)
#   * LOW MEMORY — the e2-micro hung at ~09:55 on 09-09 with no warning
# Each is measured, never inferred; each has an injectable input.
ZERO_CAPTURE_SESSIONS = 2            # consecutive blind sessions -> RED
LOW_MEMORY_MB = 100                  # MemAvailable below this -> RED
CAPTURE_LOG = "intraday_15m.log"     # the 15-minute tracker's cron log
CAPTURE_TAIL_BYTES = 4_000_000       # enough for ~2 weeks of slots


def _read_tail(path: Path, max_bytes: int = CAPTURE_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with open(path, "r", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                   # drop the partial first line
            return f.read()
    except OSError:
        return ""


def capture_blindness(logs_dir: Path = LOGS_DIR, now: datetime = None,
                      capture_log: str = CAPTURE_LOG) -> dict:
    """Per-session capture tally from the 15-minute tracker's JSON lines.

    A SESSION is a calendar date that has at least one non-skipped capture
    line. It is BLIND when EVERY such line reports captured == 0 (a partial
    day — some slots empty, others full — is not blind; 2026-09-03 had 23
    empty slots and 30 full ones). `blind_streak` counts consecutive blind
    sessions ending at the most recent session on or before `now`.
    Returns {"sessions": {date: {"slots", "zero"}}, "blind_streak": int,
             "blind_dates": [...], "latest_session": date|None}."""
    now = now or datetime.now()
    today = now.date().isoformat()
    per_day: dict = {}
    for line in _read_tail(Path(logs_dir) / capture_log).splitlines():
        text = line.strip()
        if not text.startswith("{") or '"captured"' not in text:
            continue
        try:
            rec = json.loads(text)
        except ValueError:
            continue
        if rec.get("skipped"):
            continue
        day = str(rec.get("ts", ""))[:10]
        if len(day) != 10 or day > today:
            continue
        slot = per_day.setdefault(day, {"slots": 0, "zero": 0})
        slot["slots"] += 1
        if not rec.get("captured"):
            slot["zero"] += 1
    streak, blind_dates = 0, []
    for day in sorted(per_day, reverse=True):
        s = per_day[day]
        if s["slots"] and s["zero"] == s["slots"]:
            streak += 1
            blind_dates.append(day)
        else:
            break
    return {"sessions": per_day, "blind_streak": streak,
            "blind_dates": sorted(blind_dates),
            "latest_session": max(per_day) if per_day else None}


def auth_alarms(problems: list) -> list:
    """RED alarms for any AUTH / DATA-ACCESS code among the swept lines."""
    counts: dict = {}
    for p in problems:
        for code in DHAN_CODE_PATTERN.findall(p.get("line", "")):
            if code in AUTH_CODES:
                counts[code] = counts.get(code, 0) + int(p.get("count", 1))
    return [{"kind": "auth", "code": code, "count": n,
             "text": f"🔴 AUTH/DATA ACCESS: {code} x{n} — {AUTH_CODES[code]}"}
            for code, n in sorted(counts.items())]


def capture_alarm(blind: dict) -> dict | None:
    streak = int(blind.get("blind_streak") or 0)
    if streak <= 0:
        return None
    dates = ", ".join(blind.get("blind_dates") or [])
    if streak >= ZERO_CAPTURE_SESSIONS:
        return {"kind": "zero_capture", "streak": streak, "red": True,
                "text": (f"🔴 DATA BLIND: {streak} consecutive session(s) with "
                         f"ZERO captures ({dates}) — the desk is not seeing prices")}
    return {"kind": "zero_capture", "streak": streak, "red": False,
            "text": (f"⚠️ zero captures all session on {dates} — one more "
                     f"blind session turns this RED")}


def memory_alarm(telemetry: dict) -> dict | None:
    mb = (telemetry or {}).get("mem_available_mb")
    if mb is None or mb >= LOW_MEMORY_MB:
        return None
    return {"kind": "low_memory", "mem_available_mb": mb, "red": True,
            "text": (f"🔴 LOW MEMORY: {mb} MB available (< {LOW_MEMORY_MB} MB) — "
                     f"the VM hung at this level on 2026-09-09")}


def collect_alarms(problems: list, telemetry: dict, logs_dir: Path = LOGS_DIR,
                   now: datetime = None) -> list:
    """All red/amber alarms for one sweep, in card order. Fail-open per
    detector: a broken detector costs its own line, never the sweep."""
    alarms = []
    for fn in (lambda: auth_alarms(problems),
               lambda: [a for a in [capture_alarm(capture_blindness(logs_dir, now))] if a],
               lambda: [a for a in [memory_alarm(telemetry)] if a]):
        try:
            alarms.extend(fn())
        except Exception as e:                      # pragma: no cover - defensive
            alarms.append({"kind": "detector_error", "red": False,
                           "text": f"⚠️ an alarm detector failed open: {e}"})
    return alarms


def health_verdict(logs_dir: Path = LOGS_DIR, now: datetime = None,
                   telemetry: dict = None) -> list:
    """On-demand (stateless) read for `daily_health_and_queue.sh`: the same
    three detectors over the log TAILS, so a human running the command
    during an outage sees RED without waiting for the 20:30 sweep."""
    now = now or datetime.now()
    tail_problems = []
    for path in sorted(Path(logs_dir).glob("*.log")):
        if path.name in REPORT_LOGS:
            continue
        counts: dict = {}
        for line in _read_tail(path, 512_000).splitlines():
            for code in DHAN_CODE_PATTERN.findall(line):
                if code in AUTH_CODES:
                    counts[code] = counts.get(code, 0) + 1
        for code, n in counts.items():
            tail_problems.append({"log": path.name, "line": code, "count": n})
    telemetry = telemetry if telemetry is not None else system_telemetry()
    alarms = collect_alarms(tail_problems, telemetry, logs_dir, now)
    blind = capture_blindness(logs_dir, now)
    lines = []
    if not alarms:
        lines.append("  ✅ no auth/data-access refusals in the log tails, "
                     "no blind sessions, memory above the floor")
    for a in alarms:
        lines.append("  " + a["text"])
    latest = blind.get("latest_session")
    if latest:
        s = blind["sessions"][latest]
        lines.append(f"  last capture session {latest}: "
                     f"{s['slots'] - s['zero']}/{s['slots']} slots captured")
    lines.append("  " + telemetry_line(telemetry))
    return lines

# What should have written its log today (name -> weekdays-only flag).
# This default is the VM's schedule (the engine machine). Any deployment
# can override per-machine via OPS_EXPECTED_JOBS, a comma list of
# "name.log:flag" where flag 1/true = weekdays-only, 0 = daily — e.g.
#   OPS_EXPECTED_JOBS="renew_token.log:0,master_scheduler.log:1"
EXPECTED_JOBS = {
    "renew_token.log": False,        # daily 07:00 IST
    "sleep_phase.log": False,        # daily 20:00 IST (decay-only w/o Ollama)
    "suggest.log": True,             # Mon-Fri 08:00 IST
    "morning_brief.log": True,       # Mon-Fri 08:05 IST (Directive 2, 07-27)
    "main.log": True,                # Mon-Fri 15:35 IST
    "master_scheduler.log": True,    # Mon-Fri 09:10 IST
    "chain_archiver.log": True,      # Mon-Fri 15:40 IST (Phase-0 capture)
                                     # HEARTBEAT ONLY — see the note below on
                                     # why that was not enough on 2026-08-05.
    "deals_tracker.log": False,      # daily 19:30 IST (EOD bulk/block pull)
    "daily_archiver.log": False,     # daily 19:45 IST (perishable snapshots)
    "earnings_calendar.log": False,  # daily 19:20 IST (results dates)
    "flows_tracker.log": False,      # daily 19:35 IST (FII/DII cash flows)
    "news_processor.log": False,     # daily 19:10 IST (Gemini news sentiment)
    # rss_ingester.log REMOVED 2026-08-05 with its cron (Sequence 2).
    # A heartbeat for a job that no longer runs flags SILENT every
    # night — and health_gate turns that into a permanent block on
    # discovery/nightly, so the miner would never run even at 60
    # frames. Restore this line and the cron together, or neither.
    "corporate_events.log": False,   # daily 19:25 IST (NSE announcements
                                     # -> the corporate_risk_halt feed)
    # PERISHABLE-DATA LOSS IS NOT A MISSING HEARTBEAT (2026-08-13).
    # chain_archiver.log and flows_tracker.log were ALREADY listed here on
    # 2026-08-05, and the card still said nothing when that day's option
    # chains were lost and 08-04's flows were skipped. Both jobs RAN; both
    # exited 0. A heartbeat can only prove the cron fired, never that it
    # brought anything home.
    #
    # The fix is therefore NOT another entry in this dict — it is upstream,
    # in the two clerks, which now emit named skips worded so `sweep_logs`
    # already catches them:
    #     CA-EMPTY / CA-BLACKOUT   chain_archiver — zero chains captured
    #     FL-STALE                 flows_tracker  — source served an old
    #                              session; the lake write was refused
    # Each contains "UNAVAILABLE", which PROBLEM_PATTERNS matches, so the
    # nightly card carries them with no change to this module's logic.
    # Do not "tidy" those words out of the clerks: they ARE the wiring.
    #
    # fo_bhavcopy is deliberately ABSENT from this dict. It is MAC-ONLY (NSE
    # bot-walls datacentre IPs) and this default is the VM's schedule, so a
    # row here would flag SILENT on the VM every single night — the exact
    # trap documented above for rss_ingester, which feeds health_gate and
    # would permanently block the discovery miner. It is watched instead by
    # staleness_guard's `fo_bhavcopy_lake` + `fo_liquidity` artifacts, which
    # measure the DATA's age rather than a log's presence and therefore work
    # across the machine boundary. On the Mac itself, add it with
    #     OPS_EXPECTED_JOBS="...,fo_bhavcopy.log:1"
    "discovery_nightly.log": False,  # daily 20:20 IST (gated miner pass #76,
                                     # pre-sweep like every job here — the log
                                     # is touched even on a gate-skip, so
                                     # silence means the CRON died, not that
                                     # the gate held)
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


def sweep_logs(logs_dir: Path = LOGS_DIR, state: dict = None,
               baseline_cold_start: bool = False) -> tuple:
    """Scan every *.log for NEW problem lines since the last sweep.

    Returns (problems, new_state): problems is a list of
    {"log", "line", "count"}; new_state maps filename -> byte offset
    already examined. A truncated/rotated file (smaller than its stored
    offset) is re-read from the start rather than silently skipped.

    COLD START (no stored offset for a log). Two defensible behaviours:

      baseline_cold_start=False (default, what ops_monitor has always done)
        read the file from byte 0. On a fresh box that is a one-time replay
        of the log's whole history.

      baseline_cold_start=True
        record the current end-of-file and report nothing for that log this
        run. Reporting starts from the next sweep.

    WHY THE OPTION EXISTS (2026-07-20): the CEO brief keeps its OWN offset
    file, so its first-ever run had no offsets at all and replayed weeks of
    history as "since the last brief" — 63 lines, including a
    corporate_events crash that had already been fixed by 6d89eb4. A report
    whose headline is "since the last brief" must not open with archaeology.
    `ops_monitor` keeps the replay (its own state file is years old, and on a
    genuinely fresh box one historical dump is useful); the brief opts in.
    """
    state = dict(state or {})
    problems = []
    for path in sorted(Path(logs_dir).glob("*.log")):
        if path.name in REPORT_LOGS:
            continue  # never scan a report's own output — its card quotes
                      # problem lines, which would re-match every sweep
                      # (see REPORT_LOGS)
        if baseline_cold_start and path.name not in state:
            try:
                state[path.name] = path.stat().st_size
            except OSError:
                state[path.name] = 0
            continue
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
               telemetry: dict = None, stale: dict = None,
               alarms: list = None) -> str:
    """The terse nightly health card.

    `stale` is `staleness_guard.alert_payload(...)` — None (the default, and
    the value on any clean scan) leaves this card BYTE-IDENTICAL to before the
    guard existed. A stale artifact is never folded into the problem count: a
    log line that says "failed" and a file that quietly stopped updating are
    different diseases and the card must not blur them.

    `alarms` (2026-09-11) are the RED conditions from `collect_alarms`. Any
    alarm forbids the ✅ card and is printed FIRST, above the problem cap —
    "all OK" during a data outage is the failure this line exists to end."""
    total = sum(p["count"] for p in problems)
    alarms = alarms or []
    if not problems and not missing and not alarms:
        card = (f"✅ **Ops sweep {when}** — all jobs ran, "
                "no problem lines in any log.")
        if stale:
            card += "\n" + stale["text"]
        if telemetry:
            card += "\n" + telemetry_line(telemetry)
        return card
    red = sum(1 for a in alarms if a.get("red", True))
    head = "🚨" if red else "🩺"
    lines = [f"{head} **Ops sweep {when}** — "
             + (f"{red} RED alarm(s), " if red else "")
             + f"{total} problem line(s), {len(missing)} silent job(s):"]
    for a in alarms:
        lines.append(f"• {a['text']}")
    for m in missing:
        lines.append(f"• ⏰ {m}")
    for p in problems[:MAX_CARD_PROBLEMS]:
        n = f" x{p['count']}" if p["count"] > 1 else ""
        lines.append(f"• `{p['log']}`{n}: {p['line'][:120]}")
    if len(problems) > MAX_CARD_PROBLEMS:
        lines.append(f"…and {len(problems) - MAX_CARD_PROBLEMS} more — "
                     "see logs/problems.jsonl")
    if stale:
        lines.append(stale["text"])
    if telemetry:
        lines.append(telemetry_line(telemetry))
    return "\n".join(lines)


def run_sweep(logs_dir: Path = LOGS_DIR, state_path: Path = STATE_PATH,
              problems_path: Path = PROBLEMS_PATH, now: datetime = None,
              notify_fn=None, staleness_root=None) -> dict:
    """The full nightly pass. Returns a summary dict (also printed).

    `staleness_root` is the guard's data root — None means the real repo, and
    tests pass a tempdir so no assertion here ever depends on the mtime of a
    file in `data/`. (Four separate regressions in this repo have come from a
    new default that reaches live state from inside pytest; this is the seam
    that stops the fifth.)"""
    now = now or datetime.now()
    when = now.strftime("%Y-%m-%d %H:%M")
    problems, new_state = sweep_logs(logs_dir, _load_state(state_path))
    missing = check_heartbeats(logs_dir, now)
    record_problems(problems, when, problems_path)
    _save_state(state_path, new_state)

    # Data-freshness sweep (2026-08-05). Fail-open on the REPORT — a broken
    # guard costs its own card section, never the ops sweep. (The guard's own
    # per-artifact verdicts fail SAFE in the other direction; see its docstring.)
    stale = None
    stale_verdicts = []
    try:
        from src import staleness_guard
        stale_verdicts = staleness_guard.scan(now=now, root=staleness_root)
        stale = staleness_guard.alert_payload(stale_verdicts)
    except Exception as e:
        print(f"  (staleness scan skipped — failing open: {e})")

    telemetry = system_telemetry()
    alarms = collect_alarms(problems, telemetry, logs_dir, now)
    card = build_card(problems, missing, when, telemetry=telemetry, stale=stale,
                      alarms=alarms)
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
            "stale_artifacts": (stale or {}).get("count", 0),
            "disabled_components": (stale or {}).get("disabled", 0),
            "stale_names": (stale or {}).get("names", []),
            "telemetry": telemetry,
            "alarms": [a["kind"] for a in alarms],
            "red_alarms": sum(1 for a in alarms if a.get("red", True)),
            "auth_failures": sum(a["count"] for a in alarms if a["kind"] == "auth"),
            "blind_sessions": next((a["streak"] for a in alarms
                                    if a["kind"] == "zero_capture"), 0),
            "low_memory": any(a["kind"] == "low_memory" for a in alarms)}


if __name__ == "__main__":
    import sys
    if "--verdict" in sys.argv[1:]:
        # Stateless read for the daily health command — no card, no state
        # file, no ledger write. Exit 2 on a RED alarm so a shell can see it.
        verdict = health_verdict()
        print("\n".join(verdict))
        sys.exit(2 if any("🔴" in v for v in verdict) else 0)
    summary = run_sweep()
    print(json.dumps(summary))

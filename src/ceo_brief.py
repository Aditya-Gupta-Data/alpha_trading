"""
src/ceo_brief.py — The Daily CEO Brief (Department 6, Reporting & Advisory)
===========================================================================

WHAT IT DOES (plain English): once a day, after the close, it answers the
four questions the owner actually asks — without reading a single log file:

  1. OPERATIONS & HEALTH  — did everything run? (scheduled-job heartbeats)
  2. ISSUES & RESOLUTIONS — what broke? (Dhan throttles, dead tickers,
     margin rejections, everything else), bucketed by kind.
  3. DEPLOYMENTS & WORK   — what code is live, and what got built today?
  4. RISK & CAPITAL       — open exposure and realized P&L, one line.

INPUTS  (all read-only, all injectable — nothing here fetches or trades):
  logs/*.log                 via ops_monitor.sweep_logs   (issues)
  logs/*.log mtimes          via ops_monitor.check_heartbeats (health)
  logs/deploy_log.jsonl      via deploy_log's record       (what's live)
  git log                    (today's local commits)
  data/journal.jsonl         via eod_summary               (risk & capital)

OUTPUT: ONE Discord card through `notifier.fire_broadcast` — the single
Department-6 door (#non-negotiable: every card from every department leaves
through `fire_broadcast`). This module never posts to Discord itself.

MANAGER SEAM: `build_brief_card()` builds the payload; `send_brief()` hands
it to the notifier. Everything else is a pure collector you can call and
assert on offline.

THREE DESIGN CONSTRAINTS worth knowing before you change this:

  * IT STEALS NOTHING. `ops_monitor.sweep_logs` is INCREMENTAL — it reports
    each problem line exactly once and remembers a byte offset per log. If
    this module used ops_monitor's state file, every issue it reported at
    16:00 would be MISSING from the 20:30 nightly ops card. So the brief
    keeps its OWN offset file (`logs/.ceo_brief_state.json`) and never calls
    `record_problems` — `ops_monitor` remains the sole owner of the state
    file and of `logs/problems.jsonl`. The two jobs read the same logs
    independently and neither blinds the other.

  * IT COMPUTES NO MONEY. Every P&L / exposure number is reused from
    `eod_summary` — the module that already owns that computation for the
    15:30 card. Two reports that each compute "today's P&L" their own way
    is how two reports start disagreeing. If a number here looks wrong, fix
    it in `eod_summary` and both cards move together.

  * IT IS MACHINE-HONEST. `logs/deploy_log.jsonl` and `git log` describe THE
    BOX THIS RUNS ON. On the VM that is the production record (the intended
    home, per the cron below). On the Mac it describes the Mac. The card
    stamps the hostname so a brief can never silently claim the Mac's HEAD
    is what's live in production.

Fail-open per section: any collector that raises degrades to an honest
"unavailable" field — a broken log parser must never cost the owner the
P&L line. Read-only: writes nothing but its own sweep offset.

CRON — entry #19 in `scripts/setup_cron.sh`, Mon-Fri 16:30 IST. Installed by
re-running that script ON THE VM (it is idempotent and asserts the host clock
is +0530; ledger Issue 1 — Debian cron silently ignores CRON_TZ).

    30 16 * * 1-5  cd $REPO_ROOT && $PYTHON_BIN -m src.ceo_brief \
                   >> $REPO_ROOT/logs/ceo_brief.log 2>&1

WHY 16:30 AND NOT 16:00 (the owner's first suggestion): `main` runs 15:35 and
`chain_archiver` 15:40, so at 16:00 both are still inside the 30-min grace and
the brief could judge only 3 of the 13 jobs. 16:30 is the earliest slot that
covers every post-close job. It stays before the 18:50-20:30 evening block by
design — those are reported as "not due yet" and judged by the 20:30 ops sweep.

WHY NOT FOLD THIS INTO `eod_summary` (15:30): that card is Department 3/4's
P&L at the close and is a different altitude and audience. The brief is the
cross-department status roll-up and needs the post-close jobs to have run.

NOT YET SCHEDULED: staged locally only — the VM stays on 6d89eb4 for the
weekend hold, so nothing is installed there until the owner lifts it.

DEPLOY-TIME STEP, deliberately not done here: add `"ceo_brief.log": True` to
`ops_monitor.EXPECTED_JOBS` (and the matching hour to JOB_DUE_HOUR) so the
brief is heartbeat-monitored like every other job. It must land in the SAME
change as the cron install — add it earlier and the 20:30 card cries "ceo_brief
did not run" every night until the cron exists. `test_due_filter_covers_every_
monitored_job` fails loudly if the two maps are updated out of step.

CLI:  python3 -m src.ceo_brief            (build + send)
      python3 -m src.ceo_brief --dry-run  (print the card, send nothing)
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

# Our OWN sweep offset — deliberately NOT ops_monitor's STATE_PATH. See the
# "steals nothing" constraint in the header.
STATE_PATH = LOGS_DIR / ".ceo_brief_state.json"
DEPLOY_LOG_PATH = LOGS_DIR / "deploy_log.jsonl"

MAX_ISSUE_LINES = 6        # per bucket, on the card
MAX_LINE_CHARS = 110

# When each monitored job is DUE (IST hour, from scripts/setup_cron.sh).
#
# WHY THIS EXISTS: `ops_monitor.check_heartbeats` asks "did this log get
# touched TODAY?" — correct at 20:30, when every job has had its slot. At
# 16:00 it is nonsense: 8 of the 13 jobs run between 18:50 and 20:30, so the
# brief would report them "silent" EVERY SINGLE DAY. A card that cries wolf
# daily is a card the owner stops reading, which costs more than no card.
# So the brief only asks about jobs whose slot has already passed, and says
# how many it is holding back.
#
# DUPLICATION, ACKNOWLEDGED: the schedule's truth is `scripts/setup_cron.sh`;
# this mirrors it, so the two drift if someone edits one alone. The honest
# fix is for `ops_monitor.EXPECTED_JOBS` to carry each job's hour instead of
# only a weekdays-only flag — then both jobs read ONE schedule. That is a
# Department-6 manager change; flagged for Fable, deliberately not done here
# mid-freeze. `_unknown_jobs()` fails LOUD (reports the job) if a name here
# and in ops_monitor ever diverge, so drift surfaces on the card.
JOB_DUE_HOUR = {
    "renew_token.log": 7.0,
    "suggest.log": 8.0,
    "master_scheduler.log": 9.2,
    "main.log": 15.6,
    "chain_archiver.log": 15.7,
    "rss_ingester.log": 18.8,
    "news_processor.log": 19.2,
    "earnings_calendar.log": 19.3,
    "deals_tracker.log": 19.5,
    "flows_tracker.log": 19.6,
    "daily_archiver.log": 19.75,
    "sleep_phase.log": 20.0,
    "discovery_nightly.log": 20.3,
}
DUE_GRACE_HOURS = 0.5      # a job gets half an hour to actually write its log

# Issue buckets — the owner's named categories. MOST SPECIFIC FIRST: the
# first pattern that matches wins, so "Dhan quote error for LTIM: scrip
# master miss" is a dead ticker, not a generic Dhan error.
#
# These classify lines that `ops_monitor.sweep_logs` ALREADY flagged as
# problem-shaped. They cannot widen that net — a line ops_monitor doesn't
# consider a problem never reaches this function. See THROTTLE_CAVEAT.
ISSUE_BUCKETS = (
    ("Dead / unknown ticker", re.compile(
        r"(?i)(scrip[\s-]?master|security[\s_-]?id|delisted|no nse listing|"
        r"unknown (?:ticker|symbol|security)|not (?:found|listed) in scrip|"
        r"demerg)")),
    ("Margin / capital", re.compile(
        r"(?i)(margin|insufficient (?:funds|capital)|capital pool|"
        r"exposure gate|risk-of-ruin|drawdown halt|circuit breaker)")),
    ("Token / auth", re.compile(
        r"(?i)(token|\bauth|\b401\b|\b403\b|unauthor|expired)")),
    ("Dhan API / data", re.compile(
        r"(?i)(dhan|dh-\d{3}|rate[\s-]?limit|throttl|too many requests|"
        r"\b429\b|quota exceeded)")),
)
OTHER_BUCKET = "Other errors"

# HONESTY NOTE, carried onto the card. `dhan_client._throttle()` paces calls
# by SLEEPING — it prints nothing, so a throttle leaves no log line and this
# brief cannot count throttles. The Dhan bucket therefore reports Dhan API
# *errors* (which do print), not pacing events. Saying "no throttles today"
# would be a lie dressed as a green tick; the card says this instead.
# FOLLOW-UP (needs Fable + a Dept-1 change, deliberately NOT done here mid-
# freeze): have `_throttle()` count its own sleeps and expose the daily total.
THROTTLE_CAVEAT = ("Throttle pacing is silent by design (`_throttle()` sleeps "
                   "without logging) — Dhan pacing events are NOT counted here.")


def _now(clock=None) -> datetime:
    return clock() if clock else datetime.now(IST)


def _today(clock=None) -> str:
    return _now(clock).date().isoformat()


def _truncate(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Flatten to ONE line and clip. For log lines, never whole fields."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip(text: str, limit: int) -> str:
    """Clip a MULTI-LINE field body, keeping its line breaks.

    `_truncate` collapses all whitespace, which is right for one log line and
    catastrophic for an assembled field: on 2026-07-20 it ran the whole Issues
    section into a single unreadable paragraph. Discord caps a field value at
    1024 chars, so clipping still has to happen — it just must not flatten.
    Clips on a line boundary so the card never ends mid-word.
    """
    text = str(text)
    if len(text) <= limit:
        return text
    notice = "_…trimmed — full detail in the ops ledger._"
    budget = limit - len(notice) - 1          # -1 for the notice's own newline
    kept, used = [], 0
    for line in text.split("\n"):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(notice)
    return "\n".join(kept)


def _human_time(ts: str) -> str:
    """'2026-07-20T15:33:04+05:30' -> '20 Jul, 3:33 PM'. Raw text if unparsable."""
    raw = str(ts or "").strip()
    try:
        return datetime.fromisoformat(raw).strftime("%-d %b, %-I:%M %p")
    except Exception:
        return raw[:16] or "time unknown"


# Conventional-commit prefixes are for the git log, not for the owner's card.
_COMMIT_PREFIX = re.compile(r"^(feat|fix|docs|chore|refactor|test|perf|build|"
                            r"ci|style|revert)(\([^)]*\))?!?:\s*")


def _plain_subject(subject: str, limit: int = 80) -> str:
    """Strip the 'feat(analysis): ' machine prefix and sentence-case the rest."""
    text = _COMMIT_PREFIX.sub("", " ".join(str(subject or "").split()))
    if text:
        text = text[0].upper() + text[1:]
    return _truncate(text, limit)


# Plain-English readings of the log shapes this system actually emits. The
# owner does not read Python tracebacks or JSON stats dicts, so the card says
# what HAPPENED; the verbatim line stays in `logs/problems.jsonl`.
#
# HONESTY RULE for anything added here: a reading may only restate what the
# line already says. It must never diagnose a cause the log did not state —
# a confident wrong summary is worse than a raw line the owner can grep.
DHAN_ERROR_CODES = {
    "DH-901": "the access token is invalid or expired",
    "DH-902": "the authentication is not valid",
    "DH-903": "the login failed",
    "DH-904": "too many requests — rate limited",
    "DH-905": "the request itself was malformed (bad symbol or date range)",
    "DH-906": "the data is not available for that instrument",
}


def humanize_issue(line: str) -> str:
    """Turn one raw log line into a sentence the owner can act on.

    Falls back to the cleaned-up raw line whenever the shape is unrecognized —
    an unreadable truth beats a readable guess.
    """
    raw = " ".join(str(line or "").split()).strip().strip("()")

    m = re.search(r"(?i)error:\s*unrecognized arguments:\s*(.+)$", raw)
    if m:
        script = re.match(r"([\w./]+\.py)", raw)
        who = script.group(1) if script else "a script"
        return (f"{who} was started with options it does not accept "
                f"({_truncate(m.group(1), 60)}) — that run did nothing.")

    m = re.search(r"(?i)\b(DH-\d{3})\b", raw)
    if m:
        code = m.group(1).upper()
        meaning = DHAN_ERROR_CODES.get(code, "the broker API refused the call")
        what = "a historical-data request" if re.search(
            r"(?i)historical", raw) else "a request"
        return f"Dhan rejected {what} — {meaning} ({code})."

    m = re.search(r'(?i)"captured":\s*(\d+).*?"failed":\s*(\d+)'
                  r'.*?"tickers":\s*(\d+)', raw)
    if m:
        got, bad, total = m.group(1), m.group(2), m.group(3)
        return (f"Intraday capture got {got} of {total} tickers — "
                f"{bad} failed. Partial data for that slot.")

    if re.search(r"(?i)fall(?:ing|s|en)?[\s-]open", raw):
        what = "A live feed"
        m = re.match(r"(?i)([\w\s]{3,30}?):", raw)
        if m:
            what = m.group(1).strip().capitalize()
        reason = "timed out" if re.search(r"(?i)tim(?:e|ed)\s*out", raw) \
            else "was unreachable"
        return (f"{what} {reason} — the system fell back to its saved "
                f"snapshot, so this data is stale rather than missing.")

    return _truncate(raw, MAX_LINE_CHARS)


# --------------------------------------------------------------------------
# 1. OPERATIONS & HEALTH
# --------------------------------------------------------------------------

def jobs_due_by(now: datetime, jobs: dict,
                due_hours: dict = None) -> tuple[dict, list]:
    """Split monitored jobs into (due by `now`, not yet due).

    A job with no entry in JOB_DUE_HOUR is treated as DUE — an unknown job is
    reported rather than silently excused, so schedule drift shows up on the
    card instead of hiding a dead cron.
    """
    due_hours = JOB_DUE_HOUR if due_hours is None else due_hours
    hour_now = now.hour + now.minute / 60.0
    due, pending = {}, []
    for name, weekdays_only in jobs.items():
        slot = due_hours.get(name)
        if slot is not None and hour_now < slot + DUE_GRACE_HOURS:
            pending.append(name)
        else:
            due[name] = weekdays_only
    return due, pending


def collect_operations(logs_dir: Path = LOGS_DIR, clock=None,
                       expected: dict = None, due_hours: dict = None) -> dict:
    """Which scheduled jobs that were DUE by now touched their log today?

    Heartbeat freshness is `ops_monitor.check_heartbeats` (weekday-aware,
    honors OPS_EXPECTED_JOBS per machine) — that definition lives there and is
    not duplicated. What this adds is the time filter: see JOB_DUE_HOUR for
    why a 16:00 card must not ask about a 20:00 job.

    Returns {"missing", "expected_count", "pending", "ok", "available"}.
    """
    try:
        from src.ops_monitor import (EXPECTED_JOBS, _expected_jobs_from_env,
                                     check_heartbeats)
        jobs = expected or _expected_jobs_from_env() or EXPECTED_JOBS
        now = _now(clock)
        due, pending = jobs_due_by(now, jobs, due_hours)
        # check_heartbeats compares against local-date mtimes; hand it a naive
        # local clock so a tz-aware IST clock can't shift the day boundary.
        missing = check_heartbeats(logs_dir=Path(logs_dir),
                                   now=now.replace(tzinfo=None),
                                   expected=due)
        return {"missing": list(missing), "expected_count": len(due),
                "pending": pending, "ok": not missing, "available": True}
    except Exception as exc:
        return {"missing": [], "expected_count": 0, "pending": [], "ok": False,
                "available": False, "error": str(exc)}


# --------------------------------------------------------------------------
# 2. ISSUES & RESOLUTIONS
# --------------------------------------------------------------------------

def bucket_issue(line: str) -> str:
    """Classify one problem line into an owner-facing category."""
    for name, pattern in ISSUE_BUCKETS:
        if pattern.search(line or ""):
            return name
    return OTHER_BUCKET


def collect_issues(logs_dir: Path = LOGS_DIR,
                   state_path: Path = STATE_PATH) -> dict:
    """Problem lines logged since the last brief, bucketed by kind.

    Uses `ops_monitor.sweep_logs` (the problem-line vocabulary lives there)
    but with THIS module's own offset state, so the 20:30 ops card still sees
    every line. Advancing our offset is the only write this module performs.

    Returns {"buckets": {name: [{"log","line","count"}]}, "total": int,
             "available": bool}.
    """
    try:
        from src.ops_monitor import sweep_logs
        # COLD START is "we have never run here" — the FILE is absent. It is
        # NOT "the file is unreadable": a corrupt offset file must fall back
        # to a full re-read, because silently baselining past a corruption
        # would swallow real problems (test_issues_corrupt_state_file_
        # recovers pins this). Missing = baseline; corrupt = replay.
        cold = not Path(state_path).exists()
        state = {}
        if not cold:
            try:
                state = json.loads(Path(state_path).read_text())
            except Exception:
                state = {}   # corrupt offset file — re-read fully, don't skip
        # See sweep_logs: cold start records EOF rather than replaying weeks
        # of history. `cold` rides onto the card so a quiet first brief reads
        # as "the ledger starts here", never as "nothing is wrong".
        problems, new_state = sweep_logs(Path(logs_dir), state,
                                         baseline_cold_start=cold)

        buckets: dict = {}
        for p in problems:
            buckets.setdefault(bucket_issue(p.get("line", "")), []).append(p)

        try:
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(json.dumps(new_state, indent=2))
        except Exception as exc:
            # A card that can't checkpoint is still a useful card; it will
            # simply re-report these lines tomorrow. Say so rather than fail.
            print(f"  (ceo_brief: could not save sweep offset: {exc})")

        return {"buckets": buckets,
                "total": sum(p.get("count", 1) for p in problems),
                "cold_start": cold,
                "available": True}
    except Exception as exc:
        return {"buckets": {}, "total": 0, "cold_start": False,
                "available": False, "error": str(exc)}


# --------------------------------------------------------------------------
# 3. DEPLOYMENTS & WORK
# --------------------------------------------------------------------------

# Entries written BEFORE deploy_log gained its `kind` field carry no marker,
# so short-lived jobs have to be recognised by name for the existing log.
# New entries carry kind="job" and need no entry here.
KNOWN_SCHEDULED_JOBS = {"master_scheduler"}


def _is_scheduled_job(name: str, entry: dict) -> bool:
    """Does this deploy-log entry describe a process that has already exited?

    Prefers the explicit `kind` written since 2026-07-20; falls back to the
    name list for entries logged before the field existed.
    """
    kind = (entry or {}).get("kind")
    if kind:
        return kind == "job"
    return name in KNOWN_SCHEDULED_JOBS


def live_version(log_path: Path = DEPLOY_LOG_PATH) -> dict:
    """What code the long-running services on THIS box came up on.

    Reads `logs/deploy_log.jsonl` — written by the services themselves at
    startup (never by a human remembering), so it is the trustworthy answer
    to "what was actually running?". Returns the most recent entry per
    service plus a `consistent` flag: two services on different shas means a
    half-finished deploy, which is exactly the state worth shouting about.
    """
    out = {"services": {}, "jobs": {}, "consistent": True, "available": False,
           "dirty": False}
    try:
        latest: dict = {}
        with open(Path(log_path), encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue   # junk-tolerant: a torn line is not an outage
                svc = entry.get("service")
                if svc:
                    latest[svc] = entry
        if not latest:
            return out
        services = {n: e for n, e in latest.items() if not _is_scheduled_job(n, e)}
        jobs = {n: e for n, e in latest.items() if _is_scheduled_job(n, e)}
        # CONSISTENCY IS A LONG-RUNNING-SERVICE QUESTION ONLY. A scheduled job
        # has already exited by brief time; its sha is history, not something
        # "live" that could disagree with anything. Including it made the
        # 2026-07-20 card shout "half-finished deploy" because
        # master_scheduler had started at 09:10 and the two real services
        # restarted at 15:33 — which is just a normal day with a commit in it.
        shas = {e.get("sha") for e in services.values()}
        out["services"] = services
        out["jobs"] = jobs
        out["consistent"] = len(shas) <= 1
        out["dirty"] = any(e.get("dirty") for e in latest.values())
        out["available"] = True
        return out
    except OSError:
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out


def todays_commits(repo_root: Path = ROOT, clock=None) -> dict:
    """Commits authored on this box today — "what got built".

    Degrades to unavailable rather than raising (mirrors deploy_log._git_state:
    git may be absent, or this may be an export rather than a checkout).
    """
    out = {"commits": [], "head": "unknown", "dirty": False, "available": False}
    try:
        today = _today(clock)
        res = subprocess.run(
            ["git", "-C", str(repo_root), "log",
             f"--since={today} 00:00", f"--until={today} 23:59",
             "--format=%h\x1f%s"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            for line in res.stdout.strip().splitlines():
                if "\x1f" in line:
                    sha, subject = line.split("\x1f", 1)
                    out["commits"].append({"sha": sha, "subject": subject})
        head = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%h"],
            capture_output=True, text=True, timeout=5,
        )
        if head.returncode == 0 and head.stdout.strip():
            out["head"] = head.stdout.strip()
        porcelain = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if porcelain.returncode == 0:
            out["dirty"] = bool(porcelain.stdout.strip())
        out["available"] = True
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out


def collect_deployments(log_path: Path = DEPLOY_LOG_PATH,
                        repo_root: Path = ROOT, clock=None) -> dict:
    """Section 3: what's live on this box + what was built here today."""
    return {"live": live_version(log_path),
            "work": todays_commits(repo_root, clock),
            "host": _hostname()}


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


# --------------------------------------------------------------------------
# 4. RISK & CAPITAL
# --------------------------------------------------------------------------

def collect_risk(journal_path=None) -> dict:
    """Open exposure + realized P&L for today.

    Every number is REUSED from `eod_summary`, which already owns this
    computation for the 15:30 card — see the "computes no money" constraint.
    NOTE for the Fable review: the three journal readers below are currently
    underscore-private in `eod_summary`; they want promoting to a public
    read API (`open_positions()` / `todays_exits()`) so this reuse is a
    supported seam rather than a reach-in.

    Returns {"daily_pnl", "resolved", "open_spreads", "open_equities",
             "net_delta", "available"}.
    """
    try:
        from src import eod_summary
        entries = eod_summary._read_journal(journal_path)
        today = eod_summary._today()

        todays_exits = [
            e for e in entries
            if (e.get("outcome") or {}).get("exit_date") == today
            and e.get("decision") == "approved"
        ]
        open_spreads = eod_summary._open_approved_spreads(entries)
        open_equities = eod_summary._open_approved_equities(entries)
        daily_pnl = sum(
            float((e.get("outcome") or {}).get("pnl_rs") or 0.0)
            for e in todays_exits
        )
        return {
            "daily_pnl": daily_pnl,
            "resolved": len(todays_exits),
            "open_spreads": len(open_spreads),
            "open_equities": len(open_equities),
            "net_delta": eod_summary.compute_net_delta_exposure(open_spreads),
            "available": True,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------

def _ops_field(ops: dict) -> dict:
    if not ops.get("available"):
        return {"name": "🩺 Operations",
                "value": "Health unavailable — heartbeat check failed.",
                "inline": False}
    pending = ops.get("pending") or []
    later = (f"\n_{len(pending)} evening job(s) not due yet — "
             f"the 20:30 ops sweep covers those._" if pending else "")
    if ops["ok"]:
        return {"name": "🩺 Operations",
                "value": f"✅ All {ops['expected_count']} jobs due by now ran."
                         + later,
                "inline": False}
    lines = [f"⏰ {len(ops['missing'])} of {ops['expected_count']} due job(s) silent:"]
    lines += [f"• {m}" for m in ops["missing"][:MAX_ISSUE_LINES]]
    if len(ops["missing"]) > MAX_ISSUE_LINES:
        lines.append(f"…and {len(ops['missing']) - MAX_ISSUE_LINES} more")
    return {"name": "🩺 Operations", "value": "\n".join(lines) + later,
            "inline": False}


def _issues_field(issues: dict) -> dict:
    if not issues.get("available"):
        return {"name": "🐛 Issues",
                "value": "Log sweep unavailable.", "inline": False}
    if issues.get("cold_start"):
        return {"name": "🐛 Issues",
                "value": "📍 First brief on this box — the issue ledger starts "
                         "from here.\nOlder log history was deliberately not "
                         "replayed (it would report bugs already fixed weeks "
                         "ago as if they happened today). Reporting begins "
                         "with the next brief.\n"
                         f"_{THROTTLE_CAVEAT}_",
                "inline": False}
    if not issues["total"]:
        return {"name": "🐛 Issues",
                "value": "✅ No problem lines in any log since the last brief.\n"
                         f"_{THROTTLE_CAVEAT}_",
                "inline": False}
    distinct = sum(len(items) for items in issues["buckets"].values())
    lines = [f"**{issues['total']}** problem line(s) since the last brief, "
             f"in **{distinct}** distinct issue(s):", ""]
    for name, items in issues["buckets"].items():
        count = sum(i.get("count", 1) for i in items)
        lines.append(f"__{name}__ — {count} line(s)")
        for item in items[:MAX_ISSUE_LINES]:
            times = item.get("count", 1)
            n = f" _(happened {times} times)_" if times > 1 else ""
            lines.append(f"• {humanize_issue(item.get('line', ''))}{n}")
            lines.append(f"  ⤷ from `{item.get('log', '?')}`")
        if len(items) > MAX_ISSUE_LINES:
            lines.append(f"• …and {len(items) - MAX_ISSUE_LINES} more of these")
        lines.append("")
    lines.append("_Verbatim log text lives in `logs/problems.jsonl`. "
                 "Resolutions are the owner's call._")
    lines.append(f"_{THROTTLE_CAVEAT}_")
    return {"name": "🐛 Issues", "value": _clip("\n".join(lines), 1000),
            "inline": False}


def _deploy_field(dep: dict) -> dict:
    live = dep.get("live", {})
    work = dep.get("work", {})
    host = dep.get("host", "?")
    lines = []

    if live.get("available"):
        for svc, entry in sorted(live.get("services", {}).items()):
            flag = " ⚠️ running uncommitted edits" if entry.get("dirty") else ""
            lines.append(f"• **{svc}** — running `{entry.get('sha', '?')}`, "
                         f"{entry.get('event', 'started')} "
                         f"{_human_time(entry.get('ts', ''))}{flag}")
        if not live.get("consistent"):
            lines.append("⚠️ **These are not all the same version — a deploy "
                         "was left half-finished.** The older service is still "
                         "running yesterday's behaviour.")
        # Scheduled jobs are reported, never sha-compared: they have already
        # exited, so "which version" is history, not a live inconsistency.
        for job, entry in sorted(live.get("jobs", {}).items()):
            lines.append(f"• **{job}** _(scheduled job, already finished)_ — "
                         f"last ran `{entry.get('sha', '?')}` at "
                         f"{_human_time(entry.get('ts', ''))}")
    else:
        lines.append("• No deploy record on this box "
                     "(services log their own startup — none has come up here).")

    if work.get("available"):
        commits = work["commits"]
        if commits:
            lines.append(f"**Built today — {len(commits)} change(s):**")
            for c in commits[:MAX_ISSUE_LINES]:
                lines.append(f"• {_plain_subject(c['subject'])} "
                             f"_(`{c['sha']}`)_")
            if len(commits) > MAX_ISSUE_LINES:
                lines.append(f"• …and {len(commits) - MAX_ISSUE_LINES} more")
        else:
            lines.append("**Built today:** nothing committed.")
        if work.get("dirty"):
            lines.append("⚠️ There are edits here that have not been "
                         "committed yet.")
    else:
        lines.append("**Built today:** git unavailable.")

    lines.append(f"_Reported from `{host}` — this box's record only._")
    return {"name": "🚀 Deployments & Work", "value": "\n".join(lines),
            "inline": False}


def _risk_field(risk: dict) -> dict:
    if not risk.get("available"):
        return {"name": "💰 Risk & Capital",
                "value": "Journal unavailable — no P&L reported.",
                "inline": False}
    pnl = risk["daily_pnl"]
    sign = "+" if pnl >= 0 else ""
    delta = risk["net_delta"]
    if delta > 0:
        bias = f"leaning LONG — it profits if the market rises ({delta:+.1f})"
    elif delta < 0:
        bias = f"leaning SHORT — it profits if the market falls ({delta:+.1f})"
    else:
        bias = "market-neutral — direction doesn't move it much (0)"
    total_open = risk["open_spreads"] + risk["open_equities"]

    if risk["resolved"]:
        booked = (f"Booked today: **Rs.{sign}{pnl:,.0f}** from "
                  f"{risk['resolved']} position(s) that closed.")
    else:
        booked = ("Nothing closed today, so there is no profit or loss to "
                  "book yet.")
    value = (f"{booked}\n"
             f"Still open: **{total_open}** position(s) "
             f"({risk['open_spreads']} option spread(s), "
             f"{risk['open_equities']} share position(s)).\n"
             f"Overall the book is {bias}.")
    return {"name": "💰 Risk & Capital", "value": value, "inline": False}


def build_brief_card(logs_dir: Path = LOGS_DIR,
                     state_path: Path = STATE_PATH,
                     deploy_log_path: Path = DEPLOY_LOG_PATH,
                     repo_root: Path = ROOT,
                     journal_path=None,
                     clock=None) -> dict:
    """The whole brief as ONE notifier payload (event="ceo_brief").

    Every seam is a parameter so the entire card is assertable offline. Each
    section fails open independently: a broken collector costs its own field,
    never the card.
    """
    ops = collect_operations(logs_dir=logs_dir, clock=clock)
    issues = collect_issues(logs_dir=logs_dir, state_path=state_path)
    dep = collect_deployments(log_path=deploy_log_path, repo_root=repo_root,
                              clock=clock)
    risk = collect_risk(journal_path=journal_path)

    # A cold start is NOT a clean day — we baselined instead of looking, and
    # the card must never bank credit for a check it did not perform.
    if issues.get("cold_start"):
        description = ("📍 First brief on this box — issue reporting starts "
                       "from here.")
    elif ops.get("ok") and not issues.get("total"):
        description = "✅ Clean day — everything ran, nothing broke."
    else:
        description = "⚠️ Attention needed — see the sections below."

    return {
        "event": "ceo_brief",
        "ticker": "",
        "date": _today(clock),
        "description": description,
        "fields": [_ops_field(ops), _issues_field(issues),
                   _deploy_field(dep), _risk_field(risk)],
    }


def send_brief(**kwargs) -> dict:
    """Build the card and hand it to the Department-6 manager.

    `fire_broadcast` is the ONE Discord door and never raises (a Discord
    outage must not take the cron down), so this returns the payload for the
    caller/log rather than a delivery boolean.
    """
    payload = build_brief_card(**kwargs)
    from src.notifier import fire_broadcast
    fire_broadcast(payload)
    return payload


def _render_text(payload: dict) -> str:
    """Terminal rendering of the card (--dry-run, and the cron log)."""
    out = [f"CEO Brief — {payload['date']}", payload["description"], ""]
    for f in payload["fields"]:
        out.append(f"{f['name']}\n{f['value']}\n")
    return "\n".join(out)


def main(argv=None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    payload = build_brief_card() if dry else send_brief()
    print(_render_text(payload), flush=True)
    if dry:
        print("(dry run — nothing sent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

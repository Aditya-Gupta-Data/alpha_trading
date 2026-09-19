#!/usr/bin/env python3
# MANUAL OFFLINE TOOL — not on any cron. Run by hand on the box whose logs
# you want audited (the VM holds the live logs; a Mac clone has the pulled
# artifacts only). Reads files, writes ONE markdown report. Touches no
# ledger, no token, no network. Never raises: a source that cannot be read
# becomes an INFO row saying so.
"""
scripts/audit_logs.py — the 15-day FORENSIC LOG SWEEP (silent-failure audit)
============================================================================

    python3 scripts/audit_logs.py                 # last 15 days -> logs/audit_report_15day.md
    python3 scripts/audit_logs.py --days 7 --stdout
    python3 scripts/audit_logs.py --root /path/to/alpha_trading --out /tmp/audit.md

WHAT IT HUNTS (architect directive 2026-09-19), each finding graded
CRITICAL / WARNING / INFO, each with its source file, date and the evidence
line, so a reader can go and look:

  1. API TIMEOUTS, DROPS, RATE LIMITS — every `logs/*.log` / `*.jsonl` line
     in the window matching a Dhan error code (DH-9xx), a rate-limit
     marker, a timeout, a connection drop, or an auth/token failure — even
     when the job then recovered (a retried call is still a friction).
     Auth/data-access codes (DH-901/902/903/906) are CRITICAL; a day with
     20+ API frictions is CRITICAL; anything else is WARNING.
  2. STALE-DATA TRIGGERS — the system's OWN staleness verdicts
     (`staleness_guard.scan`, current state), the live snapshot's age
     (`data/market_snapshot.json`, 3-minute rule), and every log line that
     says stale / too old / no market state / snapshot age. HONEST LIMIT:
     the journal stamps no quote timestamp on a proposal, so "a trade was
     evaluated on data older than 3 minutes" can only be proven from those
     three sources; what the logs never recorded is reported as unknown,
     not as clean.
  3. MEMORY / COMPUTE — the ops monitor's 🖥 telemetry lines (mem used %,
     MB free, swap, load) in `logs/ops_monitor.log`: two or more
     CONSECUTIVE readings past a threshold is a spike run (WARNING; three
     or more, or < 100 MB free, CRITICAL); `logs/problems.jsonl` rows that
     name memory/OOM.
  4. MISSED EXITS — every OPEN approved spread in `data/journal.jsonl`:
     past expiry with no outcome = CRITICAL (the expiry backstop failed);
     otherwise the tracker's OWN resolver (`plan_tracker._resolve_spread`,
     the same 65 % take / stop / pre-expiry rules) is replayed on OFFLINE
     bars (the macro lake's index closes for NIFTY 50 / NIFTY BANK, the
     bhavcopy lake for stocks) — a trigger that fired on a past day while
     the row is still open is a WARNING with the day named. Rows the lake
     cannot price are INFO ("cannot replay offline"), never "clean".
     Resolved rows in the window settled at `no_price_data_max_loss`, and
     rows whose venue execution recorded an error, are WARNING.

The report is regenerable runtime output (`logs/` is gitignored).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
CRITICAL, WARNING, INFO = "CRITICAL", "WARNING", "INFO"
SEV_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
MAX_LINE = 220
SNAPSHOT_MAX_AGE_S = 180            # market_snapshot.DEFAULT_MAX_AGE_SECONDS
API_CRITICAL_PER_DAY = 20
MEM_USED_PCT_HIGH = 85.0
MEM_FREE_MB_LOW = 150.0
MEM_FREE_MB_CRITICAL = 100.0
LOAD_HIGH = 2.0

# ---- patterns (the ops monitor's DH-9xx + the retry/timeout vocabulary the
# ---- the Dhan client, its guard and the NSE clerks actually print)
DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
API_PATTERNS = [
    ("dhan_code", re.compile(r"\b(DH-9\d\d)\b")),
    ("rate_limit", re.compile(r"(?i)rate[- ]?limit|too many requests|\b429\b|DH-905|throttl")),
    ("timeout", re.compile(r"(?i)timed?\s*out|timeout\b|ReadTimeout|ConnectTimeout")),
    ("connection_drop", re.compile(r"(?i)connection (reset|aborted|refused|error)|RemoteDisconnected|"
                                   r"ConnectionError|EOF occurred|Max retries exceeded|"
                                   r"temporarily unavailable|502|503|504")),
    ("auth", re.compile(r"(?i)\b401\b|token (invalid|expired)|invalid token|unauthori[sz]ed|"
                        r"DH-90[1236]")),
    ("retry", re.compile(r"(?i)\bretry(ing)?\b|second attempt|re-?tried")),
]
AUTH_CODES = {"DH-901", "DH-902", "DH-903", "DH-906"}
STALE_RE = re.compile(r"(?i)\bstale\b|too old|no market state|snapshot.{0,20}\bage\b|"
                      r"freshness|older than")
MEM_RE = re.compile(r"(?i)\bOOM\b|out of memory|MemoryError|Cannot allocate|killed process|"
                    r"low_memory")
TELEMETRY_RE = re.compile(r"🖥 mem (?P<used>[\d.?]+)% used \((?P<free>[\d.?]+)MB free\) · "
                          r"swap (?P<swap>[\d.?]+)MB · load (?P<load>[\d.?]+)")
REPORT_LOGS = {"ops_monitor.log", "ceo_brief.log", "eod_summary.log", "harness_digest.log",
               "performance.log"}   # a report quotes problem lines; scan them separately


# ------------------------------------------------------------------ helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _clip(s: str) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= MAX_LINE else s[:MAX_LINE - 1] + "…"


def _day_of(line: str, last: str | None) -> str | None:
    m = DATE_RE.search(line)
    return m.group(1) if m else last


def _in_window(day: str | None, start: date) -> bool:
    if not day:
        return True          # undated lines are kept; the report says so
    try:
        return date.fromisoformat(day) >= start
    except ValueError:
        return True


def _iter_lines(path: Path, start: date, mtime_floor: bool = True):
    """(day_or_None, line) for every line in the window. A file whose
    mtime is older than the window is skipped whole. Lines carry the
    last timestamp seen above them (a print without a date belongs to
    the run that printed it)."""
    try:
        if mtime_floor and datetime.fromtimestamp(path.stat().st_mtime).date() < start:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    last = None
    for line in text.splitlines():
        if not line.strip():
            continue
        last = _day_of(line, last)
        if _in_window(last, start):
            yield last, line


class Findings:
    def __init__(self):
        self.rows = []

    def add(self, severity, category, source, detail, day=None, count=1, evidence=""):
        self.rows.append({"severity": severity, "category": category, "source": str(source),
                          "day": day or "—", "count": int(count), "detail": detail,
                          "evidence": _clip(evidence) if evidence else ""})

    def sorted(self):
        return sorted(self.rows, key=lambda r: (SEV_ORDER[r["severity"]], r["category"],
                                                r["day"], r["source"]))


# ------------------------------------------------------------------ 1. API

def audit_api(root: Path, start: date, F: Findings) -> dict:
    logs = root / "logs"
    per_day = defaultdict(int)
    per_kind = defaultdict(int)
    if not logs.is_dir():
        F.add(INFO, "api", logs, "no logs/ directory here — run this on the VM")
        return {"per_day": {}, "per_kind": {}}
    seen = {}
    for path in sorted(list(logs.glob("*.log")) + list(logs.glob("*.jsonl"))):
        if path.name.startswith("audit_report"):
            continue
        for day, line in _iter_lines(path, start):
            kinds = [k for k, rx in API_PATTERNS if rx.search(line)]
            if not kinds or kinds == ["retry"]:
                continue        # a bare "retry" without a failure word is noise
            codes = set(re.findall(r"\bDH-9\d\d\b", line))
            sev = CRITICAL if (codes & AUTH_CODES or "auth" in kinds) else WARNING
            key = (path.name, day, sev, _clip(line)[:120])
            per_day[day or "undated"] += 1
            for k in kinds:
                per_kind[k] += 1
            if key in seen:
                seen[key]["count"] += 1
                continue
            row = {"severity": sev, "category": "api",
                   "source": path.name, "day": day or "—", "count": 1,
                   "detail": ("auth / data-access failure: " if sev == CRITICAL
                              else "API friction (") + ", ".join(kinds)
                             + ("" if sev == CRITICAL else ")")
                             + (" — recovered on retry" if "retry" in kinds else ""),
                   "evidence": _clip(line)}
            seen[key] = row
            F.rows.append(row)
    for day, n in sorted(per_day.items()):
        if n >= API_CRITICAL_PER_DAY:
            F.add(CRITICAL, "api", "logs/*", f"{n} API friction lines on one day "
                  f"(threshold {API_CRITICAL_PER_DAY}) — a burst, not a blip", day=day, count=n)
    if not per_day:
        F.add(INFO, "api", "logs/*", "no API timeout / rate-limit / drop / auth lines in the window")
    return {"per_day": dict(per_day), "per_kind": dict(per_kind)}


# ------------------------------------------------------------------ 2. stale data

def audit_stale(root: Path, start: date, F: Findings, now: datetime) -> None:
    # (a) the system's own freshness verdicts — current state
    try:
        sys.path.insert(0, str(root))
        from src import staleness_guard
        verdicts = staleness_guard.scan(root=root)
        for v in verdicts or []:
            if isinstance(v, dict) and v.get("stale"):
                F.add(WARNING, "stale_data", "staleness_guard.scan",
                      f"artifact `{v.get('name')}` is STALE now: age {v.get('age_hours', '?')}h "
                      f"vs {v.get('threshold_hours', '?')}h — {v.get('effect') or v.get('note') or ''}",
                      day=now.date().isoformat())
        if not verdicts:
            F.add(INFO, "stale_data", "staleness_guard.scan", "no artifacts registered / scan empty")
    except Exception as e:
        F.add(INFO, "stale_data", "staleness_guard.scan", f"could not run the scan here ({e})")
    # (b) the live snapshot's age vs the 3-minute rule (current state only)
    snap = root / "data" / "market_snapshot.json"
    if snap.is_file():
        try:
            d = json.loads(snap.read_text())
            ts = d.get("ts") or d.get("now") or d.get("as_of")
            when = datetime.fromisoformat(str(ts)) if ts else None
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=IST)
            age = (now - when).total_seconds() if when else None
            marks = d.get("marks") or d.get("positions") or []
            if age is None:
                F.add(INFO, "stale_data", snap.name, "snapshot carries no timestamp field")
            elif age > SNAPSHOT_MAX_AGE_S and len(marks) > 0:
                F.add(INFO, "stale_data", snap.name,
                      f"snapshot is {age / 60:.0f} min old with {len(marks)} mark(s) — fine "
                      "outside market hours; readers reject it past 180 s by rule",
                      day=when.date().isoformat())
            else:
                F.add(INFO, "stale_data", snap.name, f"snapshot age {age:.0f} s, {len(marks)} mark(s)")
        except Exception as e:
            F.add(INFO, "stale_data", snap.name, f"unreadable ({e})")
    else:
        F.add(INFO, "stale_data", "data/market_snapshot.json", "absent here")
    # (c) what the logs said about staleness in the window
    logs = root / "logs"
    hits = defaultdict(lambda: {"n": 0, "ev": ""})
    if logs.is_dir():
        for path in sorted(logs.glob("*.log")):
            if path.name.startswith("audit_report"):
                continue
            for day, line in _iter_lines(path, start):
                if STALE_RE.search(line) and not re.search(r"(?i)not stale|fresh\b", line):
                    h = hits[(path.name, day)]
                    h["n"] += 1
                    h["ev"] = h["ev"] or line
    for (name, day), h in sorted(hits.items(), key=lambda kv: (kv[0][1] or "", kv[0][0])):
        sev = WARNING if re.search(r"(?i)no market state|too old|refus|fail.?closed|no NEW", h["ev"]) else INFO
        F.add(sev, "stale_data", name, f"{h['n']} staleness line(s)", day=day, count=h["n"],
              evidence=h["ev"])
    F.add(INFO, "stale_data", "data/journal.jsonl",
          "HONEST LIMIT: proposals carry no quote timestamp, so a 3-minute staleness at "
          "evaluation time cannot be proven or disproven from the journal — only from the "
          "snapshot age and the log lines above")


# ------------------------------------------------------------------ 3. memory / compute

def audit_memory(root: Path, start: date, F: Findings) -> dict:
    logs = root / "logs"
    readings = []
    if (logs / "ops_monitor.log").is_file():
        for day, line in _iter_lines(logs / "ops_monitor.log", start):
            m = TELEMETRY_RE.search(line)
            if m:
                readings.append({"day": day, "used": _f(m["used"]), "free": _f(m["free"]),
                                 "swap": _f(m["swap"]), "load": _f(m["load"]), "line": line})
    if not readings:
        F.add(INFO, "memory", "logs/ops_monitor.log", "no 🖥 telemetry lines in the window")
    runs = {"mem": 0, "load": 0}

    def _flush(kind, run, label):
        if len(run) >= 2:
            worst = min(run, key=lambda r: r["free"] if r["free"] is not None else 1e9)
            sev = CRITICAL if (len(run) >= 3 or (worst["free"] is not None
                                                 and worst["free"] < MEM_FREE_MB_CRITICAL)) else WARNING
            F.add(sev, "memory", "logs/ops_monitor.log",
                  f"{len(run)} CONSECUTIVE {label} readings ({run[0]['day']} → {run[-1]['day']})",
                  day=run[-1]["day"], count=len(run), evidence=worst["line"])
            runs[kind] += 1

    mem_run, load_run = [], []
    for r in readings:
        hot = ((r["used"] is not None and r["used"] >= MEM_USED_PCT_HIGH)
               or (r["free"] is not None and r["free"] < MEM_FREE_MB_LOW))
        if hot:
            mem_run.append(r)
        else:
            _flush("mem", mem_run, "high-memory"); mem_run = []
        if r["load"] is not None and r["load"] >= LOAD_HIGH:
            load_run.append(r)
        else:
            _flush("load", load_run, "high-load"); load_run = []
    _flush("mem", mem_run, "high-memory"); _flush("load", load_run, "high-load")
    swaps = [r for r in readings if r["swap"] is not None]
    if len(swaps) >= 3 and all(b["swap"] > a["swap"] for a, b in zip(swaps[-3:], swaps[-3:][1:])):
        F.add(WARNING, "memory", "logs/ops_monitor.log",
              f"swap rising across the last 3 readings ({swaps[-3]['swap']:.0f} → "
              f"{swaps[-1]['swap']:.0f} MB) — a leak candidate", day=swaps[-1]["day"],
              evidence=swaps[-1]["line"])
    if readings and not runs["mem"] and not runs["load"]:
        peak = max(readings, key=lambda r: r["used"] or 0)
        F.add(INFO, "memory", "logs/ops_monitor.log",
              f"{len(readings)} telemetry readings, no consecutive spike; peak mem "
              f"{peak['used']}% used ({peak['free']} MB free)", day=peak["day"])
    # OOM words anywhere + the ops ledger
    for path in [logs / "problems.jsonl"] + sorted(logs.glob("*.log")) if logs.is_dir() else []:
        if not path.is_file() or path.name.startswith("audit_report"):
            continue
        for day, line in _iter_lines(path, start):
            if MEM_RE.search(line):
                F.add(CRITICAL, "memory", path.name, "memory failure named in the logs",
                      day=day, evidence=line)
    return {"readings": len(readings)}


# ------------------------------------------------------------------ 4. missed exits

def _offline_bars(root: Path, ticker: str, since: str) -> list:
    """[(day, low, high, close)] from the lakes; [] when unpriceable."""
    sys.path.insert(0, str(root))
    t = str(ticker).upper()
    key = {"NIFTY 50": "NIFTY", "NIFTY BANK": "NIFTY_BANK", "NIFTY FIN SERVICE": "NIFTY_FIN_SERVICE",
           "NIFTY IT": "NIFTY_IT"}.get(t)
    try:
        if key:
            from src.ingestion.macro_lake import read_series
            rows = [(d, v) for d, v in read_series(key, lake_dir=root / "data" / "lake" / "macro")
                    if v is not None and d >= since]
            return [(d, v, v, v) for d, v in rows]
        from src.ingestion.bhavcopy_clerk import bars_for
        bars = bars_for(t, days=400, lake_dir=root / "data" / "lake" / "bhavcopy")
        out = []
        for b in bars:
            d = b.get("date") or b.get("day")
            if d and d >= since and b.get("close") is not None:
                out.append((d, b.get("low", b["close"]), b.get("high", b["close"]), b["close"]))
        return out
    except Exception:
        return []


def audit_exits(root: Path, start: date, F: Findings, today: date) -> dict:
    jpath = root / "data" / "journal.jsonl"
    stats = {"open": 0, "replayed": 0, "unpriceable": 0}
    if not jpath.is_file():
        F.add(INFO, "missed_exit", "data/journal.jsonl", "absent here")
        return stats
    entries = []
    for line in jpath.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    try:
        sys.path.insert(0, str(root))
        from src import plan_tracker as pt
    except Exception as e:
        F.add(INFO, "missed_exit", "src.plan_tracker", f"resolver not importable here ({e})")
        pt = None
    for e in entries:
        spread = e.get("spread")
        if not spread or e.get("decision") != "approved":
            continue
        sid = e.get("short_id", "?")
        if e.get("outcome"):
            o = e["outcome"]
            exit_day = str(o.get("exit_date") or o.get("checked") or "")
            if exit_day and exit_day >= start.isoformat():
                if o.get("settlement_basis") == "no_price_data_max_loss":
                    F.add(WARNING, "missed_exit", "data/journal.jsonl",
                          f"`{sid}` {e.get('ticker')} settled at the DEFINED MAX LOSS with no price "
                          "data (expiry backstop) — review by hand", day=exit_day)
                ex = o.get("execution") or {}
                if ex.get("error"):
                    F.add(WARNING, "missed_exit", "data/journal.jsonl",
                          f"`{sid}` exit ticket fell back to the modeled exit: {ex['error']}",
                          day=exit_day)
            continue
        stats["open"] += 1
        expiry = str(spread.get("expiry") or "")
        if expiry and expiry < today.isoformat():
            F.add(CRITICAL, "missed_exit", "data/journal.jsonl",
                  f"`{sid}` {e.get('ticker')} {spread.get('strategy')} expired {expiry} and is STILL "
                  "OPEN — the expiry backstop has not settled it", day=expiry)
            continue
        if pt is None:
            continue
        bars = _offline_bars(root, e.get("ticker"), str(e.get("date")))
        if not bars:
            stats["unpriceable"] += 1
            F.add(INFO, "missed_exit", "data/journal.jsonl",
                  f"`{sid}` {e.get('ticker')} open since {e.get('date')}: cannot replay offline "
                  "(no lake bars for this underlying here)")
            continue
        stats["replayed"] += 1
        try:
            hit = pt._resolve_spread(e, bars)
        except Exception as ex:
            F.add(INFO, "missed_exit", "data/journal.jsonl", f"`{sid}` replay failed ({ex})")
            continue
        if hit and hit[3] < today.isoformat():
            F.add(WARNING, "missed_exit", "data/journal.jsonl",
                  f"`{sid}` {e.get('ticker')} {spread.get('strategy')}: the tracker's own rule "
                  f"`{hit[0]}` fired on {hit[3]} on lake closes, yet the row is still open — "
                  "check that day's tracker run / price feed", day=hit[3], evidence=str(hit))
        else:
            F.add(INFO, "missed_exit", "data/journal.jsonl",
                  f"`{sid}` {e.get('ticker')} replayed on {len(bars)} lake bars: no exit trigger yet")
    if stats["open"] == 0:
        F.add(INFO, "missed_exit", "data/journal.jsonl", "no open approved spreads")
    # venue / shadow-account errors on entries inside the window
    for e in entries:
        if not str(e.get("created_at") or e.get("date") or "").startswith(tuple(
                (start + timedelta(days=i)).isoformat() for i in range((today - start).days + 1))):
            continue
        ex = e.get("execution") or {}
        if ex.get("error"):
            F.add(WARNING, "missed_exit", "data/journal.jsonl",
                  f"`{e.get('short_id')}` entry venue error (legacy fill applied): {ex['error']}",
                  day=str(e.get("date")))
        for acct, v in (e.get("accounts") or {}).items():
            if (v or {}).get("status") == "error":
                F.add(WARNING, "missed_exit", "data/journal.jsonl",
                      f"`{e.get('short_id')}` shadow account {acct} errored: {v.get('reason')}",
                      day=str(e.get("date")))
    return stats


# ------------------------------------------------------------------ 5. the reports' own words

def audit_reports(root: Path, start: date, F: Findings) -> None:
    logs = root / "logs"
    for name in ("ceo_brief.log", "ops_monitor.log", "eod_summary.log"):
        path = logs / name
        if not path.is_file():
            F.add(INFO, "reports", name, "absent here")
            continue
        for day, line in _iter_lines(path, start):
            if re.search(r"(?i)\bSILENT\b|\bSTALE\b|RED ALARM|🔴|SYSTEM PAUSED|blind|"
                         r"heartbeat.*(missing|silent)", line):
                F.add(WARNING, "reports", name, "the system's own report flagged this",
                      day=day, evidence=line)
    ppath = logs / "problems.jsonl"
    if ppath.is_file():
        by_log = defaultdict(int)
        for day, line in _iter_lines(ppath, start):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            found = str(row.get("found") or "")[:10]
            if found and found < start.isoformat():
                continue
            by_log[row.get("log", "?")] += int(row.get("count") or 1)
        for log, n in sorted(by_log.items(), key=lambda kv: -kv[1]):
            F.add(INFO, "reports", "problems.jsonl", f"ops monitor logged {n} problem line(s) from `{log}`",
                  count=n)


# ------------------------------------------------------------------ render

def render(F: Findings, root: Path, start: date, today: date, stats: dict) -> str:
    rows = F.sorted()
    counts = {s: sum(1 for r in rows if r["severity"] == s) for s in (CRITICAL, WARNING, INFO)}
    out = [f"# Forensic log sweep — {start} → {today} ({(today - start).days} days)", "",
           f"_Generated {datetime.now(IST):%Y-%m-%d %H:%M IST} from `{root}`. Read-only. "
           "A source that is absent here is reported as absent, never as clean._", "",
           "| Severity | Findings |", "|---|---|"]
    out += [f"| {s} | {counts[s]} |" for s in (CRITICAL, WARNING, INFO)]
    out += ["", "**Coverage.** API lines by kind: "
            + (", ".join(f"{k} {v}" for k, v in sorted(stats['api']['per_kind'].items())) or "none")
            + f" · telemetry readings {stats['memory']['readings']} · open spreads "
            f"{stats['exits']['open']} (replayed {stats['exits']['replayed']}, unpriceable "
            f"{stats['exits']['unpriceable']}).", ""]
    for sev in (CRITICAL, WARNING, INFO):
        group = [r for r in rows if r["severity"] == sev]
        out += [f"## {sev} ({len(group)})", ""]
        if not group:
            out += ["_none_", ""]
            continue
        out += ["| Category | Source | Date | n | Finding | Evidence |", "|---|---|---|---|---|---|"]
        for r in group:
            ev = r["evidence"].replace("|", "\\|")
            det = r["detail"].replace("|", "\\|")
            out.append(f"| {r['category']} | `{r['source']}` | {r['day']} | {r['count']} | "
                       f"{det} | {ev} |")
        out.append("")
    out += ["## How to read this", "",
            "- **api**: every Dhan/NSE friction line in the window, recovered or not. Auth/data-access "
            "codes (DH-901/902/903/906) are CRITICAL because no retry fixes them.",
            "- **stale_data**: the guard's live verdicts + what the logs said. The journal cannot prove "
            "quote age at evaluation time; that gap is named above, not hidden.",
            "- **memory**: consecutive hot readings only — one hot reading is weather, two is a run.",
            "- **missed_exit**: the tracker's own resolver replayed on offline lake closes; a trigger "
            "day before today on a still-open row means that day's run or feed must be checked.",
            "- Undated lines inherit the last timestamp above them in their file; `—` = none found.", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="15-day forensic log sweep (read-only).")
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--out", default=None, help="default <root>/logs/audit_report_15day.md")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--today", default=None, help="ISO date (tests)")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    today = date.fromisoformat(a.today) if a.today else datetime.now(IST).date()
    start = today - timedelta(days=a.days)
    now = datetime.combine(today, datetime.now(IST).time(), tzinfo=IST)
    F = Findings()
    stats = {}
    stats["api"] = audit_api(root, start, F)
    audit_stale(root, start, F, now)
    stats["memory"] = audit_memory(root, start, F)
    stats["exits"] = audit_exits(root, start, F, today)
    audit_reports(root, start, F)
    text = render(F, root, start, today, stats)
    if a.stdout:
        print(text)
    else:
        out = Path(a.out) if a.out else root / "logs" / "audit_report_15day.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(out)
        c = {s: sum(1 for r in F.rows if r["severity"] == s) for s in (CRITICAL, WARNING, INFO)}
        print(f"[audit_logs] wrote {out} — CRITICAL {c[CRITICAL]} · WARNING {c[WARNING]} · INFO {c[INFO]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

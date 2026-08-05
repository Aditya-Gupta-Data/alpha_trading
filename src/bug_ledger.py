"""
src/bug_ledger.py — the autonomous run's internal bug ledger (#84, D5)
======================================================================

Owner Directive 5 (2026-07-21, issued at sign-off): with Discord
throttled to 5 messages/day, silent failures need a home the owner's
return-day review reads FIRST. Once nightly (cron #22, 20:40 IST —
right after the 20:30 ops sweep refreshes problems.jsonl) this collator
folds every non-fatal error, logic miss and skipped execution into ONE
consolidated file:

    logs/autonomous_bug_report.jsonl

SOURCES (all read-only; the module is imported by NOTHING in the
trading path and can never touch it):
  * logs/problems.jsonl          — the ops sweep's harvest of problem
                                   lines from every job log
  * brain_map account_events     — margin_exhaustion,
                                   equity_budget_exhausted, sizing_zero,
                                   equity_desk_ruin_halt,
                                   risk_of_ruin_halt, daily_breaker_halt
  * logs/treasury_ledger.jsonl   — aborted / vm_unreachable rows
                                   (holds and rotations are normal ops)
  * logs/sizing_adjustments.jsonl — VETO rows (a veto is a skipped
                                   execution; penalties are normal ops)

Dedup is ledger-as-memory (Issue-8 pattern): a (source, fingerprint)
already reported never re-appends. Append-only output; every collector
fails open — a broken source costs its own rows, never the sweep.

THE THURSDAY PROTOCOL (locked in HANDOVER.md + project memory): the
next working session's FIRST task — before any new code, query, or
architecture — is `python3 -m src.bug_ledger --report`, then analyze
and fix every row.

CLI:
    python3 -m src.bug_ledger             # collect now (the cron mode)
    python3 -m src.bug_ledger --report    # the return-day read
    python3 -m src.bug_ledger --prune [--dry-run]   # retire quiet rows
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
REPORT_PATH = LOGS_DIR / "autonomous_bug_report.jsonl"

EVENT_TYPES = ("margin_exhaustion", "equity_budget_exhausted",
               "sizing_zero", "equity_desk_ruin_halt",
               "risk_of_ruin_halt", "daily_breaker_halt")
TREASURY_ACTIONS = ("aborted", "vm_unreachable")


def _read_jsonl(path) -> list:
    try:
        out = []
        for ln in Path(path).read_text().splitlines():
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
        return out
    except OSError:
        return []


def _existing_fingerprints(report_path=None) -> set:
    return {(r.get("source"), r.get("fingerprint"))
            for r in _read_jsonl(report_path or REPORT_PATH)}


def _append(rows: list, report_path=None) -> int:
    if not rows:
        return 0
    p = Path(report_path) if report_path else REPORT_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            for r in rows:
                f.write(json.dumps(dict(
                    r, reported=datetime.now(IST).isoformat(
                        timespec="seconds"))) + "\n")
        return len(rows)
    except OSError:
        return 0


def collect(logs_dir=None, conn=None, report_path=None) -> dict:
    """One nightly sweep. Returns {"added": n, "by_source": {...}} —
    every collector fail-opens independently."""
    logs = Path(logs_dir) if logs_dir else LOGS_DIR
    seen = _existing_fingerprints(report_path)
    fresh, by_source = [], {}

    def _take(source, fingerprint, detail: dict):
        if (source, str(fingerprint)) in seen:
            return
        seen.add((source, str(fingerprint)))
        fresh.append({"source": source, "fingerprint": str(fingerprint),
                      **detail})
        by_source[source] = by_source.get(source, 0) + 1

    # 1. the ops sweep's problem lines (every job log)
    try:
        for row in _read_jsonl(logs / "problems.jsonl"):
            _take("ops_problems",
                  f"{row.get('log')}|{str(row.get('line'))[:160]}",
                  {"log": row.get("log"),
                   "line": str(row.get("line"))[:400],
                   "count": row.get("count"), "found": row.get("found")})
    except Exception:
        pass

    # 2. account events — the silent rejections and halts
    try:
        owns = conn is None
        if conn is None:
            from src import brain_map
            conn = brain_map.connect()
        try:
            marks = ",".join("?" for _ in EVENT_TYPES)
            for ts, etype, detail in conn.execute(
                    f"SELECT ts, event_type, detail FROM account_events "
                    f"WHERE event_type IN ({marks})", EVENT_TYPES):
                _take("account_events", f"{etype}|{ts}",
                      {"event": etype, "ts": ts,
                       "detail": str(detail)[:400]})
        finally:
            if owns:
                conn.close()
    except Exception:
        pass

    # 3. treasury anomalies (holds/rotations are normal ops)
    try:
        for row in _read_jsonl(logs / "treasury_ledger.jsonl"):
            if row.get("action") in TREASURY_ACTIONS:
                _take("treasury", f"{row.get('action')}|{row.get('ts')}",
                      {"action": row.get("action"), "ts": row.get("ts"),
                       "detail": str(row.get("detail"))[:400]})
    except Exception:
        pass

    # 4. adaptive-sizing vetoes = skipped executions
    try:
        for row in _read_jsonl(logs / "sizing_adjustments.jsonl"):
            if row.get("action") == "veto":
                _take("sizing_veto", f"{row.get('key')}|{row.get('ts')}",
                      {"key": row.get("key"), "ts": row.get("ts"),
                       "detail": str(row.get("detail"))[:400]})
    except Exception:
        pass

    added = _append(fresh, report_path)
    return {"added": added, "by_source": by_source}


# ------------------------------------------------------- aging (2026-08-05)
#
# THE DEFECT THIS LEDGER HAD, demonstrated by its own contents. The first
# Thursday-Protocol triage found 74 rows and EVERY ONE traced to a root cause
# already fixed days or weeks earlier — the DH-905 host throttle (#85), the
# Secret-Manager token flow (#47/#48), the `"failed": []` scrubber, the
# ceo_brief self-echo exclusion, the Issue-4 Ollama-offline wording. The
# ledger could not say so, because it has no concept of a row going quiet:
# append-only, dedup-forever, no last-seen, no resolution. So a fixed bug
# looks exactly like a live one, the pile only grows, and the honest reaction
# to a 74-item file is to stop opening it — which is what happened for 15
# days.
#
# Aging is deliberately EVIDENCE-BASED, not a delete button: a family is
# "quiet" only because nothing matching it has been collected for
# QUIET_AFTER_DAYS, and quiet rows are MOVED to a sibling file, never
# destroyed. Nothing is ever marked fixed by assertion.

QUIET_AFTER_DAYS = 7
RESOLVED_PATH = LOGS_DIR / "autonomous_bug_report.resolved.jsonl"


def _row_day(row: dict) -> str:
    """The row's own date (YYYY-MM-DD), or '' when it carries none."""
    for key in ("found", "ts", "reported"):
        v = row.get(key)
        if v:
            return str(v)[:10]
    return ""


def _family(row: dict) -> str:
    """What this row is an instance of: the log it came from, else its
    source. Rows recur per-occurrence, so aging must judge the FAMILY —
    one fresh intraday failure keeps the whole intraday family live."""
    fp = str(row.get("fingerprint") or "")
    return fp.split("|")[0] if "|" in fp else str(row.get("source") or "?")


def family_last_seen(rows: list) -> dict:
    """{family: newest day it was collected} — how you tell a family that
    is still happening from one that stopped."""
    out = {}
    for r in rows:
        day = _row_day(r)
        if day:
            fam = _family(r)
            out[fam] = max(out.get(fam, ""), day)
    return out


def partition_by_age(rows: list, today=None,
                     quiet_after_days: int = QUIET_AFTER_DAYS) -> tuple:
    """(active, quiet), judged PER ROW on the row's own date.

    Row-level, not family-level, and that distinction is the whole point.
    The intraday-capture family is still live (one failure on 08-03) but 43
    of its 45 rows are occurrences from 07-17..07-27 that the DH-905
    host-throttle fix (#85) superseded. Keeping them because the family is
    live buries the two rows that still mean something. Retiring them
    leaves the recent occurrences AND the family's last-seen date, which is
    everything triage needs.

    An UNDATED row is always ACTIVE: an unknown age is not evidence of
    quiet — the same fail-safe direction the staleness guard takes."""
    today = today or datetime.now(IST).date()
    if isinstance(today, str):
        today = datetime.fromisoformat(today).date()
    active, quiet = [], []
    for r in rows:
        day = _row_day(r)
        if not day:
            active.append(r)
            continue
        try:
            age = (today - datetime.fromisoformat(day).date()).days
        except ValueError:
            active.append(r)
            continue
        (quiet if age > quiet_after_days else active).append(r)
    return active, quiet


def prune(report_path=None, resolved_path=None, today=None,
          quiet_after_days: int = QUIET_AFTER_DAYS,
          dry_run: bool = False) -> dict:
    """Move rows whose family has gone quiet into the resolved sibling.

    NOT a delete: the rows are appended to `autonomous_bug_report.resolved
    .jsonl` with the day they were retired, so the evidence survives and a
    family that comes BACK is re-collected as a fresh row (dedup memory
    lives in the active file, which no longer holds it)."""
    path = Path(report_path or REPORT_PATH)
    rows = _read_jsonl(path)
    active, quiet = partition_by_age(rows, today=today,
                                     quiet_after_days=quiet_after_days)
    fams = sorted({_family(r) for r in quiet})
    if dry_run or not quiet:
        return {"active": len(active), "retired": len(quiet),
                "families": fams, "written": False}
    rp = Path(resolved_path or RESOLVED_PATH)
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        with rp.open("a") as f:
            for r in quiet:
                f.write(json.dumps({**r, "retired_at": stamp}) + "\n")
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w") as f:
            for r in active:
                f.write(json.dumps(r) + "\n")
        tmp.replace(path)
    except OSError as exc:
        return {"active": len(active), "retired": 0, "families": [],
                "written": False, "error": str(exc)}
    return {"active": len(active), "retired": len(quiet),
            "families": fams, "written": True}


def render_report(report_path=None, today=None) -> str:
    """The Thursday read: every collected row, grouped by source,
    oldest first — or the honest all-clear.

    Since 2026-08-05 it leads with the ACTIVE/QUIET split, so a pile of
    long-fixed rows can never again read as a pile of live bugs."""
    rows = _read_jsonl(report_path or REPORT_PATH)
    if not rows:
        return ("AUTONOMOUS BUG REPORT — empty. No non-fatal errors, "
                "misses or skips were collected during the run.")
    active, quiet = partition_by_age(rows, today=today)
    groups = {}
    for r in rows:
        groups.setdefault(r.get("source", "?"), []).append(r)
    lines = [f"AUTONOMOUS BUG REPORT — {len(rows)} item(s) across "
             f"{len(groups)} source(s)"]
    lines.append(f"  ACTIVE {len(active)} (collected in the last "
                 f"{QUIET_AFTER_DAYS}d) · QUIET {len(quiet)} (older — "
                 "`--prune` retires them to the .resolved sibling)")
    seen = family_last_seen(rows)
    if seen:
        lines.append("  family last seen: " + " · ".join(
            f"{k} {v}" for k, v in sorted(seen.items(),
                                          key=lambda kv: kv[1], reverse=True)))
    lines.append("")
    for source in sorted(groups):
        lines.append(f"== {source} ({len(groups[source])}) " + "=" * 30)
        for r in groups[source]:
            when = r.get("ts") or r.get("found") or r.get("reported", "")
            what = (r.get("detail") or r.get("line") or r.get("event")
                    or r.get("action") or "")
            lines.append(f"  [{when}] {what}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if "--prune" in sys.argv:
        res = prune(dry_run="--dry-run" in sys.argv)
        print(json.dumps(res, indent=2))
    elif "--report" in sys.argv:
        print(render_report())
    else:
        res = collect()
        print(f"bug ledger sweep: +{res['added']} "
              f"({res['by_source'] or 'nothing new'})")

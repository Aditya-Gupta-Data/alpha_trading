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


def render_report(report_path=None) -> str:
    """The Thursday read: every collected row, grouped by source,
    oldest first — or the honest all-clear."""
    rows = _read_jsonl(report_path or REPORT_PATH)
    if not rows:
        return ("AUTONOMOUS BUG REPORT — empty. No non-fatal errors, "
                "misses or skips were collected during the run.")
    groups = {}
    for r in rows:
        groups.setdefault(r.get("source", "?"), []).append(r)
    lines = [f"AUTONOMOUS BUG REPORT — {len(rows)} item(s) across "
             f"{len(groups)} source(s)\n"]
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
    if "--report" in sys.argv:
        print(render_report())
    else:
        res = collect()
        print(f"bug ledger sweep: +{res['added']} "
              f"({res['by_source'] or 'nothing new'})")

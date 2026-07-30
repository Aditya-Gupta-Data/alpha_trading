#!/usr/bin/env python3
# MANUAL OFFLINE TOOL — not on any cron/systemd path; keep out of dead-code
# sweeps (same marker convention as src/tuner.py, CLAUDE.md Rule 5).
"""Stage B forward-scoring clock — instant read-only status.

The Oct-1 target is a 60-session Dept-5 ledger (docs/strategy_registry_spec.md
§11 Stage B, "the hinge"). The only thing that can lose it is the VM's 19:50
`macro_nightly` cron silently missing sessions, so this prints the clock AND
the uptime canary in one shot.

READ-ONLY BY CONSTRUCTION — the property, not just the intent:
  * stdlib ONLY (`argparse`/`json`/`datetime`/`pathlib`); it imports NOTHING
    from `src/`, so it cannot transitively reach `brain_map.connect()` or any
    other sqlite door. The sqlite module is never imported here at all, so
    there is no DB code to review.
  * every file is opened for READ. No writes, no temp files, no locks.
  * it never calls the scorer, the declarer or the registry — a status tool
    that RUNS the pipeline could perturb the very ledger it reports on, and
    on the 1 GB e2-micro a stray heavy call is also an OOM risk.
  Same doctrine as `scripts/export_trade_book.py`.

Sources (all append-only or regenerated artifacts, never mutated here):
  logs/macro_regime_declarations.jsonl  the immutable declaration ledger
  logs/macro_strategy_scores.jsonl      graded calls (absent until the first
                                        forward window matures — that is
                                        CORRECT, not a fault)
  data/strategy_scoreboard.json         the rolled-up forward verdicts
  logs/macro_nightly.log                per-run stage heartbeat

Usage:
    python3 scripts/stage_b_tracker.py
    python3 scripts/stage_b_tracker.py --json          # machine-readable
    python3 scripts/stage_b_tracker.py --root ~/alpha_trading
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

TARGET_SESSIONS = 60
TARGET_DATE = date(2026, 10, 1)
STALE_AFTER_WEEKDAYS = 2      # missed cron canary


def _read_jsonl(path: Path) -> list:
    """Every parseable JSON object in a file, tolerant of the pretty-printed
    blocks macro_nightly.log interleaves with its compact heartbeat lines."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def _weekdays_between(a: date, b: date) -> int:
    """Business days from a to b (cron is Mon-Fri). Holidays are NOT modelled
    — this is an estimate and is labelled as one wherever it is printed."""
    if b <= a:
        return 0
    days = (b - a).days
    return sum(1 for i in range(days)
               if (a + timedelta(days=i + 1)).weekday() < 5)


def collect(root: Path, today: date) -> dict:
    logs, data = root / "logs", root / "data"
    decls = _read_jsonl(logs / "macro_regime_declarations.jsonl")
    scores = _read_jsonl(logs / "macro_strategy_scores.jsonl")

    sessions = sorted({d.get("as_of_session") for d in decls
                       if d.get("as_of_session")})
    # One declaration row can carry several horizon calls; the 60-session
    # clock counts SESSIONS, the pending/graded counts count CALLS.
    calls = 0
    for d in decls:
        h = d.get("horizons")
        if isinstance(h, dict):
            calls += sum(1 for v in h.values()
                         if isinstance(v, dict) and v.get("declared"))

    board = {}
    p = data / "strategy_scoreboard.json"
    if p.is_file():
        try:
            board = json.loads(p.read_text())
        except (ValueError, TypeError):
            board = {}
    summary = board.get("summary") or {}

    # Heartbeat: the LAST run that reported stages.
    runs = [r for r in _read_jsonl(logs / "macro_nightly.log") if "stages" in r]
    last = runs[-1] if runs else {}
    last_stages = last.get("stages") or {}
    failed_stages = sorted(k for k, v in last_stages.items()
                           if isinstance(v, dict) and v.get("error"))

    last_session = sessions[-1] if sessions else None
    gap = _weekdays_between(date.fromisoformat(last_session), today) if last_session else None

    return {
        "as_of": today.isoformat(),
        "sessions_on_ledger": len(sessions),
        "target_sessions": TARGET_SESSIONS,
        "sessions_remaining": max(0, TARGET_SESSIONS - len(sessions)),
        "first_session": sessions[0] if sessions else None,
        "last_session": last_session,
        "weekdays_since_last_session": gap,
        "declared_calls": calls,
        "graded_calls": len(scores),
        "pending_calls": max(0, calls - len(scores)),
        "forward_confirmed": summary.get("confirmed_count", 0),
        "forward_contradicted": summary.get("contradicted_count", 0),
        "scoreboard_states": summary.get("summary") if isinstance(
            summary.get("summary"), dict) else {
                k: v for k, v in summary.items() if k.startswith("FORWARD_")
                or k == "INCONCLUSIVE"},
        "last_run_at": last.get("run_at") or last.get("ts"),
        "last_run_all_ok": last.get("all_ok"),
        "last_run_status_line": last.get("status_line"),
        "failed_stages": failed_stages,
        "weekdays_to_target_date": _weekdays_between(today, TARGET_DATE),
        "target_date": TARGET_DATE.isoformat(),
    }


def render(s: dict) -> str:
    L = []
    done, target = s["sessions_on_ledger"], s["target_sessions"]
    filled = int(round(20 * done / target)) if target else 0
    L.append("STAGE B — 60-session forward-scoring clock")
    L.append(f"  [{'#' * filled}{'.' * (20 - filled)}] "
             f"{done}/{target} sessions  ({done * 100 // target}%)")
    L.append(f"  ledger spans {s['first_session']} -> {s['last_session']}"
             f"   ({s['sessions_remaining']} sessions still needed)")
    L.append("")
    L.append("CALLS")
    L.append(f"  declared : {s['declared_calls']:4}   (one per declared horizon)")
    L.append(f"  graded   : {s['graded_calls']:4}")
    L.append(f"  pending  : {s['pending_calls']:4}   "
             "(awaiting their forward window to mature)")
    if s["graded_calls"] == 0:
        L.append("  note: zero graded is CORRECT until the first forward window "
                 "matures;")
        L.append("        a missing macro_strategy_scores.jsonl is not a fault.")
    L.append("")
    L.append("FORWARD VERDICTS (never pooled with in-sample)")
    L.append(f"  confirmed {s['forward_confirmed']} · "
             f"contradicted {s['forward_contradicted']}")
    L.append("")
    L.append("UPTIME CANARY — the only thing that can lose the Oct-1 target")
    gap = s["weekdays_since_last_session"]
    if gap is None:
        L.append("  ?? no declarations on the ledger at all")
    elif gap > STALE_AFTER_WEEKDAYS:
        L.append(f"  !! STALE — {gap} weekdays since the last declared session "
                 f"({s['last_session']}). The 19:50 cron may be dead.")
    else:
        L.append(f"  ok — last session {s['last_session']} "
                 f"({gap} weekday(s) ago)")
    L.append(f"  last run : {s['last_run_at']}  all_ok={s['last_run_all_ok']}")
    if s["last_run_status_line"]:
        L.append(f"  stages   : {s['last_run_status_line']}")
    if s["failed_stages"]:
        L.append(f"  !! FAILED STAGES: {', '.join(s['failed_stages'])}")
    L.append("")
    L.append(f"PACE — {s['weekdays_to_target_date']} weekdays to "
             f"{s['target_date']} (holidays not modelled)")
    need, have = s["sessions_remaining"], s["weekdays_to_target_date"]
    if need == 0:
        L.append("  target already met.")
    elif have >= need:
        L.append(f"  on track: {have} available vs {need} needed "
                 f"(slack {have - need}).")
    else:
        L.append(f"  !! BEHIND: {have} available vs {need} needed "
                 f"(short {need - have}).")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None,
                    help="repo root (default: this script's parent)")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--today", default=None, help="override for testing")
    args = ap.parse_args()

    root = Path(args.root).expanduser() if args.root else Path(__file__).resolve().parent.parent
    today = date.fromisoformat(args.today) if args.today else date.today()
    s = collect(root, today)
    print(json.dumps(s, indent=2) if args.json else render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

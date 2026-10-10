"""
src/node_fingerprint.py — a read-only picture of one host's trading day.

WHY (2026-10-10, decision #144). The home node (`minipc1`) runs the VM's
whole schedule in parallel for a trial week, and every night the node
compares itself with the VM (`src/node_reconcile.py`). Both sides must be
measured by the SAME code, so this file is the measuring tape: it runs
unchanged on the node and — copied to /tmp, run, deleted, never installed —
on the VM, where it only READS.

STANDARD LIBRARY ONLY, no `src` imports: that is what lets it travel as a
single file to a host whose repo does not contain it.

    python3 node_fingerprint.py [--root DIR] [--date YYYY-MM-DD]   # JSON to stdout

What it reports for one IST date:
  * meta       host, git sha, config.json hash, uptime, seed marker
  * jobs       every cron job in the host's crontab (schedule, log file,
               due that day?, did its log move that day?, last error line)
  * artifacts  the shipped/derived files: size, mtime, sha256, fresh today?
  * tables     row counts (total, and rows dated that day) per sqlite table
  * lists      that day's tickets, journal decisions, outcomes (as sorted
               strings, so two hosts can be set-compared)
  * accounts   the small paper_accounts / treasury_state tables, whole

Every probe is fail-open and named: a missing table or file is recorded as
absent, never raised, and nothing is ever written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

# Files whose presence, freshness and content the reconcile compares.
ARTIFACTS = (
    "data/darling_tiers.json", "data/darlings_levels.json", "data/darlings_valuation.json",
    "data/darlings_queue.json", "data/darling_ids.json", "data/fo_liquidity.json",
    "data/sector_index_bars.json", "data/news_sentiment.json", "data/market_snapshot.json",
    "data/dashboard_benchmarks.json", "data/human_pulse.json", "data/portfolio.json",
    "logs/equity_shadow_journal.jsonl", "logs/macro_regime_declarations.jsonl",
    "logs/macro_strategy_scores.jsonl", "data/journal.jsonl",
)

TABLES = (
    "trade_tickets", "trade_legs", "outcomes", "shadow_trades", "margin_locks",
    "paper_live_positions", "paper_margin_locks", "portfolio_margin_locks", "portfolio_events",
    "account_events", "paper_account_events", "equity_curve", "paper_equity_curve",
    "portfolio_equity_curve", "net_equity_history", "events", "graph_edges", "semantic_nodes",
    "daily_context", "simulated_trades", "treasury_state", "capital_allocations", "wealth_lock_ledger",
)
SMALL_TABLES = ("paper_accounts", "treasury_state", "portfolios")      # dumped whole
_DATE_COLS = ("issued_at", "created_at", "ts", "timestamp", "date", "opened_at", "closed_at", "updated_at")
_LIST_CAP = 500
_HASH_MAX_BYTES = 64 * 1024 * 1024
_PROBLEM = re.compile(r"traceback|error|failed|fatal|exception", re.I)


# ------------------------------------------------------------------ cron
_SCHEDULE = re.compile(r"^\s*([0-9*/,\-]+\s+[0-9*/,\-]+\s+[0-9*/,\-]+\s+[0-9*/,\-]+\s+[0-9*/,\-]+)\s+(.*\S)\s*$")
_LOG = re.compile(r">>\s*\"?([^\"\s]+)\"?")
_MODULE = re.compile(r"-m\s+([A-Za-z0-9_.]+)((?:\s+--?[A-Za-z0-9_\-]+(?:\s+[A-Za-z0-9_.\-]+)?)*)")
_SCRIPT = re.compile(r"(?:bash|sh)\s+\"?(?:\$?[A-Za-z_/.\-]*/)?(scripts/[A-Za-z0-9_.\-]+\.sh)\"?((?:\s+--?[A-Za-z0-9_\-]+)*)")


def parse_cron(text: str, root: Path | None = None) -> list[dict]:
    """Active schedule lines of a crontab as [{key, schedule, log, raw}].
    `key` names the job: `src.module [--flag]` or `scripts/x.sh [--flag]`,
    so two lines of one module (the intraday tracker and its --darlings
    twin) stay distinct. Comments, env assignments and @reboot are skipped."""
    jobs = []
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _SCHEDULE.match(line)
        if not m:
            continue
        schedule, cmd = m.group(1), m.group(2)
        key = None
        mm = _MODULE.search(cmd)
        if mm:
            key = (mm.group(1) + (" " + mm.group(2).strip() if mm.group(2).strip() else "")).strip()
        else:
            sm = _SCRIPT.search(cmd)
            if sm:
                key = (sm.group(1) + (" " + sm.group(2).strip() if sm.group(2).strip() else "")).strip()
        if not key:
            key = cmd.split("&&")[-1].strip().split(">>")[0].strip()[:80]
        log = None
        lm = _LOG.search(cmd)
        if lm:
            log = lm.group(1)
            if root is not None:
                try:
                    log = str(Path(log).resolve().relative_to(Path(root).resolve()))
                except Exception:
                    pass
        jobs.append({"key": key, "schedule": schedule, "log": log})
    return jobs


def _expand(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = max(1, int(s))
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        out.update(range(a, b + 1, step))
    return {v for v in out if lo <= v <= hi}


def due_on(schedule: str, day: date) -> bool:
    """Would a crontab line with this schedule fire at all on `day`?
    Standard cron: when both day-of-month and day-of-week are restricted,
    either matching is enough."""
    try:
        _m, _h, dom, mon, dow = schedule.split()
        if day.month not in _expand(mon, 1, 12):
            return False
        cron_dow = (day.weekday() + 1) % 7                    # Mon=1 … Sun=0
        dows = {0 if v == 7 else v for v in _expand(dow, 0, 7)}
        dom_ok = day.day in _expand(dom, 1, 31)
        dow_ok = cron_dow in dows
        if dom != "*" and dow != "*":
            return dom_ok or dow_ok
        return dom_ok and dow_ok
    except Exception:
        return True                                           # unparseable = assume it should run


def first_fire_minute(schedule: str) -> int | None:
    """Minutes after midnight of the earliest firing in a day, or None."""
    try:
        mins, hours = schedule.split()[:2]
        return min(_expand(hours, 0, 23)) * 60 + min(_expand(mins, 0, 59))
    except Exception:
        return None


# ------------------------------------------------------------------ probes
def _sha256(path: Path) -> str | None:
    try:
        if path.stat().st_size > _HASH_MAX_BYTES:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _mtime_ist(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, IST)
    except Exception:
        return None


def _tail_lines(path: Path, n: int = 40) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except Exception:
        return []


def probe_jobs(root: Path, day: date, crontab_text: str) -> list[dict]:
    out = []
    for j in parse_cron(crontab_text, root):
        row = {"key": j["key"], "schedule": j["schedule"], "log": j["log"],
               "due": due_on(j["schedule"], day), "first_fire_min": first_fire_minute(j["schedule"]),
               "log_mtime": None, "ran": False, "problem": None}
        if j["log"]:
            p = Path(j["log"]) if os.path.isabs(j["log"]) else Path(root) / j["log"]
            mt = _mtime_ist(p)
            if mt is not None:
                row["log_mtime"] = mt.isoformat(timespec="seconds")
                row["ran"] = mt.date() == day
                if row["ran"]:
                    bad = [ln for ln in _tail_lines(p) if _PROBLEM.search(ln)]
                    if bad:
                        row["problem"] = bad[-1].strip()[:160]
        out.append(row)
    return out


def probe_artifacts(root: Path, day: date) -> dict:
    out = {}
    for rel in ARTIFACTS:
        p = Path(root) / rel
        if not p.exists():
            out[rel] = {"present": False}
            continue
        mt = _mtime_ist(p)
        row = {"present": True, "size": p.stat().st_size,
               "mtime": mt.isoformat(timespec="seconds") if mt else None,
               "fresh": bool(mt and mt.date() == day), "sha256": _sha256(p)}
        if rel.endswith(".jsonl"):
            try:
                with open(p, "rb") as f:
                    lines = f.read().splitlines()
                row["lines"] = len(lines)
                row["tail_sha"] = hashlib.sha256(lines[-1]).hexdigest() if lines else None
            except Exception:
                pass
        out[rel] = row
    return out


def _ro(db: Path) -> sqlite3.Connection | None:
    try:
        if not Path(db).exists():
            return None
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    except Exception:
        return None


def probe_db(root: Path, day: date) -> dict:
    out = {"present": False, "tables": {}, "accounts": {}, "tickets_today": [], "outcomes_today": []}
    conn = _ro(Path(root) / "data" / "brain_map.db")
    if conn is None:
        return out
    out["present"] = True
    d = day.isoformat()
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in TABLES + SMALL_TABLES:
            if t not in have:
                continue
            row = {"rows": conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]}
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            dc = next((c for c in _DATE_COLS if c in cols), None)
            if dc:
                row["today"] = conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE substr({dc},1,10)=?", (d,)).fetchone()[0]
            out["tables"][t] = row
        for t in SMALL_TABLES:
            if t in have:
                cur = conn.execute(f"SELECT * FROM {t} LIMIT 50")
                names = [c[0] for c in cur.description]
                out["accounts"][t] = [dict(zip(names, r)) for r in cur.fetchall()]
        if "trade_tickets" in have:
            out["tickets_today"] = sorted(
                "|".join(str(x) for x in r) for r in conn.execute(
                    "SELECT underlying, strategy, COALESCE(direction,''), status FROM trade_tickets "
                    "WHERE substr(issued_at,1,10)=?", (d,)))[:_LIST_CAP]
        if "outcomes" in have:
            out["outcomes_today"] = sorted(
                "|".join(str(x) for x in r) for r in conn.execute(
                    "SELECT ticker, result FROM outcomes WHERE date=?", (d,)))[:_LIST_CAP]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:160]
    finally:
        conn.close()
    return out


def probe_journal(root: Path, day: date) -> list[str]:
    p = Path(root) / "data" / "journal.jsonl"
    d = day.isoformat()
    rows = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if f'"{d}' not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if str(r.get("date", ""))[:10] != d:
                    continue
                spread = r.get("spread") or {}
                plan = r.get("plan") or {}
                tags = r.get("pattern_tags") or []
                strategy = (spread.get("strategy") if isinstance(spread, dict) else None) \
                    or (plan.get("strategy") if isinstance(plan, dict) else None) \
                    or (tags[0] if tags else "")
                rows.append(f"{r.get('ticker', '?')}|{strategy}|{r.get('decision', '?')}")
    except Exception:
        return []
    return sorted(rows)[:_LIST_CAP]


def _git_sha(root: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None


def _crontab() -> str:
    try:
        return subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def _uptime_s() -> int | None:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return None


def fingerprint(root, day: date | None = None, crontab_text: str | None = None) -> dict:
    root = Path(root)
    day = day or datetime.now(IST).date()
    seed = None
    try:
        seed = json.loads((root / "data" / ".node_seed.json").read_text())
    except Exception:
        pass
    return {
        "host": socket.gethostname(), "day": day.isoformat(),
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "git": _git_sha(root), "config_sha": _sha256(root / "config.json"),
        "python": sys.version.split()[0], "uptime_s": _uptime_s(), "seed": seed,
        "jobs": probe_jobs(root, day, _crontab() if crontab_text is None else crontab_text),
        "artifacts": probe_artifacts(root, day),
        "db": probe_db(root, day),
        "journal_today": probe_journal(root, day),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only fingerprint of one host's trading day")
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--date", default=None, help="IST date YYYY-MM-DD (default: today)")
    args = ap.parse_args(argv)
    day = date.fromisoformat(args.date) if args.date else None
    print(json.dumps(fingerprint(Path(args.root), day), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

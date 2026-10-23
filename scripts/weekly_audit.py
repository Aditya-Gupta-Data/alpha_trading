#!/usr/bin/env python3
# MANUAL OFFLINE TOOL — nothing schedules this; the owner (or the Architect's session) runs it by hand.
"""
scripts/weekly_audit.py — the weekly architecture audit, straight from the live files. READ-ONLY.

Run it ON THE VM (the book, the proposal ledger, the logs) and ON THE MINI PC (event emission,
reconcile reports, heartbeat); each host prints what it holds and says plainly what it does not.

    cd ~/alpha_trading && venv/bin/python scripts/weekly_audit.py                 # this week, Mon..today
    venv/bin/python scripts/weekly_audit.py --since 2026-10-19 --until 2026-10-23

It prints ONLY the WEEKLY_AUDIT_REPORT block. Every figure is a count taken from a file on THIS host;
a section with no data says `NO DATA` and why. Nothing is written, nothing is fetched, no secret is read
(config.json is read for two switches only). The sqlite book is opened `mode=ro`.

What each line answers:
  * Earnings Gate     proposal-ledger rows whose reason is the #151 "earnings proximity gate" refusal
  * GEX Overrides     traces of the #155 gamma zone in the logs and ledger — the live proposer does NOT route
                      on GEX (D5, planner only), so an empty result is the designed outcome, not a fault
  * Greek Attribution rows of `greek_attribution` written in the window, coverage, one sample
  * Event Engine      PAPER_EVENTS: cards seen, actions, positions, and P&L split by strike path A (fixed) / B (llm)
  * System Health     deployment fingerprint, disk, last-write age of every expected job log, reconcile verdicts
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
EARNINGS_RE = re.compile(r"earnings proximity gate: results in (\d+) day\(s\) \(<= (\d+)\) — (\w+) refused")
GEX_LOG_RE = re.compile(r"short[_ ]gamma|gamma[_ ]zone|gamma flip|zero[_ ]gamma|GEX", re.I)


def week_bounds(since=None, until=None, today=None):
    today = today or datetime.now(IST).date()
    until = date.fromisoformat(until) if until else today
    since = date.fromisoformat(since) if since else until - timedelta(days=until.weekday())
    return since, until


def _ro(path: Path):
    try:
        if not path.exists():
            return None
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return None


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _read_jsonl(path: Path) -> list:
    rows = []
    try:
        for ln in path.read_text().splitlines():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def _in(d: str, since: date, until: date) -> bool:
    try:
        return since <= date.fromisoformat(str(d)[:10]) <= until
    except ValueError:
        return False


# --------------------------------------------------------------------------- 1. earnings gate
def earnings_gate(root: Path, since: date, until: date) -> list:
    ledger = root / "data" / "proposal_ledger.jsonl"
    rows = [r for r in _read_jsonl(ledger) if _in(r.get("session_date", ""), since, until)]
    if not ledger.exists():
        return ["NO DATA: data/proposal_ledger.jsonl is not on this host (the live proposer runs on the VM)"]
    hits = [(r, EARNINGS_RE.search(str(r.get("reason") or ""))) for r in rows]
    hits = [(r, m) for r, m in hits if m]
    out = [f"evaluations in the window: {len(rows)}; refused by the earnings proximity gate: {len(hits)}"]
    if not hits:
        out.append("no #151 refusals in the window. Either no gated structure (condor, butterfly, credit vertical, "
                   "bear put) was proposed inside 5 days of results, or the gate is not deployed on this host "
                   "(check the System Health fingerprint).")
        return out
    by_u, by_s = Counter(), Counter()
    days = Counter()
    for r, m in hits:
        by_u[r.get("underlying")] += 1
        by_s[m.group(3)] += 1
        days[int(m.group(1))] += 1
    out.append("by underlying: " + ", ".join(f"{k} {v}" for k, v in by_u.most_common()))
    out.append("by refused strategy: " + ", ".join(f"{k} {v}" for k, v in by_s.most_common()))
    out.append("by days-to-results: " + ", ".join(f"{k}d {v}" for k, v in sorted(days.items())))
    infy = [(r, m) for r, m in hits if "INFY" in str(r.get("underlying")).upper()]
    out.append(f"INFY: {len(infy)} refusal(s)" + (
        "; " + "; ".join(f"{r['session_date']} {m.group(3)} ({m.group(1)}d)" for r, m in infy[:5]) if infy else ""))
    return out


# --------------------------------------------------------------------------- 2. GEX
def gex_overrides(root: Path, since: date, until: date) -> list:
    out = []
    ledger = [r for r in _read_jsonl(root / "data" / "proposal_ledger.jsonl") if _in(r.get("session_date", ""), since, until)]
    zoned = [r for r in ledger if r.get("gamma_zone") or "gamma" in str(r.get("reason") or "").lower()]
    out.append(f"proposal-ledger rows carrying a gamma zone or a gamma reason: {len(zoned)} of {len(ledger)}")
    log_hits, scanned = [], 0
    for name in ("master_scheduler.log", "gex_backfill.log", "gex_planner_dry_run.log"):
        p = root / "logs" / name
        if not p.exists():
            continue
        scanned += 1
        try:
            for ln in p.read_text(errors="replace").splitlines():
                m = re.match(r"\[?(\d{4}-\d{2}-\d{2})", ln)
                if GEX_LOG_RE.search(ln) and (not m or _in(m.group(1), since, until)):
                    log_hits.append(f"{name}: {ln.strip()[:140]}")
        except OSError:
            pass
    out.append(f"log lines mentioning GEX / gamma in the window: {len(log_hits)} (logs scanned: {scanned})")
    out.extend(log_hits[:4])
    out.append("DESIGN NOTE: #155 is PLANNER ONLY. The live proposer (options_proposer) still routes on the SMA view "
               "(D5 shadow-first) and the VM has no participant-OI feed, so a live GEX override of an SMA 'neutral' "
               "is not possible on the VM this week. An empty line here is expected, not a failure.")
    return out


# --------------------------------------------------------------------------- 3. greek attribution
def greek_attribution(root: Path, since: date, until: date) -> list:
    conn = _ro(root / "data" / "brain_map.db")
    if conn is None:
        return ["NO DATA: data/brain_map.db is not on this host"]
    try:
        if "greek_attribution" not in _tables(conn):
            return ["table `greek_attribution` DOES NOT EXIST: the #155 settlement hook has not run on this host "
                    "(not deployed, or no trade has settled since the deploy)"]
        cur = conn.execute("SELECT journal_ref, as_of, ticker, strategy, coverage, components, taylor_total, "
                           "actual_window_pnl, residual, trade_pnl, outside_window, method, reason "
                           "FROM greek_attribution WHERE substr(as_of,1,10) BETWEEN ? AND ? ORDER BY as_of",
                           (since.isoformat(), until.isoformat()))
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        total = conn.execute("SELECT COUNT(*) FROM greek_attribution").fetchone()[0]
    finally:
        conn.close()
    out = [f"rows written in the window: {len(rows)} (table total: {total})"]
    if not rows:
        return out + ["no trade settled with attribution in the window"]
    full = 0
    for r in rows:
        try:
            comps = json.loads(r["components"]) if r["components"] else {}
        except ValueError:
            comps = {}
        keys = set(comps) if isinstance(comps, dict) else set()
        if {"delta", "gamma", "theta", "vega"} <= keys:
            full += 1
    out.append(f"rows with delta + gamma + theta + vega all present: {full} of {len(rows)}; "
               f"mean coverage {sum(float(r['coverage'] or 0) for r in rows) / len(rows):.2f}")
    reasons = Counter(str(r["reason"]) for r in rows if r["reason"])
    if reasons:
        out.append("no-attribution reasons: " + "; ".join(f"{k[:70]} ×{v}" for k, v in reasons.most_common(3)))
    s = next((r for r in rows if r["components"]), rows[0])
    try:
        comps = json.loads(s["components"]) if s["components"] else None
    except ValueError:
        comps = s["components"]
    out.append("SAMPLE: " + json.dumps({"ref": s["journal_ref"], "ticker": s["ticker"], "strategy": s["strategy"],
                                         "components": comps, "taylor_total": s["taylor_total"],
                                         "actual_window_pnl": s["actual_window_pnl"], "residual": s["residual"],
                                         "trade_pnl": s["trade_pnl"]}, default=str))
    return out


# --------------------------------------------------------------------------- 4. event engine
def event_engine(root: Path, since: date, until: date) -> list:
    out = []
    try:
        cfg = json.loads((root / "config.json").read_text()).get("event_engine") or {}
    except (OSError, ValueError):
        cfg = {}
    out.append(f"switches: execute_enabled={cfg.get('execute_enabled')}, emit_enabled={cfg.get('emit_enabled')}, "
               f"max_loss_per_trade_rs={cfg.get('max_loss_per_trade_rs')}")
    cards = sorted((root / "data").glob("event_card__*.json")) if (root / "data").is_dir() else []
    emitted = sorted((root / "data" / "event_cards").glob("*.json")) if (root / "data" / "event_cards").is_dir() else []
    out.append(f"cards shipped to this host (data/event_card__*.json): {len(cards)}; cards emitted on this host "
               f"(data/event_cards): {len(emitted)}")
    conn = _ro(root / "data" / "brain_map.db")
    if conn is None:
        return out + ["NO DATA: data/brain_map.db is not on this host"]
    try:
        have = _tables(conn)
        if "event_positions" not in have:
            return out + ["tables event_cards_seen / event_actions / event_positions DO NOT EXIST: the executor has "
                          "never run here (not deployed, or execute_enabled has never been true)"]
        seen = conn.execute("SELECT status, COUNT(*) FROM event_cards_seen WHERE substr(seen_at,1,10) BETWEEN ? AND ? "
                            "GROUP BY status", (since.isoformat(), until.isoformat())).fetchall()
        acts = conn.execute("SELECT action, COUNT(*) FROM event_actions WHERE substr(ts,1,10) BETWEEN ? AND ? "
                            "GROUP BY action", (since.isoformat(), until.isoformat())).fetchall()
        why = conn.execute("SELECT reason, COUNT(*) FROM event_actions WHERE action IN ('abstained','refused') AND "
                           "substr(ts,1,10) BETWEEN ? AND ? GROUP BY reason ORDER BY 2 DESC LIMIT 3",
                           (since.isoformat(), until.isoformat())).fetchall()
        out.append("cards seen by the VM: " + (", ".join(f"{s} {n}" for s, n in seen) or "none"))
        out.append("actions: " + (", ".join(f"{a} {n}" for a, n in acts) or "none"))
        if why:
            out.append("top skip reasons: " + "; ".join(f"{str(r)[:70]} ×{n}" for r, n in why))
        pos = conn.execute("SELECT path, state, COUNT(*), COALESCE(SUM(pnl_net),0), COALESCE(SUM(entry_total_rs),0), "
                           "COALESCE(SUM(CASE WHEN state='open' AND last_mark IS NOT NULL THEN "
                           "(last_mark-entry_premium)*lot_size*lots END),0) FROM event_positions "
                           "WHERE substr(opened_at,1,10) <= ? GROUP BY path, state", (until.isoformat(),)).fetchall()
        split = defaultdict(lambda: {"opened": 0, "closed": 0, "realised": 0.0, "premium": 0.0, "mtm": 0.0})
        for path, state, n, pnl, prem, mtm in pos:
            d = split[path]
            d["opened"] += n
            d["premium"] += prem
            if state == "closed":
                d["closed"] += n
                d["realised"] += pnl
            else:
                d["mtm"] += mtm
        label = {"fixed": "Path A fixed 3% OTM", "llm": "Path B LLM strike"}
        for p in ("fixed", "llm"):
            d = split.get(p)
            out.append(f"{label[p]}: " + ("no positions" if not d else
                       f"{d['opened']} opened, {d['closed']} closed, realised Rs.{d['realised']:+,.0f}, "
                       f"open mark-to-bid Rs.{d['mtm']:+,.0f}, premium at risk Rs.{d['premium']:,.0f}"))
        if "paper_accounts" in have:
            a = conn.execute("SELECT starting_capital, realized_pnl FROM paper_accounts WHERE account_id='PAPER_EVENTS'").fetchone()
            out.append("PAPER_EVENTS account: " + (f"capital Rs.{a[0]:,.0f}, realised Rs.{a[1]:+,.0f}" if a else "no row yet"))
    finally:
        conn.close()
    return out


# --------------------------------------------------------------------------- 5. system health
def system_health(root: Path, since: date, until: date) -> list:
    out = []
    import subprocess
    try:
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except Exception:
        sha = "?"
    has = {p: (root / p).exists() for p in ("src/event_engine/executor.py", "src/analysis/greek_attribution.py",
                                            "src/analysis/gamma_profile.py", "src/node_reconcile.py")}
    out.append(f"commit {sha}; code present: " + ", ".join(f"{k.split('/')[-1]}={'yes' if v else 'NO'}" for k, v in has.items()))
    try:
        u = shutil.disk_usage(root)
        pct = 100.0 * u.used / u.total
        flag = "  ** RED: under 1 GB free **" if u.free < 1 << 30 else ("  * low *" if pct > 85 else "")
        out.append(f"disk: {u.free / 1e9:.1f} GB free of {u.total / 1e9:.0f} GB ({pct:.0f}% used){flag}")
    except OSError:
        out.append("disk: unreadable")
    stale = []
    try:
        sys.path.insert(0, str(root))
        from src.ops_monitor import EXPECTED_JOBS
        for name, weekdays_only in EXPECTED_JOBS.items():
            p = root / "logs" / name
            if not p.exists():
                stale.append(f"{name}: absent")
                continue
            m = datetime.fromtimestamp(p.stat().st_mtime, IST).date()
            if m < until - timedelta(days=3 if weekdays_only else 1):
                stale.append(f"{name}: last write {m}")
        out.append(f"expected job logs: {len(EXPECTED_JOBS)}; stale or absent: {len(stale)}" + (
            " — " + "; ".join(stale[:6]) if stale else ""))
    except Exception as exc:
        out.append(f"expected job logs: not checked ({type(exc).__name__})")
    problems = Counter()
    for p in (root / "logs").glob("*.log") if (root / "logs").is_dir() else []:
        try:
            for ln in p.read_text(errors="replace").splitlines()[-400:]:
                m = re.match(r"\[?(\d{4}-\d{2}-\d{2})", ln)
                if m and _in(m.group(1), since, until) and re.search(r"Traceback|FATAL|CRITICAL|UNAVAILABLE", ln):
                    problems[p.name] += 1
        except OSError:
            continue
    out.append("error-shaped log lines in the window: " + (", ".join(f"{k} {v}" for k, v in problems.most_common(6)) or "none found"))
    recon = sorted((root / "logs" / "node_reconcile").glob("*.json")) if (root / "logs" / "node_reconcile").is_dir() else []
    verdicts = []
    for f in recon:
        if _in(f.stem, since, until):
            try:
                verdicts.append(f"{f.stem[5:]} {json.loads(f.read_text()).get('verdict')}")
            except (OSError, ValueError):
                pass
    if recon:
        out.append("node reconcile verdicts: " + (", ".join(verdicts) or "none in the window"))
    hb = root / "logs" / "node_heartbeat.log"
    if hb.exists():
        beats = sum(1 for ln in hb.read_text().splitlines() if any(f"|{(since + timedelta(days=i)).isoformat()}" in ln
                                                                    for i in range((until - since).days + 1)))
        out.append(f"node heartbeat lines in the window: {beats}")
    return out


def build_report(root: Path, since: date, until: date) -> str:
    sections = (("Earnings Gate", earnings_gate), ("GEX Overrides", gex_overrides),
                ("Greek Attribution", greek_attribution), ("Event Engine", event_engine),
                ("System Health", system_health))
    lines = ["WEEKLY_AUDIT_REPORT", "===================",
             f"(host {__import__('socket').gethostname()}, window {since} .. {until}, read-only)"]
    for title, fn in sections:
        try:
            body = fn(root, since, until)
        except Exception as exc:                          # one broken section never hides the rest
            body = [f"SECTION FAILED: {type(exc).__name__}: {str(exc)[:140]}"]
        lines.append(f"- {title}: {body[0]}")
        lines.extend(f"    {b}" for b in body[1:])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Weekly architecture audit (read-only)")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--since")
    ap.add_argument("--until")
    a = ap.parse_args(argv)
    since, until = week_bounds(a.since, a.until)
    print(build_report(Path(a.root), since, until))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

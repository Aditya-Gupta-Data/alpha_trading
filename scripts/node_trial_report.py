#!/usr/bin/env python3
# MANUAL OFFLINE TOOL — nothing schedules this; the owner runs it by hand.
"""
scripts/node_trial_report.py — did the home node keep its schedule this week?

WHY (2026-10-10, decision #143). The Linux Mini PC runs its first week in
SHADOW alongside the Mac: every job does its full work, nothing is shipped
to the VM. The question at the end of the week is simple — did the box
fire every slot, did every producer succeed, would the ship have been 7/7
— and the answer must come from the node's own logs, not from memory.

Reads (all fail-open — a missing log is a named gap, never a crash):
  logs/mac_auto_sync.cron.log   the three daily sync slots + @reboot catch-ups
  logs/edge_miner.log           the 21:00 miner summaries
  the boot history              `journalctl --list-boots` (or `last -x reboot`)

Prints one row per day for the last N days (default 7) and a verdict.
Nothing is written anywhere.

    python3 scripts/node_trial_report.py            # last 7 days
    python3 scripts/node_trial_report.py --days 10
    python3 scripts/node_trial_report.py --logs /path/to/logs --through 2026-10-17   # tests

The window ends on the last FULL day (yesterday by default), so today's
slots that have not come round yet are never counted as misses.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLOTS = (("07:30", 7 * 60 + 30), ("12:30", 12 * 60 + 30), ("19:20", 19 * 60 + 20))
SLOT_WINDOW_MIN = 60          # a run within ±60 min of a slot counts for that slot
PRODUCERS = ("sector bars", "valuation", "fo bhavcopy", "darling ids")
SHIP_TOTAL = 7

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}\] (.*)$")


# ------------------------------------------------------------------ parsing
def parse_sync_log(text: str) -> list[dict]:
    """One dict per sync RUN (a '=== mac auto-sync starting' line opens it).
    Throttle skips are kept as runs with status 'throttled' — they prove the
    cron fired even though the work was already done."""
    runs: list[dict] = []
    cur = None
    for raw in text.splitlines():
        m = _TS.match(raw)
        if m:
            day, hhmm, msg = m.groups()
            if msg.startswith("=== mac auto-sync starting"):
                cur = {"date": day, "time": hhmm, "status": "started",
                       "shadow": "SHADOW" in msg, "producers": {},
                       "ship": None, "ship_total": SHIP_TOTAL, "pull": None}
                runs.append(cur)
                continue
            if msg.startswith("skip: last sync"):
                runs.append({"date": day, "time": hhmm, "status": "throttled",
                             "shadow": None, "producers": {}, "ship": None,
                             "ship_total": SHIP_TOTAL, "pull": None})
                cur = None
                continue
            if cur is None:
                continue
            for prod in PRODUCERS:
                if msg.startswith(prod + ":"):
                    if msg.startswith(prod + ": ok"):
                        cur["producers"][prod] = "ok"
                    elif "FAILED" in msg:
                        cur["producers"][prod] = "FAILED"
            if msg.startswith("=== mac auto-sync done"):
                cur["status"] = "done"
            continue
        # untimestamped lines come from the python heredocs (tee'd); they
        # belong to the most recent open run
        if cur is None:
            continue
        line = raw.strip()
        m2 = re.match(r"^(?:SHADOW: would have shipped|shipped) (\d+)/(\d+)", line)
        if m2:
            cur["ship"] = int(m2.group(1))
            cur["ship_total"] = int(m2.group(2))
            continue
        m3 = re.match(r"^pulled (\d+)/(\d+)", line)
        if m3:
            cur["pull"] = (int(m3.group(1)), int(m3.group(2)))
    return runs


def parse_miner_log(text: str) -> list[dict]:
    """One dict per `edge_miner: {...}` summary line, with its date."""
    out = []
    for raw in text.splitlines():
        m = _TS.match(raw)
        if not m or "edge_miner:" not in m.group(3):
            continue
        payload = m.group(3).split("edge_miner:", 1)[1].strip()
        try:
            summary = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            summary = {"status": "unparsed", "raw": payload[:80]}
        out.append({"date": m.group(1), "time": m.group(2), "summary": summary})
    return out


def read_boots(run_fn=None) -> list[str] | None:
    """Boot timestamps as 'YYYY-MM-DD HH:MM' strings, newest last. None
    when neither journalctl nor `last` is available (a named unknown)."""
    run_fn = run_fn or (lambda cmd: subprocess.run(
        cmd, capture_output=True, text=True, timeout=15))
    try:
        if shutil.which("journalctl"):
            res = run_fn(["journalctl", "--list-boots", "--no-pager", "-o", "json"])
            if res.returncode == 0 and res.stdout.strip():
                boots = []
                for line in res.stdout.splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    us = rec.get("first_entry")
                    if us:
                        boots.append(datetime.fromtimestamp(int(us) / 1e6).strftime("%Y-%m-%d %H:%M"))
                if boots:
                    return sorted(boots)
        if shutil.which("last"):
            res = run_fn(["last", "-x", "reboot", "--time-format", "iso"])
            if res.returncode == 0:
                boots = []
                for line in res.stdout.splitlines():
                    m = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", line)
                    if line.startswith("reboot") and m:
                        boots.append(f"{m.group(1)} {m.group(2)}")
                return sorted(boots)
    except Exception:
        return None
    return None


# ------------------------------------------------------------------ the week
def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def build_report(sync_runs, miner_runs, boots, days: int, today: date) -> dict:
    first = today - timedelta(days=days - 1)
    rows = []
    for i in range(days):
        d = first + timedelta(days=i)
        ds = d.isoformat()
        day_runs = [r for r in sync_runs if r["date"] == ds]
        slots = {}
        extras = 0
        for r in day_runs:
            t = _minutes(r["time"])
            hit = None
            for label, mins in SLOTS:
                if abs(t - mins) <= SLOT_WINDOW_MIN:
                    hit = label
                    break
            if hit is None:
                extras += 1
            elif hit not in slots or r["status"] != "throttled":
                slots[hit] = r
        fired = [label for label, _ in SLOTS if label in slots]
        worked = [r for r in day_runs if r["status"] in ("started", "done")]
        failures = sorted({p for r in worked for p, v in r["producers"].items() if v == "FAILED"})
        unfinished = sum(1 for r in worked if r["status"] == "started")
        ships = [r["ship"] for r in worked if r["ship"] is not None]
        best_ship = max(ships) if ships else None
        worst_ship = min(ships) if ships else None
        pulls = [r["pull"] for r in worked if r["pull"] is not None]
        pull_ok = any(p[0] == p[1] and p[1] > 0 for p in pulls) if pulls else None
        miners = [m for m in miner_runs if m["date"] == ds]
        miner = miners[-1]["summary"].get("status") if miners else None
        day_boots = [b for b in (boots or []) if b.startswith(ds)]
        weekend = d.weekday() >= 5
        rows.append({
            "date": ds, "weekday": d.strftime("%a"), "weekend": weekend,
            "slots_fired": fired, "extra_runs": extras,
            "producer_failures": failures, "unfinished_runs": unfinished,
            "ship": best_ship, "ship_worst": worst_ship, "pull_ok": pull_ok, "miner": miner,
            "boots": day_boots,
        })

    misses = []
    for row in rows:
        missing = [s for s, _ in SLOTS if s not in row["slots_fired"]]
        if missing:
            misses.append(f"{row['date']} ({row['weekday']}): slot(s) {', '.join(missing)} never fired")
        if row["producer_failures"]:
            misses.append(f"{row['date']} ({row['weekday']}): producer FAILED — {', '.join(row['producer_failures'])}")
        if row["unfinished_runs"]:
            misses.append(f"{row['date']} ({row['weekday']}): {row['unfinished_runs']} run(s) started but never finished")
        if row["ship"] is not None and row["ship"] < SHIP_TOTAL:
            misses.append(f"{row['date']} ({row['weekday']}): would have shipped only {row['ship']}/{SHIP_TOTAL}")
        elif row["ship_worst"] is not None and row["ship_worst"] < SHIP_TOTAL:
            misses.append(f"{row['date']} ({row['weekday']}): one run would have shipped only "
                          f"{row['ship_worst']}/{SHIP_TOTAL} (another run that day made 7/7)")
        if row["pull_ok"] is False:
            misses.append(f"{row['date']} ({row['weekday']}): the read-only pull from the VM failed (gcloud lane)")
    full_days = [r for r in rows if len(r["slots_fired"]) == len(SLOTS) and not r["producer_failures"]
                 and r["ship"] == SHIP_TOTAL]
    shadow_seen = any(r.get("shadow") for r in sync_runs)
    live_seen = any(r.get("shadow") is False for r in sync_runs)
    return {"today": today.isoformat(), "days": days, "rows": rows, "misses": misses,
            "clean_days": len(full_days), "boots_known": boots is not None,
            "boot_count": len([b for b in (boots or []) if b[:10] >= first.isoformat()]),
            "shadow_seen": shadow_seen, "live_seen": live_seen,
            "verdict": "RELIABLE" if not misses and rows else "NOT YET"}


def render(report: dict) -> str:
    out = [f"HOME NODE TRIAL REPORT — {report['days']} full days through {report['today']}",
           ""]
    if report["shadow_seen"] and not report["live_seen"]:
        out.append("mode: SHADOW (the node shipped nothing; the Mac still owns the lane)")
    elif report["live_seen"] and not report["shadow_seen"]:
        out.append("mode: LIVE (the node owns the lane)")
    elif report["live_seen"] and report["shadow_seen"]:
        out.append("mode: MIXED — both shadow and live runs in the window (promoted mid-week?)")
    else:
        out.append("mode: unknown — no sync run found in the window")
    out.append("")
    hdr = f"{'date':<16}{'slots 07:30/12:30/19:20':<26}{'producers':<12}{'ship':<8}{'pull':<6}{'miner':<10}boots"
    out.append(hdr)
    out.append("-" * len(hdr))
    for r in report["rows"]:
        marks = " ".join("✓" if s in r["slots_fired"] else "·" for s, _ in SLOTS)
        if r["extra_runs"]:
            marks += f" +{r['extra_runs']}"
        prod = "ok" if not r["producer_failures"] else "FAIL:" + ",".join(p.split()[0] for p in r["producer_failures"])
        if not r["slots_fired"] and not r["extra_runs"]:
            prod = "—"
        ship = "—" if r["ship"] is None else f"{r['ship']}/{SHIP_TOTAL}"
        pull = "—" if r["pull_ok"] is None else ("ok" if r["pull_ok"] else "FAIL")
        miner = r["miner"] or "—"
        boots = ", ".join(b[11:] for b in r["boots"]) if r["boots"] else ("—" if report["boots_known"] else "?")
        out.append(f"{r['date'] + ' ' + r['weekday']:<16}{marks:<26}{prod:<12}{ship:<8}{pull:<6}{miner:<10}{boots}")
    out.append("")
    out.append(f"clean days (3/3 slots, no producer failure, would-ship 7/7): {report['clean_days']}/{report['days']}")
    if report["boots_known"]:
        out.append(f"reboots in the window: {report['boot_count']}")
    else:
        out.append("reboots in the window: unknown (no journalctl / last on this host)")
    if report["misses"]:
        out.append("")
        out.append("what went wrong:")
        out.extend("  - " + m for m in report["misses"])
    out.append("")
    if report["verdict"] == "RELIABLE":
        out.append("VERDICT: RELIABLE — every slot fired, every producer succeeded, every ship would have been 7/7.")
        out.append("Next: promote the node (bash scripts/setup_mininode_cron.sh, no --shadow), then retire the Mac's agents (CRON_SETUP.md).")
    else:
        out.append("VERDICT: NOT YET — see 'what went wrong'. Keep the Mac's agents; fix, then run another clean week.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--logs", default=str(ROOT / "logs"), help="logs directory (default: the repo's)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--through", default=None,
                    help="last FULL day of the window, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--no-boots", action="store_true", help="skip the boot-history read")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    logs = Path(args.logs)
    today = date.fromisoformat(args.through) if args.through else date.today() - timedelta(days=1)
    sync_text = (logs / "mac_auto_sync.cron.log").read_text(errors="replace") \
        if (logs / "mac_auto_sync.cron.log").exists() else ""
    miner_text = (logs / "edge_miner.log").read_text(errors="replace") \
        if (logs / "edge_miner.log").exists() else ""
    boots = None if args.no_boots else read_boots()
    report = build_report(parse_sync_log(sync_text), parse_miner_log(miner_text),
                          boots, args.days, today)
    if not sync_text:
        report["misses"].insert(0, f"no {logs / 'mac_auto_sync.cron.log'} — has the cron block been installed?")
        report["verdict"] = "NOT YET"
    print(json.dumps(report, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

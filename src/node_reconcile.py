"""
src/node_reconcile.py — the home node checks itself against the VM, every night.

WHY (2026-10-10, decision #144). For the trial week the node (`minipc1`)
runs the VM's whole cron schedule in parallel while the VM changes in no
way. At 23:30 IST, on the NODE, this module:

  1. fingerprints the node (`src.node_fingerprint`, local),
  2. fingerprints the VM — the same file copied to /tmp, run read-only
     over `gcloud compute ssh`, deleted; nothing is installed or written
     on the VM,
  3. reads the node's own heartbeat log to find the minutes the box was
     dark or off the network,
  4. compares jobs, artifacts, ledger counts, today's tickets / journal
     decisions / outcomes and the paper accounts, labelling every
     difference `explained` (it overlaps a node outage) or not,
  5. files the report — `logs/node_reconcile/<day>.md` + `.json` — and posts
     ONE Discord card labelled `[minipc1]`.

The VM is the reference. A difference the node's outage can account for is
reported as an outage gap, not a divergence. Verdicts:

  MATCH                    nothing differs
  MATCH_WITH_OUTAGE_GAPS   everything that differs overlaps a node outage
  DIVERGED                 something differs with no outage to blame
  INCONCLUSIVE             the VM could not be fingerprinted (no gcloud, ssh failed)

Honest limits, printed in every report: (a) no human presses Approve on the
node, so decisions that wait for a person differ by construction unless the
paper auto-approve path is on; (b) artifacts built from live fetches differ
by fetch timing; (c) a silent cron job leaves no log movement, so "no
evidence" on BOTH hosts is a note, never a failure.

    python3 -m src.node_reconcile [--date YYYY-MM-DD] [--no-notify] [--no-vm] [--week]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from src import node_fingerprint as nf
except ImportError:                                  # run as a plain script from src/
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import node_fingerprint as nf

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT_LOG = ROOT / "logs" / "node_heartbeat.log"
OUT_DIR = ROOT / "logs" / "node_reconcile"

MAX_GAP_S = 150                       # a beat is due every 60 s; > 150 s apart = the box was dark
MARKET_OPEN_MIN, MARKET_CLOSE_MIN = 9 * 60 + 10, 15 * 60 + 30

# Jobs that exist on the VM and are deliberately NOT replicated on the node.
NOT_ON_NODE_BY_DESIGN = ("src.renew_token", "scripts/publish_dashboard_mirror.sh")
# Content differs by nature (LLM output, a snapshot of a moving market): freshness only.
NONDETERMINISTIC = {"data/news_sentiment.json", "data/market_snapshot.json"}
# Built from home-lane fetches (Yahoo / NSE / the corpus) on each machine at its own
# moment: a content difference is information, not a divergence.
INPUT_TIMING = {"data/darlings_valuation.json", "data/sector_index_bars.json", "data/fo_liquidity.json",
                "data/darling_ids.json", "data/darlings_queue.json", "data/dashboard_benchmarks.json"}

LIMITS = (
    "No human presses Approve on the node, so decisions that wait for a person differ by "
    "construction unless the paper auto-approve path is on.",
    "Artifacts built from live fetches differ by fetch timing; those are listed as info, not divergence.",
    "A silent cron job leaves no log movement: no evidence on BOTH hosts is a note, not a failure.",
)


# --------------------------------------------------------------- heartbeat
def read_heartbeat(path, day: date, now: datetime | None = None) -> dict:
    """Outage windows for `day` from the node's once-a-minute heartbeat log
    (`epoch|iso|net=ok|up=<s>`). Returns {available, beats, expected,
    coverage_pct, outages[], net_down[], outage_minutes}. A gap over
    MAX_GAP_S between beats is an outage; before the first beat ever
    written is not counted (the log did not exist yet)."""
    now = now or datetime.now(IST)
    day_start = datetime.combine(day, datetime.min.time(), IST)
    window_end = min(now, day_start + timedelta(days=1))
    out = {"available": False, "beats": 0, "expected": 0, "coverage_pct": None,
           "outages": [], "net_down": [], "outage_minutes": 0}
    try:
        lines = Path(path).read_text().splitlines()
    except Exception:
        return out
    beats = []                                   # (epoch, net_ok)
    for ln in lines:
        parts = ln.split("|")
        try:
            beats.append((float(parts[0]), "net=down" not in ln))
        except (ValueError, IndexError):
            continue
    if not beats:
        return out
    beats.sort()
    t0, t1 = day_start.timestamp(), window_end.timestamp()
    earlier = any(b[0] < t0 for b in beats)
    todays = [b for b in beats if t0 <= b[0] <= t1]
    start = t0 if earlier else (todays[0][0] if todays else t1)
    out["available"] = True
    out["beats"] = len(todays)
    out["expected"] = max(0, int((t1 - start) // 60))
    out["coverage_pct"] = round(min(100.0, 100.0 * len(todays) / out["expected"]), 1) if out["expected"] else None

    def hhmm(ts):
        return datetime.fromtimestamp(ts, IST).strftime("%H:%M")

    points = [start] + [b[0] for b in todays] + [t1]
    for a, b in zip(points, points[1:]):
        if b - a > MAX_GAP_S:
            out["outages"].append({"start": hhmm(a), "end": hhmm(b), "minutes": int((b - a) // 60),
                                   "start_min": int((a - t0) // 60), "end_min": int((b - t0) // 60)})
    cur = None
    for ts, ok in todays:
        if not ok:
            if cur and ts - cur["_last"] <= MAX_GAP_S:
                cur["_last"] = ts
            else:
                if cur:
                    out["net_down"].append(cur)
                cur = {"_first": ts, "_last": ts}
        elif cur:
            out["net_down"].append(cur)
            cur = None
    if cur:
        out["net_down"].append(cur)
    out["net_down"] = [{"start": hhmm(c["_first"]), "end": hhmm(c["_last"] + 60),
                        "minutes": int((c["_last"] - c["_first"]) // 60) + 1,
                        "start_min": int((c["_first"] - t0) // 60),
                        "end_min": int((c["_last"] - t0) // 60) + 1} for c in out["net_down"]]
    out["outage_minutes"] = sum(o["minutes"] for o in out["outages"])
    return out


def _cover(windows: list, minute: int | None) -> dict | None:
    if minute is None:
        return None
    for w in windows:
        if w["start_min"] - 1 <= minute <= w["end_min"] + 1:
            return w
    return None


def _overlaps_market(hb: dict) -> bool:
    for w in hb.get("outages", []) + hb.get("net_down", []):
        if w["start_min"] < MARKET_CLOSE_MIN and w["end_min"] > MARKET_OPEN_MIN:
            return True
    return False


# --------------------------------------------------------------- comparing
def _issue(section, subject, severity, vm=None, node=None, explained=False, cause="") -> dict:
    return {"section": section, "subject": subject, "severity": severity,
            "vm": vm, "node": node, "explained": explained, "cause": cause}


def compare_jobs(vm: dict, node: dict, hb: dict, day: date, now: datetime) -> tuple[list, dict]:
    vmj = {j["key"]: j for j in vm.get("jobs", [])}
    ndj = {j["key"]: j for j in node.get("jobs", [])}
    now_min = int((now - datetime.combine(day, datetime.min.time(), IST)).total_seconds() // 60) \
        if now.date() == day else 24 * 60
    issues, counts = [], {"same": 0, "not_due": 0, "not_yet_due": 0, "by_design": 0,
                          "no_evidence_both": 0, "missing_on_node": 0, "node_only": 0}
    for key in sorted(set(vmj) | set(ndj)):
        v, n = vmj.get(key), ndj.get(key)
        ref = v or n
        if v and not n and key in NOT_ON_NODE_BY_DESIGN:
            counts["by_design"] += 1
            continue
        if not ref.get("due", True):
            counts["not_due"] += 1
            continue
        ff = ref.get("first_fire_min")
        if ff is not None and ff > now_min:
            counts["not_yet_due"] += 1
            continue
        if v and not n:
            counts["missing_on_node"] += 1
            issues.append(_issue("jobs", key, "warn", "scheduled", "NOT SCHEDULED on the node",
                                 cause="the node's crontab has no such job — reinstall the replica block"))
            continue
        if n and not v:
            counts["node_only"] += 1
            issues.append(_issue("jobs", key, "info", "not in the VM crontab", "scheduled on the node"))
            continue
        vran, nran = bool(v.get("ran")), bool(n.get("ran"))
        if vran and nran:
            counts["same"] += 1
            if n.get("problem") and not v.get("problem"):
                issues.append(_issue("jobs", key, "warn", "ran clean", f"ran, but: {n['problem']}"))
        elif vran and not nran:
            counts["missing_on_node"] += 1
            w = _cover(hb.get("outages", []), ff)
            nw = _cover(hb.get("net_down", []), ff)
            if w:
                issues.append(_issue("jobs", key, "warn", "ran", "did not run", True,
                                     f"node outage {w['start']}–{w['end']}"))
            elif nw:
                issues.append(_issue("jobs", key, "warn", "ran", "did not run", True,
                                     f"node network down {nw['start']}–{nw['end']}"))
            else:
                issues.append(_issue("jobs", key, "warn", "ran", "did not run", False,
                                     "no outage covers its start — check the node's cron and that job's log"))
        elif nran and not vran:
            counts["node_only"] += 1
            issues.append(_issue("jobs", key, "info", "no log movement", "ran"))
        else:
            counts["no_evidence_both"] += 1
    return issues, counts


def compare_artifacts(vm: dict, node: dict, hb_outage: bool) -> list:
    issues = []
    va, na = vm.get("artifacts", {}), node.get("artifacts", {})
    for rel in sorted(set(va) | set(na)):
        v, n = va.get(rel, {"present": False}), na.get(rel, {"present": False})
        if not v.get("present") and not n.get("present"):
            continue
        sev_diff = "info" if rel in INPUT_TIMING else "warn"
        if v.get("present") and not n.get("present"):
            issues.append(_issue("artifacts", rel, "warn", "present", "ABSENT", hb_outage,
                                 "node outage that day" if hb_outage else ""))
        elif n.get("present") and not v.get("present"):
            issues.append(_issue("artifacts", rel, "info", "absent", "present"))
        elif rel in NONDETERMINISTIC:
            if v.get("fresh") and not n.get("fresh"):
                issues.append(_issue("artifacts", rel, "warn", "fresh today", f"stale ({n.get('mtime')})", hb_outage,
                                     "node outage that day" if hb_outage else ""))
        elif v.get("sha256") and v.get("sha256") == n.get("sha256"):
            continue
        elif v.get("fresh") and not n.get("fresh"):
            issues.append(_issue("artifacts", rel, sev_diff, "fresh today", f"stale ({n.get('mtime')})",
                                 hb_outage, "node outage that day" if hb_outage else ""))
        else:
            extra = ""
            if "lines" in v or "lines" in n:
                extra = f" ({v.get('lines')} vs {n.get('lines')} lines)"
            issues.append(_issue("artifacts", rel, sev_diff, "content A" + extra, "content B",
                                 False, "built from live inputs at different moments" if rel in INPUT_TIMING else ""))
    return issues


def _num_close(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 0.01
    except (TypeError, ValueError):
        return a == b


def compare_db(vm: dict, node: dict, hb: dict) -> list:
    issues = []
    vd, nd = vm.get("db", {}), node.get("db", {})
    if not vd.get("present") or not nd.get("present"):
        if vd.get("present") != nd.get("present"):
            issues.append(_issue("ledgers", "brain_map.db", "warn",
                                 "present" if vd.get("present") else "absent",
                                 "present" if nd.get("present") else "absent"))
        return issues
    market_gap = _overlaps_market(hb)
    any_gap = bool(hb.get("outages") or hb.get("net_down"))
    for t in sorted(set(vd.get("tables", {})) | set(nd.get("tables", {}))):
        v, n = vd.get("tables", {}).get(t), nd.get("tables", {}).get(t)
        if v is None or n is None:
            issues.append(_issue("ledgers", t, "warn", "present" if v else "absent", "present" if n else "absent"))
            continue
        for field, label in (("rows", "rows"), ("today", "rows today")):
            if field in v or field in n:
                if v.get(field) != n.get(field):
                    behind = (n.get(field) or 0) < (v.get(field) or 0)
                    issues.append(_issue("ledgers", f"{t} {label}", "warn", v.get(field), n.get(field),
                                         behind and any_gap,
                                         "node was behind and had an outage that day" if behind and any_gap else ""))
    for name in sorted(set(vd.get("accounts", {})) | set(nd.get("accounts", {}))):
        vrows, nrows = vd.get("accounts", {}).get(name, []), nd.get("accounts", {}).get(name, [])
        if len(vrows) != len(nrows):
            issues.append(_issue("accounts", name, "warn", f"{len(vrows)} rows", f"{len(nrows)} rows"))
            continue
        for i, (a, b) in enumerate(zip(vrows, nrows)):
            ident = str(next(iter(a.values()), i)) if a else str(i)
            bad = [k for k in a if not _num_close(a.get(k), b.get(k))]
            if bad:
                issues.append(_issue("accounts", f"{name}/{ident}", "warn",
                                     {k: a.get(k) for k in bad[:4]}, {k: b.get(k) for k in bad[:4]},
                                     market_gap, "node outage overlapped the session" if market_gap else ""))
    return issues


def compare_lists(vm: dict, node: dict, hb: dict) -> list:
    issues = []
    market_gap = _overlaps_market(hb)
    pairs = (("tickets today", vm.get("db", {}).get("tickets_today", []), node.get("db", {}).get("tickets_today", [])),
             ("outcomes today", vm.get("db", {}).get("outcomes_today", []), node.get("db", {}).get("outcomes_today", [])),
             ("journal decisions today", vm.get("journal_today", []), node.get("journal_today", [])))
    for label, v, n in pairs:
        vs, ns = set(v), set(n)
        only_vm, only_node = sorted(vs - ns), sorted(ns - vs)
        if only_vm:
            issues.append(_issue("decisions", f"{label}: on the VM only", "warn", only_vm[:8], [],
                                 market_gap, "node outage overlapped the session" if market_gap else ""))
        if only_node:
            issues.append(_issue("decisions", f"{label}: on the node only", "warn", [], only_node[:8]))
    return issues


def compare_meta(vm: dict, node: dict) -> list:
    issues = []
    if vm.get("git") != node.get("git"):
        issues.append(_issue("meta", "git sha", "info", vm.get("git"), node.get("git"),
                             cause="the node carries extra commits (scripts, docs, this tool); compare the engine files if in doubt"))
    if vm.get("config_sha") != node.get("config_sha"):
        issues.append(_issue("meta", "config.json", "warn", (vm.get("config_sha") or "?")[:10],
                             (node.get("config_sha") or "?")[:10], cause="config drift — the two engines are not running the same settings"))
    if not node.get("seed"):
        issues.append(_issue("meta", "baseline", "warn", "live book", "NOT seeded from the VM",
                             cause="run scripts/node_seed_from_vm.sh on a weekend before the trial, or every ledger difference is just a different starting point"))
    return issues


def reconcile(vm: dict, node: dict, hb: dict, day: date, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    if not vm or vm.get("error"):
        return {"day": day.isoformat(), "verdict": "INCONCLUSIVE",
                "reason": (vm or {}).get("error", "no VM fingerprint"), "issues": [], "job_counts": {},
                "heartbeat": hb, "vm_git": None, "node_git": node.get("git")}
    job_issues, counts = compare_jobs(vm, node, hb, day, now)
    outage_day = bool(hb.get("outages") or hb.get("net_down"))
    issues = (job_issues + compare_artifacts(vm, node, outage_day) + compare_db(vm, node, hb)
              + compare_lists(vm, node, hb) + compare_meta(vm, node))
    warn = [i for i in issues if i["severity"] == "warn"]
    unexplained = [i for i in warn if not i["explained"]]
    verdict = "MATCH" if not warn else ("MATCH_WITH_OUTAGE_GAPS" if not unexplained else "DIVERGED")
    return {"day": day.isoformat(), "verdict": verdict, "issues": issues, "job_counts": counts,
            "warn": len(warn), "unexplained": len(unexplained), "heartbeat": hb,
            "vm_git": vm.get("git"), "node_git": node.get("git"),
            "node_uptime_s": node.get("uptime_s"), "vm_host": vm.get("host"), "node_host": node.get("host")}


# --------------------------------------------------------------- the VM side
def fetch_vm_fingerprint(day: date, runner=None, gcloud: str | None = None) -> dict:
    """Run src/node_fingerprint.py on the VM and return its JSON. The file is
    copied to /tmp, run with the VM's own interpreter from the repo root
    (read-only: sqlite `mode=ro`, no writes anywhere) and deleted. Never
    raises: a failure is {"error": "<why>"}, and the verdict becomes
    INCONCLUSIVE instead of a guess."""
    try:
        from src import edge_miner as em
        gc = gcloud or em._gcloud()
        if not gc:
            return {"error": "gcloud CLI not found on the node"}
        runner = runner or em._run
        scp = [gc, "compute", "scp", f"--project={em.GCP_PROJECT}", f"--zone={em.GCP_ZONE}", "--quiet"] \
            + em.SCP_FLAGS + [str(Path(nf.__file__).resolve()), f"{em.VM}:/tmp/node_fingerprint.py"]
        r = em.run_resilient(runner, scp, "ship the fingerprint tool to the VM")
        if getattr(r, "returncode", 1) != 0:
            return {"error": f"could not copy the fingerprint tool: {(getattr(r, 'stderr', '') or '')[-160:]}"}
        ssh = [gc, "compute", "ssh", em.VM, f"--project={em.GCP_PROJECT}", f"--zone={em.GCP_ZONE}", "--quiet"] \
            + em.SSH_FLAGS + ["--command",
                              f"cd {em.VM_REPO} && venv/bin/python3 /tmp/node_fingerprint.py --root . --date {day.isoformat()}; "
                              "rc=$?; rm -f /tmp/node_fingerprint.py; exit $rc"]
        r = em.run_resilient(runner, ssh, "fingerprint the VM")
        out = getattr(r, "stdout", "") or ""
        if getattr(r, "returncode", 1) != 0 or "{" not in out:
            return {"error": f"VM fingerprint failed: {((getattr(r, 'stderr', '') or out))[-160:]}"}
        return json.loads(out[out.index("{"):])
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


# --------------------------------------------------------------- reporting
def _clip(v, n=70) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


def render_markdown(r: dict) -> str:
    hb = r.get("heartbeat", {})
    L = [f"# Node vs VM — {r['day']} — **{r['verdict']}**", "",
         f"VM `{r.get('vm_git')}` · node `{r.get('node_git')}` · label `minipc1`", ""]
    if r["verdict"] == "INCONCLUSIVE":
        L += [f"The VM could not be fingerprinted: {r.get('reason')}.", "",
              "Nothing was compared. The node's own heartbeat for the day is below.", ""]
    L += ["## Node uptime", ""]
    if not hb.get("available"):
        L.append("No heartbeat log — outage attribution is unavailable (is the heartbeat cron line installed?).")
    else:
        L.append(f"Heartbeat coverage **{hb['coverage_pct']}%** ({hb['beats']} of {hb['expected']} minutes).")
        for o in hb.get("outages", []):
            L.append(f"- dark {o['start']}–{o['end']} ({o['minutes']} min)")
        for o in hb.get("net_down", []):
            L.append(f"- network down {o['start']}–{o['end']} ({o['minutes']} min)")
        if not hb.get("outages") and not hb.get("net_down"):
            L.append("No outage today.")
    c = r.get("job_counts", {})
    if c:
        L += ["", "## Jobs", "",
              f"{c.get('same', 0)} ran on both · {c.get('missing_on_node', 0)} missing on the node · "
              f"{c.get('no_evidence_both', 0)} no log evidence on either (silent or not run) · "
              f"{c.get('by_design', 0)} left off the node by design · {c.get('not_due', 0)} not due today · "
              f"{c.get('not_yet_due', 0)} not yet due"]
    issues = r.get("issues", [])
    for sev, title in (("warn", "Differences"), ("info", "For information")):
        rows = [i for i in issues if i["severity"] == sev]
        if not rows:
            continue
        L += ["", f"## {title}", "", "| area | subject | VM | node | explained |", "|---|---|---|---|---|"]
        for i in rows:
            ex = ("outage: " + i["cause"]) if i["explained"] else (i["cause"] or "—")
            L.append(f"| {i['section']} | {i['subject']} | {_clip(i['vm'])} | {_clip(i['node'])} | {ex} |")
    L += ["", "## Limits of this comparison", ""] + [f"- {x}" for x in LIMITS]
    return "\n".join(L) + "\n"


def card_text(r: dict, week: str = "") -> str:
    hb = r.get("heartbeat", {})
    head = {"MATCH": "✅ node matches the VM",
            "MATCH_WITH_OUTAGE_GAPS": "🟡 matches, except where the node was down",
            "DIVERGED": "🔴 node DIVERGED from the VM",
            "INCONCLUSIVE": "⚪ could not compare"}[r["verdict"]]
    L = [f"**{head}** — {r['day']}"]
    if r["verdict"] == "INCONCLUSIVE":
        L.append(str(r.get("reason"))[:200])
    if hb.get("available"):
        bit = f"uptime {hb['coverage_pct']}%"
        if hb.get("outages"):
            bit += " · dark " + ", ".join(f"{o['start']}–{o['end']}" for o in hb["outages"][:4])
        if hb.get("net_down"):
            bit += " · net down " + ", ".join(f"{o['start']}–{o['end']}" for o in hb["net_down"][:4])
        L.append(bit)
    else:
        L.append("no heartbeat log")
    c = r.get("job_counts", {})
    if c:
        L.append(f"jobs: {c.get('same', 0)} same · {c.get('missing_on_node', 0)} missing on node")
    warn = [i for i in r.get("issues", []) if i["severity"] == "warn"]
    for i in warn[:8]:
        tag = "🟡" if i["explained"] else "🔴"
        L.append(f"{tag} {i['section']}: {i['subject']}" + (f" — {i['cause']}" if i["cause"] else ""))
    if len(warn) > 8:
        L.append(f"… and {len(warn) - 8} more in the report file")
    if week:
        L.append(week)
    return "\n".join(L)[:1800]


def week_summary(out_dir, through: date, days: int = 7) -> str:
    rows = []
    for k in range(days - 1, -1, -1):
        d = through - timedelta(days=k)
        try:
            j = json.loads((Path(out_dir) / f"{d.isoformat()}.json").read_text())
            hb = j.get("heartbeat", {})
            rows.append(f"{d.strftime('%a %d')} {j['verdict']}"
                        + (f" ({hb.get('outage_minutes', 0)} min dark)" if hb.get("outage_minutes") else ""))
        except Exception:
            rows.append(f"{d.strftime('%a %d')} no report")
    return "week: " + " · ".join(rows)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text)
    os.replace(tmp, path)


def run_daily(day: date | None = None, root=None, out_dir=None, heartbeat_path=None,
              vm_fingerprint=None, now: datetime | None = None, notify: bool = True,
              notify_fn=None, vm: bool = True) -> dict:
    """The nightly job. Every input is injectable; nothing here can raise."""
    now = now or datetime.now(IST)
    day = day or now.date()
    root = Path(root or ROOT)
    out_dir = Path(out_dir or OUT_DIR)
    try:
        node_fp = nf.fingerprint(root, day)
        hb = read_heartbeat(heartbeat_path or HEARTBEAT_LOG, day, now)
        vm_fp = vm_fingerprint if vm_fingerprint is not None else (
            fetch_vm_fingerprint(day) if vm else {"error": "VM comparison skipped (--no-vm)"})
        result = reconcile(vm_fp, node_fp, hb, day, now)
        md = render_markdown(result)
        _atomic_write(out_dir / f"{day.isoformat()}.md", md)
        _atomic_write(out_dir / f"{day.isoformat()}.json", json.dumps(result, indent=1, default=str))
        week = week_summary(out_dir, day) if day.weekday() == 5 else ""
        if week:
            _atomic_write(out_dir / f"{day.isoformat()}.md", md + f"\n## Week\n\n{week}\n")
        if notify:
            try:
                fn = notify_fn
                if fn is None:
                    from src.notifier import fire_broadcast as fn
                fn({"event": "node_reconcile", "ticker": "desk", "date": day.isoformat(),
                    "text": card_text(result, week)})
            except Exception as exc:
                print(f"  (node_reconcile: card not sent: {exc})")
        result["report"] = str(out_dir / f"{day.isoformat()}.md")
        return result
    except Exception as exc:                         # the nightly never dies loudly in cron
        print(f"node_reconcile: failed: {type(exc).__name__}: {exc}")
        return {"day": day.isoformat(), "verdict": "ERROR", "reason": str(exc)[:200]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile the home node against the VM")
    ap.add_argument("--date", default=None, help="IST date YYYY-MM-DD (default: today)")
    ap.add_argument("--no-notify", action="store_true", help="file the report, post nothing")
    ap.add_argument("--no-vm", action="store_true", help="skip the VM round-trip (verdict INCONCLUSIVE)")
    ap.add_argument("--week", action="store_true", help="print the last 7 days' verdicts and exit")
    args = ap.parse_args(argv)
    day = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()
    if args.week:
        print(week_summary(OUT_DIR, day))
        return 0
    r = run_daily(day, notify=not args.no_notify, vm=not args.no_vm)
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] node_reconcile {r['day']}: {r['verdict']}"
          + (f" — {r.get('warn', 0)} difference(s), {r.get('unexplained', 0)} unexplained" if 'warn' in r else "")
          + (f" — report {r['report']}" if r.get("report") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

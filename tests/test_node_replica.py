"""
The home node as a parallel replica of the VM — decision #144, 2026-10-10.

Owner directive: the Mini PC runs EXACTLY the VM's schedule for a trial week, the VM
changes in no way, every night the node reconciles itself against the VM and files a
report (a missing-on-node job is attributed to an outage when the node's own heartbeat
shows one), and the node's Discord posts carry the label `minipc1`.

Offline: no network, no gcloud, no real data/ or logs/ (every path is a tmp_path).
    python -m pytest tests/test_node_replica.py -q
"""
import base64
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
IST = timezone(timedelta(hours=5, minutes=30))

from src import discord_client, node_fingerprint as nf, node_reconcile as nr, notifier  # noqa: E402


def _load(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ============================================================ the Discord label
def test_label_is_blank_on_the_vm_and_sanitised_on_the_node(monkeypatch):
    monkeypatch.delenv("ALPHA_NODE_LABEL", raising=False)
    assert discord_client.node_label() == "" and notifier.node_label() == ""
    monkeypatch.setenv("ALPHA_NODE_LABEL", "  minipc1 ")
    assert discord_client.node_label() == "minipc1"
    monkeypatch.setenv("ALPHA_NODE_LABEL", "@everyone <b>x</b>")        # no markup / mentions through
    assert discord_client.node_label() == "everyonebxb"            # only letters, digits, - _ . survive
    monkeypatch.setenv("ALPHA_NODE_LABEL", "x" * 100)
    assert len(discord_client.node_label()) == 32


def test_embeds_carry_the_label_only_on_the_node(monkeypatch):
    payload = {"event": "eod", "ticker": "desk", "date": "2026-10-12", "description": "x"}
    monkeypatch.delenv("ALPHA_NODE_LABEL", raising=False)
    plain = notifier._build_embed(payload)
    assert "minipc1" not in json.dumps(plain)
    monkeypatch.setenv("ALPHA_NODE_LABEL", "minipc1")
    tagged = notifier._build_embed(payload)
    assert tagged["title"] == "[minipc1] " + plain["title"]
    assert tagged["footer"]["text"].startswith("minipc1  •  ")
    assert tagged["fields"] == plain["fields"] and tagged["color"] == plain["color"]


def test_webhook_username_plain_content_and_email_subject_are_labelled(monkeypatch):
    import asyncio
    posted = {}

    class FakeResp:
        status_code = 204

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, params=None):
            posted.update(json or {})
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(notifier, "WEBHOOK_MUZZLE_OVERRIDE", False)
    monkeypatch.setattr(notifier, "budget_gate", lambda p: "send")
    monkeypatch.setenv("ALPHA_NODE_LABEL", "minipc1")

    assert asyncio.run(notifier.broadcast_alert(
        {"event": "node_reconcile", "ticker": "desk", "date": "2026-10-12", "text": "hello"})) is True
    assert posted["username"] == "Alpha Trading · minipc1"
    assert posted["embeds"][0]["title"].startswith("[minipc1] ")

    posted.clear()
    assert asyncio.run(discord_client.send_webhook_message("plain note")) is True
    assert posted["content"] == "[minipc1] plain note"

    monkeypatch.delenv("ALPHA_NODE_LABEL")                                # the VM: byte-for-byte as before
    posted.clear()
    asyncio.run(notifier.broadcast_alert({"event": "node_reconcile", "ticker": "desk", "date": "d", "text": "t"}))
    assert "username" not in posted
    asyncio.run(discord_client.send_webhook_message("plain note"))
    assert posted["content"] == "plain note"

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, msg): sent["subject"] = msg["Subject"]

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(notifier, "EMAIL_FROM", "a@b.c")
    monkeypatch.setattr(notifier, "EMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("ALPHA_NODE_LABEL", "minipc1")
    notifier._send_email("Digest", "body")
    assert sent["subject"] == "[minipc1] Digest"


def test_the_reconcile_card_is_never_spooled_by_the_budget():
    assert "node_reconcile" in notifier.BUDGET_ALWAYS


# ============================================================ cron parsing / schedule maths
_CRON = '''# comment
SHELL=/bin/bash
0 7 * * * cd "/r" && "/r/venv/bin/python" -m src.renew_token >> "/r/logs/renew_token.log" 2>&1
50 15 * * 1-5 cd "/r" && "/r/venv/bin/python" -m src.ingestion.intraday_tracker --darlings >> "/r/logs/intraday.log" 2>&1
*/15 9-15 * * 1-5 cd "/r" && "/r/venv/bin/python" -m src.ingestion.intraday_tracker >> "/r/logs/intraday.log" 2>&1
15 19 * * * cd "/r" && "/r/venv/bin/python" -m src.ingestion.bhavcopy_clerk --backfill 5 >> "/r/logs/bhav.log" 2>&1
*/15 9-16 * * 1-5 cd "/r" && bash scripts/publish_dashboard_mirror.sh >> "/r/logs/dashboard_mirror.log" 2>&1
0 22 * * 5 cd "/r" && "/r/venv/bin/python" -m src.analysis.weekly_recalibration >> "/r/logs/wr.log" 2>&1
@reboot sleep 90 && cd /r && bash scripts/mac_auto_sync.sh
'''


def test_parse_cron_keys_distinguish_flags_and_skip_comments_env_and_reboot():
    jobs = nf.parse_cron(_CRON, "/r")
    keys = [j["key"] for j in jobs]
    assert keys == ["src.renew_token", "src.ingestion.intraday_tracker --darlings",
                    "src.ingestion.intraday_tracker", "src.ingestion.bhavcopy_clerk --backfill 5",
                    "scripts/publish_dashboard_mirror.sh", "src.analysis.weekly_recalibration"]
    assert jobs[0]["log"] == "logs/renew_token.log"


def test_due_on_and_first_fire_follow_cron_rules():
    fri, sat = date(2026, 10, 9), date(2026, 10, 10)
    assert nf.due_on("0 22 * * 5", fri) and not nf.due_on("0 22 * * 5", sat)
    assert nf.due_on("35 15 * * 1-5", fri) and not nf.due_on("35 15 * * 1-5", sat)
    assert nf.due_on("0 7 * * *", sat)
    assert nf.due_on("0 9 10 * 1", sat)             # dom=10 OR dow=Monday — cron's either-or rule
    assert nf.first_fire_minute("*/15 9-16 * * 1-5") == 9 * 60
    assert nf.first_fire_minute("30 19 * * *") == 19 * 60 + 30


# ============================================================ the fingerprint
def _mk_host(tmp, *, tickets=(), outcomes=(), accounts=(), journal=(), files=None, logs=None, day="2026-10-12"):
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(tmp / "data" / "brain_map.db")
    db.execute("CREATE TABLE trade_tickets (ticket_id TEXT, underlying TEXT, strategy TEXT, direction TEXT, status TEXT, issued_at TEXT)")
    db.execute("CREATE TABLE outcomes (id INTEGER PRIMARY KEY, journal_ref TEXT, date TEXT, ticker TEXT, result TEXT)")
    db.execute("CREATE TABLE paper_accounts (account_id TEXT, realized REAL, equity REAL)")
    for i, t in enumerate(tickets):
        db.execute("INSERT INTO trade_tickets VALUES (?,?,?,?,?,?)", (f"t{i}", *t, f"{day}T10:00:00"))
    for i, o in enumerate(outcomes):
        db.execute("INSERT INTO outcomes (journal_ref, date, ticker, result) VALUES (?,?,?,?)", (f"o{i}", day, *o))
    for a in accounts:
        db.execute("INSERT INTO paper_accounts VALUES (?,?,?)", a)
    db.commit()
    db.close()
    with open(tmp / "data" / "journal.jsonl", "w") as f:
        for ticker, strat, decision in journal:
            f.write(json.dumps({"date": day, "ticker": ticker, "decision": decision,
                                "spread": {"strategy": strat}}) + "\n")
    for rel, text in (files or {}).items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for rel in (logs or []):
        (tmp / rel).write_text("ok\n")
    return tmp


def test_fingerprint_reads_jobs_artifacts_ledgers_and_decisions(tmp_path):
    day = date(2026, 10, 12)
    root = _mk_host(tmp_path, tickets=[("NIFTY 50", "bull_call", "bullish", "approved")],
                    outcomes=[("TCS", "win")], accounts=[("PAPER_2L", 100.0, 200.0)],
                    journal=[("NIFTY 50", "bull_call", "approved")],
                    files={"data/darling_tiers.json": "{}"}, logs=["logs/sleep.log"])
    cron = '0 20 * * * cd "%s" && "%s/venv/bin/python" -m src.sleep_phase >> "%s/logs/sleep.log" 2>&1\n' % (root, root, root)
    fp = nf.fingerprint(root, day, crontab_text=cron)
    assert fp["day"] == "2026-10-12" and fp["db"]["present"]
    assert fp["db"]["tables"]["trade_tickets"] == {"rows": 1, "today": 1}
    assert fp["db"]["tickets_today"] == ["NIFTY 50|bull_call|bullish|approved"]
    assert fp["db"]["outcomes_today"] == ["TCS|win"]
    assert fp["db"]["accounts"]["paper_accounts"] == [{"account_id": "PAPER_2L", "realized": 100.0, "equity": 200.0}]
    assert fp["journal_today"] == ["NIFTY 50|bull_call|approved"]
    assert fp["artifacts"]["data/darling_tiers.json"]["present"] is True
    assert fp["artifacts"]["data/fo_liquidity.json"] == {"present": False}
    job = fp["jobs"][0]
    assert job["key"] == "src.sleep_phase" and job["log"] == "logs/sleep.log" and job["due"] is True
    assert job["ran"] is False                       # log mtime is NOW, not 2026-10-12: movement is date-exact
    assert nf.fingerprint(root, datetime.now(IST).date(), crontab_text=cron)["jobs"][0]["ran"] is True


def test_fingerprint_never_writes_and_survives_an_empty_host(tmp_path):
    before = sorted(p.name for p in tmp_path.rglob("*"))
    fp = nf.fingerprint(tmp_path, date(2026, 10, 12), crontab_text="")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    assert fp["db"]["present"] is False and fp["jobs"] == [] and fp["journal_today"] == []


# ============================================================ outage attribution from the heartbeat
def _beats(day, gaps=(), start_min=0, end_min=24 * 60, net_down=()):
    t0 = datetime.combine(day, datetime.min.time(), IST).timestamp()
    lines = []
    for m in range(start_min, end_min):
        if any(a <= m < b for a, b in gaps):
            continue
        net = "down" if any(a <= m < b for a, b in net_down) else "ok"
        lines.append(f"{t0 + m * 60:.0f}|x|net={net}|up={m}")
    return "\n".join(lines) + "\n"


def test_heartbeat_finds_the_dark_minutes_and_the_offline_ones(tmp_path):
    day = date(2026, 10, 12)
    log = tmp_path / "hb.log"
    # a beat from the day before proves the log already existed, so the 00:00 start counts
    prev = datetime.combine(day, datetime.min.time(), IST).timestamp() - 60
    log.write_text(f"{prev:.0f}|x|net=ok|up=1\n" + _beats(day, gaps=[(10 * 60 + 12, 10 * 60 + 40)],
                                                          net_down=[(14 * 60, 14 * 60 + 5)]))
    now = datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=23, minutes=30)
    hb = nr.read_heartbeat(log, day, now)
    assert hb["available"] and len(hb["outages"]) == 1
    o = hb["outages"][0]
    assert (o["start"], o["end"], o["minutes"]) == ("10:11", "10:40", 29) or o["minutes"] in (28, 29)
    assert hb["net_down"] and hb["net_down"][0]["start"] == "14:00"
    assert 98.0 <= hb["coverage_pct"] < 100.0
    assert nr.read_heartbeat(tmp_path / "missing.log", day, now)["available"] is False


def test_first_ever_beat_is_not_an_outage_before_it(tmp_path):
    day = date(2026, 10, 12)
    log = tmp_path / "hb.log"
    log.write_text(_beats(day, start_min=9 * 60))
    now = datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=23, minutes=30)
    hb = nr.read_heartbeat(log, day, now)
    assert hb["outages"] == [] and hb["coverage_pct"] == 100.0


# ============================================================ the comparison
def _fp(jobs=(), artifacts=None, db=None, journal=(), git="aaa", cfg="c1", seed=True):
    return {"host": "h", "git": git, "config_sha": cfg, "seed": {"x": 1} if seed else None,
            "jobs": list(jobs), "artifacts": artifacts or {}, "db": db or {"present": True, "tables": {}, "accounts": {}},
            "journal_today": list(journal), "uptime_s": 1000}


def _job(key, ran, sched="30 19 * * *", due=True, problem=None):
    return {"key": key, "schedule": sched, "log": f"logs/{key}.log", "due": due,
            "first_fire_min": nf.first_fire_minute(sched), "ran": ran, "problem": problem}


def _hb(outages=(), net_down=()):
    def w(a, b):
        return {"start": f"{a // 60:02d}:{a % 60:02d}", "end": f"{b // 60:02d}:{b % 60:02d}",
                "minutes": b - a, "start_min": a, "end_min": b}
    out = [w(a, b) for a, b in outages]
    return {"available": True, "beats": 1000, "expected": 1000, "coverage_pct": 100.0,
            "outages": out, "net_down": [w(a, b) for a, b in net_down], "outage_minutes": sum(b - a for a, b in outages)}


NOW = datetime(2026, 10, 12, 23, 30, tzinfo=IST)
DAY = date(2026, 10, 12)


def test_a_job_missing_on_the_node_is_explained_only_by_a_covering_outage():
    vm = _fp(jobs=[_job("src.deals", True), _job("src.sleep", True, "0 20 * * *")])
    node = _fp(jobs=[_job("src.deals", False), _job("src.sleep", False, "0 20 * * *")])
    r = nr.reconcile(vm, node, _hb(outages=[(19 * 60 + 10, 19 * 60 + 50)]), DAY, NOW)
    by = {i["subject"]: i for i in r["issues"] if i["section"] == "jobs"}
    assert by["src.deals"]["explained"] is True and "19:10–19:50" in by["src.deals"]["cause"]
    assert by["src.sleep"]["explained"] is False                      # 20:00 is not inside the outage
    assert r["verdict"] == "DIVERGED" and r["unexplained"] >= 1
    only = nr.reconcile(_fp(jobs=[_job("src.deals", True)]), _fp(jobs=[_job("src.deals", False)]),
                        _hb(outages=[(19 * 60 + 10, 19 * 60 + 50)]), DAY, NOW)
    assert only["verdict"] == "MATCH_WITH_OUTAGE_GAPS"


def test_a_network_outage_is_named_as_such():
    vm = _fp(jobs=[_job("src.deals", True)])
    node = _fp(jobs=[_job("src.deals", False)])
    r = nr.reconcile(vm, node, _hb(net_down=[(19 * 60 + 25, 19 * 60 + 45)]), DAY, NOW)
    issue = [i for i in r["issues"] if i["subject"] == "src.deals"][0]
    assert issue["explained"] and "network down" in issue["cause"]


def test_job_classification_by_design_not_due_not_yet_due_and_silent():
    vm = _fp(jobs=[_job("src.renew_token", True, "0 7 * * *"),
                   _job("scripts/publish_dashboard_mirror.sh", True, "5 21 * * *"),
                   _job("src.wk", False, "0 22 * * 5"),                       # Friday-only, today is Monday
                   _job("src.late", False, "0 23 * * *"),
                   _job("src.quiet", False, "0 12 * * *")])
    vm["jobs"][2]["due"] = False
    node = _fp(jobs=[_job("src.wk", False, "0 22 * * 5", due=False), _job("src.late", False, "0 23 * * *"),
                     _job("src.quiet", False, "0 12 * * *")])
    r = nr.reconcile(vm, node, _hb(), DAY, datetime(2026, 10, 12, 21, 0, tzinfo=IST))
    c = r["job_counts"]
    assert c["by_design"] == 2 and c["not_due"] == 1 and c["not_yet_due"] == 1 and c["no_evidence_both"] == 1
    assert r["verdict"] in ("MATCH", "MATCH_WITH_OUTAGE_GAPS") and not [i for i in r["issues"] if i["severity"] == "warn"
                                                                       and i["section"] == "jobs"]


def test_a_job_the_node_never_scheduled_is_a_difference():
    r = nr.reconcile(_fp(jobs=[_job("src.deals", True)]), _fp(jobs=[]), _hb(), DAY, NOW)
    assert r["verdict"] == "DIVERGED"
    assert "NOT SCHEDULED" in [i for i in r["issues"] if i["subject"] == "src.deals"][0]["node"]


def test_node_job_that_ran_but_logged_an_error_is_flagged():
    vm = _fp(jobs=[_job("src.deals", True)])
    node = _fp(jobs=[_job("src.deals", True, problem="Traceback (most recent call last)")])
    r = nr.reconcile(vm, node, _hb(), DAY, NOW)
    assert any("Traceback" in str(i["node"]) for i in r["issues"])


def test_artifact_rules_identical_timing_nondeterministic_and_absent():
    same = {"present": True, "sha256": "x", "fresh": True, "mtime": "m"}
    arts_vm = {"data/darling_tiers.json": same, "data/news_sentiment.json": dict(same, sha256="a"),
               "data/darlings_valuation.json": dict(same, sha256="v1"), "data/fo_liquidity.json": same,
               "data/darlings_levels.json": dict(same, sha256="L1")}
    arts_node = {"data/darling_tiers.json": same, "data/news_sentiment.json": dict(same, sha256="b"),
                 "data/darlings_valuation.json": dict(same, sha256="v2"),
                 "data/darlings_levels.json": dict(same, sha256="L2")}
    r = nr.reconcile(_fp(artifacts=arts_vm), _fp(artifacts=arts_node), _hb(), DAY, NOW)
    by = {i["subject"]: i for i in r["issues"] if i["section"] == "artifacts"}
    assert "data/darling_tiers.json" not in by                          # identical
    assert "data/news_sentiment.json" not in by                         # LLM output: freshness only, both fresh
    assert by["data/darlings_valuation.json"]["severity"] == "info"      # input timing: information
    assert by["data/darlings_levels.json"]["severity"] == "warn"         # state-like: a real difference
    assert by["data/fo_liquidity.json"]["node"] == "ABSENT"
    stale = {"data/darling_tiers.json": dict(same, fresh=False, sha256="old", mtime="2026-10-09")}
    r2 = nr.reconcile(_fp(artifacts={"data/darling_tiers.json": same}), _fp(artifacts=stale), _hb(outages=[(60, 120)]), DAY, NOW)
    assert [i for i in r2["issues"] if i["subject"] == "data/darling_tiers.json"][0]["explained"] is True


def test_ledger_and_decision_differences_and_the_outage_overlap_rule():
    vm_db = {"present": True, "tables": {"trade_tickets": {"rows": 10, "today": 2}}, "accounts": {
        "paper_accounts": [{"account_id": "A", "equity": 100.0}]}, "tickets_today": ["NIFTY|bull|up|approved"], "outcomes_today": []}
    nd_db = {"present": True, "tables": {"trade_tickets": {"rows": 9, "today": 1}}, "accounts": {
        "paper_accounts": [{"account_id": "A", "equity": 100.004}]}, "tickets_today": [], "outcomes_today": []}
    during = nr.reconcile(_fp(db=vm_db, journal=["NIFTY|bull|approved"]), _fp(db=nd_db), _hb(outages=[(10 * 60, 10 * 60 + 30)]), DAY, NOW)
    warn = [i for i in during["issues"] if i["severity"] == "warn"]
    assert warn and all(i["explained"] for i in warn)                   # node behind + outage in the session
    assert not [i for i in warn if i["section"] == "accounts"]          # 0.004 rupee drift is within tolerance
    assert during["verdict"] == "MATCH_WITH_OUTAGE_GAPS"
    quiet = nr.reconcile(_fp(db=vm_db, journal=["NIFTY|bull|approved"]), _fp(db=nd_db), _hb(), DAY, NOW)
    assert quiet["verdict"] == "DIVERGED"
    node_only = nr.reconcile(_fp(), _fp(journal=["TCS|iron|approved"]), _hb(outages=[(600, 630)]), DAY, NOW)
    assert node_only["verdict"] == "DIVERGED"                           # node-ahead is never an outage effect


def test_config_drift_unseeded_baseline_and_git_difference():
    r = nr.reconcile(_fp(git="78671dc", cfg="aaaa"), _fp(git="bc82455", cfg="bbbb", seed=False), _hb(), DAY, NOW)
    by = {i["subject"]: i for i in r["issues"]}
    assert by["git sha"]["severity"] == "info"
    assert by["config.json"]["severity"] == "warn" and by["baseline"]["severity"] == "warn"
    assert r["verdict"] == "DIVERGED"


def test_inconclusive_when_the_vm_cannot_be_read():
    r = nr.reconcile({"error": "gcloud CLI not found on the node"}, _fp(), _hb(), DAY, NOW)
    assert r["verdict"] == "INCONCLUSIVE" and "gcloud" in r["reason"]
    assert "could not be fingerprinted" in nr.render_markdown(r)


def test_a_perfect_match():
    vm, node = _fp(jobs=[_job("src.a", True)]), _fp(jobs=[_job("src.a", True)])
    r = nr.reconcile(vm, node, _hb(), DAY, NOW)
    assert r["verdict"] == "MATCH" and r["warn"] == 0


# ============================================================ report, card, the nightly run
def test_run_daily_files_a_report_and_posts_one_labelled_card(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_NODE_LABEL", "minipc1")
    root = _mk_host(tmp_path / "node", journal=[("NIFTY 50", "bull_call", "approved")])
    (root / "data" / ".node_seed.json").write_text(json.dumps({"vm_sha": "78671dc"}))
    hb_path = tmp_path / "hb.log"
    hb_path.write_text(_beats(DAY, gaps=[(19 * 60 + 10, 19 * 60 + 50)]))
    cron = '30 19 * * * cd "%s" && "%s/venv/bin/python" -m src.ingestion.deals_tracker >> "%s/logs/deals.log" 2>&1\n' % (root, root, root)
    monkeypatch.setattr(nf, "_crontab", lambda: cron)
    vm = nf.fingerprint(_mk_host(tmp_path / "vm", journal=[("NIFTY 50", "bull_call", "approved")]), DAY, crontab_text=cron)
    vm["jobs"][0]["ran"] = True                                          # the VM's log moved that day
    cards = []
    res = nr.run_daily(DAY, root=root, out_dir=tmp_path / "out", heartbeat_path=hb_path,
                       vm_fingerprint=vm, now=NOW, notify_fn=cards.append)
    md = (tmp_path / "out" / "2026-10-12.md").read_text()
    js = json.loads((tmp_path / "out" / "2026-10-12.json").read_text())
    assert js["verdict"] == res["verdict"] == "MATCH_WITH_OUTAGE_GAPS"
    assert "node outage 19:09–19:50" in md and "Limits of this comparison" in md
    assert len(cards) == 1 and cards[0]["event"] == "node_reconcile" and "2026-10-12" in cards[0]["text"]
    assert "19:09–19:50" in cards[0]["text"]                             # the outage window is on the card
    again = nr.run_daily(DAY, root=root, out_dir=tmp_path / "out", heartbeat_path=hb_path,
                         vm_fingerprint=vm, now=NOW, notify=False)
    assert again["verdict"] == res["verdict"] and len(cards) == 1        # idempotent, --no-notify honoured


def test_saturday_report_carries_the_week(tmp_path):
    sat = date(2026, 10, 17)
    out = tmp_path / "out"
    for k in range(1, 6):
        d = sat - timedelta(days=k)
        (out).mkdir(exist_ok=True)
        (out / f"{d.isoformat()}.json").write_text(json.dumps({"verdict": "MATCH", "heartbeat": {"outage_minutes": 0}}))
    (out / f"{(sat - timedelta(days=2)).isoformat()}.json").write_text(
        json.dumps({"verdict": "MATCH_WITH_OUTAGE_GAPS", "heartbeat": {"outage_minutes": 31}}))
    w = nr.week_summary(out, sat)
    assert "MATCH_WITH_OUTAGE_GAPS (31 min dark)" in w and w.count("MATCH") >= 5 and "no report" in w


def test_run_daily_never_raises():
    class Boom:
        def __getattr__(self, n): raise RuntimeError("x")
    r = nr.run_daily(DAY, root="/nonexistent/zzz", out_dir="/nonexistent/zzz/out", vm_fingerprint={"error": "no"},
                     notify=False, now=NOW)
    assert r["verdict"] in ("INCONCLUSIVE", "ERROR")


# ============================================================ the VM side is read-only
def test_the_vm_fingerprint_is_copied_run_and_deleted_nothing_else():
    calls = []

    def runner(cmd, timeout=180):
        calls.append(cmd)
        if "scp" in cmd:
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="noise\n" + json.dumps({"host": "alpha-trading-vm", "git": "78671dc"}), stderr="")

    fp = nr.fetch_vm_fingerprint(DAY, runner=runner, gcloud="/fake/gcloud")
    assert fp == {"host": "alpha-trading-vm", "git": "78671dc"}
    assert [("scp" if "scp" in c else "ssh") for c in calls] == ["scp", "ssh"]
    assert calls[0][-1].endswith(":/tmp/node_fingerprint.py") and calls[0][-2].endswith("node_fingerprint.py")
    remote = calls[1][-1]
    assert "node_fingerprint.py --root . --date 2026-10-12" in remote and "rm -f /tmp/node_fingerprint.py" in remote
    for forbidden in ("sudo", "systemctl", "crontab", "git pull", "git checkout", ">>", "tee ", "sed -i", "mv ", "kill"):
        assert forbidden not in remote, forbidden
    assert "rm" in remote and remote.count("rm ") == 1                   # the only deletion is its own temp file


def test_the_vm_fingerprint_failures_are_named_not_raised():
    from src import edge_miner as em
    with mock.patch.object(em, "_gcloud", return_value=None):
        assert "gcloud" in nr.fetch_vm_fingerprint(DAY)["error"]
    bad = lambda cmd, timeout=180: mock.Mock(returncode=255, stdout="", stderr="Connection timed out")  # noqa: E731
    with mock.patch("src.edge_miner.time.sleep"):
        err = nr.fetch_vm_fingerprint(DAY, runner=bad, gcloud="/fake/gcloud")["error"]
    assert "could not copy" in err and "timed out" in err
    # the copy worked but the run printed no JSON
    half = lambda cmd, timeout=180: mock.Mock(returncode=0, stdout="no json here", stderr="")  # noqa: E731
    assert "VM fingerprint failed" in nr.fetch_vm_fingerprint(DAY, runner=half, gcloud="/fake/gcloud")["error"]
    # a runner that raises never escapes
    def boom(cmd, timeout=180):
        raise RuntimeError("kaput")
    with mock.patch("src.edge_miner.time.sleep"):
        assert "error" in nr.fetch_vm_fingerprint(DAY, runner=boom, gcloud="/fake/gcloud")


# ============================================================ the VM's schedule, rendered for the node
def test_the_replica_block_is_the_vm_schedule_minus_exactly_two_jobs():
    mod = _load(SCRIPTS / "node_replica_block.py")
    kept, dropped = mod.render((SCRIPTS / "setup_cron.sh").read_text(), "/home/n/alpha_trading", "/home/n/alpha_trading/venv/bin/python")
    assert len(kept) >= 30 and len(dropped) == 3
    assert all("src.renew_token" in d or "publish_dashboard_mirror.sh" in d for d in dropped)
    joined = "\n".join(kept)
    for needed in ("src.master_scheduler", "src.ops_monitor", "src.sleep_phase", "src.analysis.macro_nightly",
                   "src.analysis.weekly_recalibration", "src.firm_treasury --rotate", "src.validation.run_proving_court"):
        assert needed in joined, needed
    assert "renew_token" not in joined and "publish_dashboard_mirror" not in joined
    assert "$" not in joined and "/home/n/alpha_trading/logs/" in joined   # every variable substituted
    # nothing in the real VM installer is lost: kept + dropped == every active schedule line in the heredoc
    active = [ln for ln in mod.heredoc((SCRIPTS / "setup_cron.sh").read_text()).splitlines()
              if mod._CRON.match(ln.strip()) and not ln.lstrip().startswith("#")]
    assert len(active) == len(kept) + len(dropped)


def test_the_nodes_heartbeat_list_is_the_vms_minus_the_renewal_log():
    from src import ops_monitor
    mod = _load(SCRIPTS / "node_replica_block.py")
    val = mod.ops_expected_jobs()
    assert "renew_token.log" not in val
    os.environ["OPS_EXPECTED_JOBS"] = val
    try:
        parsed = ops_monitor._expected_jobs_from_env()
    finally:
        del os.environ["OPS_EXPECTED_JOBS"]
    assert parsed == {k: v for k, v in ops_monitor.EXPECTED_JOBS.items() if k != "renew_token.log"}
    assert "master_scheduler.log:1" in val and "macro_nightly.log:0" in val


def _installer_copy(tmp_path):
    shutil.copytree(SCRIPTS, tmp_path / "scripts")
    (tmp_path / "src").symlink_to(ROOT / "src")                     # node_replica_block --ops-env imports it
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").symlink_to(sys.executable)
    return tmp_path / "scripts" / "setup_node_replica_cron.sh"


def test_the_replica_installer_block(tmp_path):
    if os.uname().sysname == "Darwin":
        return
    inst = _installer_copy(tmp_path)
    env = dict(os.environ, TZ="Asia/Kolkata")
    plain = subprocess.run(["bash", str(inst), "--dry-run"], capture_output=True, text=True, env=env, timeout=60)
    assert plain.returncode == 0, plain.stderr
    out = plain.stdout
    assert out.startswith("# === ALPHA TRADING NODE REPLICA BLOCK START") and out.rstrip().endswith("BLOCK END ===")
    assert "ALPHA_NODE_LABEL=minipc1" in out and "ALPHA_NODE_REPLICA=1" in out
    assert "scripts/node_heartbeat.sh" in out and "-m src.node_reconcile" in out and out.count("\n* * * * * ") == 1
    assert "node_token_mirror" not in out                                   # off unless asked for
    assert "\nOPS_EXPECTED_JOBS=" in out and "renew_token.log" not in out
    # the preflight greps the crontab for these: a replica block must never trip it
    for forbidden in ("renew_token", "push_token_to_vm", "setup_cron.sh", "publish_dashboard_mirror"):
        assert forbidden not in out, forbidden
    # no catch-up @reboot lines: a missed job IS the measurement
    assert "@reboot" not in out
    tok = subprocess.run(["bash", str(inst), "--dry-run", "--with-token"], capture_output=True, text=True, env=env, timeout=60)
    assert "15 7 * * * " in tok.stdout and "node_token_mirror.sh" in tok.stdout
    bad = subprocess.run(["bash", str(inst), "--bogus"], capture_output=True, text=True, env=env, timeout=60)
    assert bad.returncode == 2
    wrong_clock = subprocess.run(["bash", str(inst), "--dry-run"], capture_output=True, text=True,
                                 env=dict(os.environ, TZ="UTC"), timeout=60)
    assert wrong_clock.returncode == 1 and "+0530" in wrong_clock.stderr


# ============================================================ the token mirror
def _jwt(exp):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'alg': 'HS512'})}.{b({'exp': exp, 'dhanClientId': '1'})}." + "s" * 90


def test_token_mirror_swaps_only_the_token_line_and_never_prints_it(tmp_path):
    mod = _load(SCRIPTS / "node_token_mirror.py")
    env = tmp_path / ".env"
    env.write_text("DHAN_CLIENT_ID=1\nDHAN_ACCESS_TOKEN=OLD\nDISCORD_WEBHOOK_URL=https://x\n")
    tok = _jwt(time.time() + 6 * 3600)
    ok, msg = mod.mirror("DHAN_ACCESS_TOKEN=" + tok, env)
    assert ok and tok not in msg and "expires" in msg
    lines = env.read_text().splitlines()
    assert lines == ["DHAN_CLIENT_ID=1", "DHAN_ACCESS_TOKEN=" + tok, "DISCORD_WEBHOOK_URL=https://x"]
    assert (tmp_path / ".env.bak").read_text().count("OLD") == 1
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_token_mirror_refuses_expired_malformed_and_foreign_lines(tmp_path):
    mod = _load(SCRIPTS / "node_token_mirror.py")
    env = tmp_path / ".env"
    env.write_text("DHAN_ACCESS_TOKEN=KEEP\n")
    for line in ("DHAN_ACCESS_TOKEN=" + _jwt(time.time() - 10), "DHAN_ACCESS_TOKEN=" + _jwt(time.time() + 120),
                 "DHAN_ACCESS_TOKEN=short", "DHAN_PIN=1234", "", "garbage"):
        ok, msg = mod.mirror(line, env)
        assert not ok
        assert env.read_text() == "DHAN_ACCESS_TOKEN=KEEP\n"
        assert "1234" not in msg


def test_token_mirror_script_reads_and_never_renews():
    src = (SCRIPTS / "node_token_mirror.sh").read_text() + (SCRIPTS / "node_token_mirror.py").read_text()
    for forbidden in ("src.renew_token", "request_v2_token", "generateAccessToken", "DHAN_PIN", "DHAN_TOTP", "push_token_to_vm"):
        assert forbidden not in src, forbidden
    assert "grep -m1 '^DHAN_ACCESS_TOKEN='" in src and "node_token_mirror.py" in src


# ============================================================ seed script guards
def test_the_seed_script_is_read_only_on_the_vm_and_verifies_before_it_overwrites():
    if os.uname().sysname == "Darwin":
        return
    src = (SCRIPTS / "node_seed_from_vm.sh").read_text()
    assert src.index("integrity_check") < src.index("cp \"$SEED/brain_map.db\"")        # verify, then install
    assert src.index("Installing on the node") > src.index("Verifying before touching the node")
    assert "seed_backup_" in src and ".node_seed.json" in src
    assert "mirror_snapshot" in src                                       # the existing consistent, read-only door
    assert "NODE REPLICA BLOCK START" in src                              # refuses while jobs could be writing
    assert "unsafe tar paths" in src
    for forbidden in ("alpha-trading-vm:~", "alpha-trading-vm:/home", "systemctl", "sudo ", "| crontab -", "git pull", "> ~/alpha_trading"):
        assert forbidden not in src, forbidden
    # the only thing it deletes on the VM is its own temp directory
    assert src.count("rm -rf /tmp/node_seed") == 2
    r = subprocess.run(["bash", str(SCRIPTS / "node_seed_from_vm.sh")], capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert r.returncode == 2 and "--yes" in r.stderr


# ============================================================ the heartbeat
def test_the_heartbeat_appends_one_parseable_line_per_run(tmp_path):
    if os.uname().sysname == "Darwin":
        return
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "node_heartbeat.sh", tmp_path / "scripts" / "node_heartbeat.sh")
    for _ in range(2):
        subprocess.run(["bash", str(tmp_path / "scripts" / "node_heartbeat.sh")], check=True, timeout=30)
    lines = (tmp_path / "logs" / "node_heartbeat.log").read_text().splitlines()
    assert len(lines) == 2
    epoch, iso, net, up = lines[0].split("|")
    assert abs(float(epoch) - time.time()) < 60 and net in ("net=ok", "net=down") and up.startswith("up=")
    day = datetime.fromtimestamp(float(epoch), IST).date()
    hb = nr.read_heartbeat(tmp_path / "logs" / "node_heartbeat.log", day,
                           datetime.fromtimestamp(float(epoch), IST) + timedelta(minutes=1))
    assert hb["available"] and hb["beats"] == 2


def test_new_scripts_parse():
    for name in ("setup_node_replica_cron.sh", "node_seed_from_vm.sh", "node_token_mirror.sh", "node_heartbeat.sh"):
        r = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"

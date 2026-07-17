"""
tests/test_ceo_brief.py — the Daily CEO Brief (Department 6)

Hermetic: no network, no Discord, no real clock. Every seam is injected;
the only filesystem touched is pytest's tmp_path.

The load-bearing test in this file is
`test_sweep_does_not_consume_ops_monitor_state` — the brief must never
blind the 20:30 ops card.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import ceo_brief

IST = timezone(timedelta(hours=5, minutes=30))


def _clock(y=2026, m=7, d=18, hh=16, mm=0):
    return lambda: datetime(y, m, d, hh, mm, tzinfo=IST)


def _touch_on(path, when):
    """Set a log's mtime so heartbeat freshness is deterministic.

    check_heartbeats compares the log's mtime DATE against the clock's date,
    so a test that writes a file "now" and injects a different date would be
    testing the calendar, not the code.
    """
    import os
    naive = when.replace(tzinfo=None)
    ts = naive.timestamp()
    os.utime(path, (ts, ts))


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# Issue bucketing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("  Dhan quote error for RELIANCE.NS: timed out", "Dhan API / data"),
    ("  Dhan historical fetch error: 429 too many requests", "Dhan API / data"),
    ("LTIM unavailable: not found in scrip master", "Dead / unknown ticker"),
    ("TATAMOTORS demerged — security id stale, fetch failed",
     "Dead / unknown ticker"),
    ("margin gate denied proposal", "Margin / capital"),
    ("drawdown halt engaged — risk-of-ruin", "Margin / capital"),
    ("token expired, 401 refused", "Token / auth"),
    ("something else broke entirely", "Other errors"),
])
def test_bucket_issue(line, expected):
    assert ceo_brief.bucket_issue(line) == expected


def test_bucket_precedence_ticker_beats_dhan():
    """Most-specific-first: a Dhan error ABOUT a dead ticker is a ticker issue."""
    assert ceo_brief.bucket_issue(
        "Dhan quote error for LTIM: not found in scrip master"
    ) == "Dead / unknown ticker"


def test_bucket_lines_are_ones_ops_monitor_actually_flags():
    """Guard: bucketing only ever sees lines ops_monitor calls problems.

    A bucket pattern that only matches text ops_monitor ignores is dead code.
    """
    from src.ops_monitor import is_problem_line
    for line in ("  Dhan quote error for RELIANCE.NS: timed out",
                 "LTIM unavailable: not found in scrip master",
                 "margin gate denied proposal",
                 "token expired, 401 refused"):
        assert is_problem_line(line), line


def test_bucket_handles_empty_and_none():
    assert ceo_brief.bucket_issue("") == "Other errors"
    assert ceo_brief.bucket_issue(None) == "Other errors"


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def test_operations_all_green(logs):
    log = logs / "renew_token.log"
    log.write_text("ok")
    _touch_on(log, _clock()())
    ops = ceo_brief.collect_operations(
        logs_dir=logs, clock=_clock(), expected={"renew_token.log": False})
    assert ops["available"] and ops["ok"]
    assert ops["missing"] == []


def test_operations_flags_silent_job(logs):
    ops = ceo_brief.collect_operations(
        logs_dir=logs, clock=_clock(), expected={"renew_token.log": False})
    assert ops["ok"] is False
    assert "renew_token.log" in ops["missing"][0]


def test_evening_jobs_are_not_due_at_the_brief(logs):
    """The card must not cry wolf about jobs that haven't had their slot.

    8 of the 13 monitored jobs run 18:50-20:30. At the 16:30 brief they are
    pending, not silent — the 20:30 ops sweep is what judges them.
    """
    from src.ops_monitor import EXPECTED_JOBS
    ops = ceo_brief.collect_operations(logs_dir=logs, clock=_clock(hh=16, mm=30),
                                       expected=dict(EXPECTED_JOBS))
    assert set(ops["pending"]) == {
        "rss_ingester.log", "news_processor.log", "earnings_calendar.log",
        "deals_tracker.log", "flows_tracker.log", "daily_archiver.log",
        "sleep_phase.log", "discovery_nightly.log"}
    # Only the 5 morning/afternoon jobs are judged.
    assert ops["expected_count"] == 5
    for name in ops["pending"]:
        assert all(name not in m for m in ops["missing"])
    assert "not due yet" in ceo_brief._ops_field(ops)["value"]


def test_every_job_is_due_at_the_2030_sweep_hour(logs):
    """By 20:30 nothing is pending — the filter only defers, never excuses."""
    from src.ops_monitor import EXPECTED_JOBS
    ops = ceo_brief.collect_operations(logs_dir=logs, clock=_clock(hh=20, mm=50),
                                       expected=dict(EXPECTED_JOBS))
    assert ops["pending"] == []
    assert ops["expected_count"] == len(EXPECTED_JOBS)


def test_due_filter_covers_every_monitored_job(logs):
    """Drift guard: a job ops_monitor watches but JOB_DUE_HOUR doesn't know.

    An unknown job is treated as DUE (reported, never silently excused), so
    this asserts the maps agree rather than that the fallback is unused.
    """
    from src.ops_monitor import EXPECTED_JOBS
    assert set(EXPECTED_JOBS) == set(ceo_brief.JOB_DUE_HOUR), (
        "scripts/setup_cron.sh, ops_monitor.EXPECTED_JOBS and "
        "ceo_brief.JOB_DUE_HOUR have drifted apart")


def test_unknown_job_is_reported_not_excused():
    now = _clock(hh=16)()
    due, pending = ceo_brief.jobs_due_by(now, {"mystery.log": False})
    assert due == {"mystery.log": False} and pending == []


def test_due_grace_period(logs):
    """A job is not 'silent' the same minute its cron fires."""
    jobs = {"main.log": True}          # due 15:36 IST
    _, pending = ceo_brief.jobs_due_by(_clock(hh=15, mm=40)(), jobs)
    assert pending == ["main.log"]     # inside the 30-min grace
    _, pending = ceo_brief.jobs_due_by(_clock(hh=16, mm=30)(), jobs)
    assert pending == []               # grace elapsed — now it's judged


def test_1600_would_be_too_early_for_the_post_close_jobs():
    """WHY the cron says 16:30, not 16:00 (the owner's first suggestion).

    main (15:35) and chain_archiver (15:40) are still inside their 30-min
    grace at 16:00, so a 16:00 brief could judge only 3 of 13 jobs. 16:30 is
    the first slot that covers every post-close job.
    """
    from src.ops_monitor import EXPECTED_JOBS
    due_at_1600, _ = ceo_brief.jobs_due_by(_clock(hh=16, mm=0)(),
                                           dict(EXPECTED_JOBS))
    due_at_1630, _ = ceo_brief.jobs_due_by(_clock(hh=16, mm=30)(),
                                           dict(EXPECTED_JOBS))
    assert len(due_at_1600) == 3
    assert len(due_at_1630) == 5
    assert {"main.log", "chain_archiver.log"} <= set(due_at_1630)


def test_operations_fails_open(monkeypatch, logs):
    def boom(*a, **k):
        raise RuntimeError("heartbeat exploded")
    monkeypatch.setattr("src.ops_monitor.check_heartbeats", boom)
    ops = ceo_brief.collect_operations(logs_dir=logs, clock=_clock())
    assert ops["available"] is False
    # The card still renders rather than raising.
    assert "unavailable" in ceo_brief._ops_field(ops)["value"]


# --------------------------------------------------------------------------
# Issues — including the anti-theft guarantee
# --------------------------------------------------------------------------

def test_issues_buckets_problem_lines(logs, tmp_path):
    (logs / "market.log").write_text(
        "  Dhan quote error for RELIANCE.NS: timed out\n"
        "LTIM unavailable: not found in scrip master\n"
        "all good here\n"
    )
    res = ceo_brief.collect_issues(logs_dir=logs,
                                   state_path=tmp_path / "state.json")
    assert res["available"] and res["total"] == 2
    assert set(res["buckets"]) == {"Dhan API / data", "Dead / unknown ticker"}


def test_issues_clean_day(logs, tmp_path):
    (logs / "market.log").write_text("cycle complete, 3 proposals\n")
    res = ceo_brief.collect_issues(logs_dir=logs,
                                   state_path=tmp_path / "state.json")
    assert res["total"] == 0
    assert "No problem lines" in ceo_brief._issues_field(res)["value"]


def test_sweep_does_not_consume_ops_monitor_state(logs, tmp_path):
    """THE constraint: the brief must not blind the 20:30 ops card.

    ops_monitor.sweep_logs is incremental — whoever advances the offset
    'consumes' the line. The brief keeps its own offset, so ops_monitor
    must still see the same problem afterwards.
    """
    from src import ops_monitor

    (logs / "market.log").write_text("  Dhan quote error for X: timed out\n")
    ops_state = tmp_path / "ops_state.json"

    brief = ceo_brief.collect_issues(logs_dir=logs,
                                     state_path=tmp_path / "ceo_state.json")
    assert brief["total"] == 1

    # ops_monitor, with its OWN untouched state, still sees the line.
    problems, _ = ops_monitor.sweep_logs(logs, ops_monitor._load_state(ops_state))
    assert len(problems) == 1
    assert "Dhan quote error" in problems[0]["line"]


def test_brief_does_not_write_the_problem_ledger(logs, tmp_path, monkeypatch):
    """ops_monitor owns logs/problems.jsonl — the brief reads, never appends."""
    called = []
    monkeypatch.setattr("src.ops_monitor.record_problems",
                        lambda *a, **k: called.append(a))
    (logs / "market.log").write_text("margin gate denied proposal\n")
    ceo_brief.collect_issues(logs_dir=logs, state_path=tmp_path / "s.json")
    assert called == []


def test_issues_offset_advances_between_runs(logs, tmp_path):
    state = tmp_path / "state.json"
    log = logs / "market.log"
    log.write_text("  Dhan quote error for X: timed out\n")
    first = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert first["total"] == 1
    # Same file, unchanged: already consumed by THIS module's own offset.
    second = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert second["total"] == 0
    # A new line is picked up.
    with open(log, "a") as fh:
        fh.write("margin gate denied proposal\n")
    third = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert third["total"] == 1


def test_issues_corrupt_state_file_recovers(logs, tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json")
    (logs / "market.log").write_text("  Dhan quote error for X: timed out\n")
    res = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert res["available"] and res["total"] == 1


# --------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------

def _deploy_line(service, sha, dirty=False, event="deploy"):
    return json.dumps({"ts": "2026-07-18T09:00:00+05:30", "service": service,
                       "sha": sha, "subject": "s", "committed": "",
                       "dirty": dirty, "event": event})


def test_live_version_reads_latest_per_service(tmp_path):
    p = tmp_path / "deploy_log.jsonl"
    p.write_text("\n".join([
        _deploy_line("api_server", "aaaaaaa"),
        _deploy_line("discord_bot", "aaaaaaa"),
        _deploy_line("api_server", "6d89eb4"),
    ]) + "\n")
    live = ceo_brief.live_version(p)
    assert live["available"]
    assert live["services"]["api_server"]["sha"] == "6d89eb4"
    assert live["consistent"] is False   # bot still on the old sha


def test_live_version_consistent(tmp_path):
    p = tmp_path / "deploy_log.jsonl"
    p.write_text(_deploy_line("api_server", "6d89eb4") + "\n"
                 + _deploy_line("discord_bot", "6d89eb4") + "\n")
    live = ceo_brief.live_version(p)
    assert live["consistent"] is True and live["dirty"] is False


def test_live_version_flags_dirty_tree(tmp_path):
    p = tmp_path / "deploy_log.jsonl"
    p.write_text(_deploy_line("api_server", "6d89eb4", dirty=True) + "\n")
    assert ceo_brief.live_version(p)["dirty"] is True


def test_live_version_tolerates_torn_line(tmp_path):
    p = tmp_path / "deploy_log.jsonl"
    p.write_text(_deploy_line("api_server", "6d89eb4") + "\n{half-writ\n")
    assert ceo_brief.live_version(p)["services"]["api_server"]["sha"] == "6d89eb4"


def test_live_version_missing_file(tmp_path):
    live = ceo_brief.live_version(tmp_path / "nope.jsonl")
    assert live["available"] is False
    assert "No deploy record" in ceo_brief._deploy_field(
        {"live": live, "work": {"available": False}, "host": "h"})["value"]


def test_deploy_field_stamps_host():
    field = ceo_brief._deploy_field(
        {"live": {"available": False}, "work": {"available": False},
         "host": "alpha-trading-vm"})
    assert "alpha-trading-vm" in field["value"]


def test_deploy_field_shouts_on_split_deploy(tmp_path):
    p = tmp_path / "deploy_log.jsonl"
    p.write_text(_deploy_line("api_server", "6d89eb4") + "\n"
                 + _deploy_line("discord_bot", "0cafdb6") + "\n")
    field = ceo_brief._deploy_field(
        {"live": ceo_brief.live_version(p), "work": {"available": False},
         "host": "h"})
    assert "DIFFERENT shas" in field["value"]


def test_todays_commits_handles_non_repo(tmp_path):
    out = ceo_brief.todays_commits(repo_root=tmp_path, clock=_clock())
    # Not a git repo: honest empty, never a raise.
    assert out["commits"] == []


# --------------------------------------------------------------------------
# Risk & capital
# --------------------------------------------------------------------------

def test_collect_risk_reuses_eod_summary(tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text("\n".join([
        json.dumps({"decision": "approved", "ticker": "NIFTY",
                    "spread": {"strategy": "bull_call_spread", "lots": 2,
                               "lot_size": 50}}),
        json.dumps({"decision": "approved", "ticker": "ONGC",
                    "outcome": {"exit_date": __import__("datetime").date.today()
                                .isoformat(), "pnl_rs": 1500.0}}),
    ]) + "\n")
    risk = ceo_brief.collect_risk(journal_path=journal)
    assert risk["available"]
    assert risk["daily_pnl"] == 1500.0
    assert risk["resolved"] == 1
    assert risk["open_spreads"] == 1
    assert risk["net_delta"] == 50.0        # 1.0 bias * 0.5 delta * 2 * 50


def test_risk_field_flat_and_loss():
    field = ceo_brief._risk_field({
        "available": True, "daily_pnl": -800.0, "resolved": 2,
        "open_spreads": 0, "open_equities": 0, "net_delta": 0.0})
    assert "Rs.-800" in field["value"] and "flat" in field["value"]


def test_risk_field_unavailable():
    field = ceo_brief._risk_field({"available": False})
    assert "unavailable" in field["value"]


# --------------------------------------------------------------------------
# The card + the notifier seam
# --------------------------------------------------------------------------

def test_build_brief_card_shape(logs, tmp_path):
    (logs / "market.log").write_text("all fine\n")
    card = ceo_brief.build_brief_card(
        logs_dir=logs, state_path=tmp_path / "s.json",
        deploy_log_path=tmp_path / "none.jsonl", repo_root=tmp_path,
        journal_path=tmp_path / "none.jsonl", clock=_clock())
    assert card["event"] == "ceo_brief"
    assert card["date"] == "2026-07-18"
    assert len(card["fields"]) == 4
    names = " ".join(f["name"] for f in card["fields"])
    for expected in ("Operations", "Issues", "Deployments", "Risk"):
        assert expected in names


def test_card_renders_through_the_real_notifier(logs, tmp_path):
    """The payload must survive the Department-6 manager's embed builder."""
    from src.notifier import _build_embed
    card = ceo_brief.build_brief_card(
        logs_dir=logs, state_path=tmp_path / "s.json",
        deploy_log_path=tmp_path / "none.jsonl", repo_root=tmp_path,
        journal_path=tmp_path / "none.jsonl", clock=_clock())
    embed = _build_embed(card)
    assert "CEO Brief" in embed["title"]
    assert embed["color"] == 0x1ABC9C          # not the grey unknown-event fallback
    assert len(embed["fields"]) == 4           # fields passed through, not dropped


def test_send_brief_routes_through_fire_broadcast(monkeypatch, logs, tmp_path):
    """Department 6 non-negotiable: ONE Discord door."""
    sent = []
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda p: sent.append(p))
    payload = ceo_brief.send_brief(
        logs_dir=logs, state_path=tmp_path / "s.json",
        deploy_log_path=tmp_path / "none.jsonl", repo_root=tmp_path,
        journal_path=tmp_path / "none.jsonl", clock=_clock())
    assert len(sent) == 1
    assert sent[0] is payload
    assert sent[0]["event"] == "ceo_brief"


def test_module_never_imports_a_second_discord_path():
    """No httpx / discord_client reach-around — everything via notifier."""
    src = (ceo_brief.ROOT / "src" / "ceo_brief.py").read_text()
    assert "discord_client" not in src
    assert "httpx" not in src
    assert "send_webhook_message" not in src


def test_dry_run_sends_nothing(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr("src.notifier.fire_broadcast", lambda p: sent.append(p))
    assert ceo_brief.main(["--dry-run"]) == 0
    assert sent == []
    assert "dry run" in capsys.readouterr().out


def test_clean_day_description(logs, tmp_path):
    card = ceo_brief.build_brief_card(
        logs_dir=logs, state_path=tmp_path / "s.json",
        deploy_log_path=tmp_path / "none.jsonl", repo_root=tmp_path,
        journal_path=tmp_path / "none.jsonl", clock=_clock(),
    )
    # No expected jobs ran in an empty tmp logs dir -> not a clean day.
    assert "Attention needed" in card["description"]

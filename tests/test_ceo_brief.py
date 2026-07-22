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

# Decision #84 discord budget: OFF here — these tests exercise the raw
# send/card path; the gate has its own suite (test_discord_budget.py).
import src.config as _cfg
_cfg.DISCORD_BUDGET_ENABLED = False

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


def _warm_state(path):
    """An offset file that EXISTS but is empty — i.e. the brief has run here
    before and simply has no offsets yet.

    Most tests below are about bucketing and offset arithmetic, not about
    first-run behaviour. Since 2026-07-20 a MISSING state file means "cold
    start: baseline, report nothing" (see collect_issues), so those tests
    have to say they are not a first run. Cold start has its own tests.
    """
    from pathlib import Path as _P
    _P(path).write_text("{}")
    return path


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
    res = ceo_brief.collect_issues(
        logs_dir=logs, state_path=_warm_state(tmp_path / "state.json"))
    assert res["available"] and res["total"] == 2
    assert set(res["buckets"]) == {"Dhan API / data", "Dead / unknown ticker"}


def test_issues_clean_day(logs, tmp_path):
    (logs / "market.log").write_text("cycle complete, 3 proposals\n")
    res = ceo_brief.collect_issues(
        logs_dir=logs, state_path=_warm_state(tmp_path / "state.json"))
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

    brief = ceo_brief.collect_issues(
        logs_dir=logs, state_path=_warm_state(tmp_path / "ceo_state.json"))
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
    state = _warm_state(tmp_path / "state.json")
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
    # Worded for the owner, not for a release engineer — assert the WARNING
    # survives rewording, not the exact sentence.
    assert "⚠️" in field["value"]
    assert "half-finished" in field["value"]
    assert "6d89eb4" in field["value"] and "0cafdb6" in field["value"]


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
    assert "Rs.-800" in field["value"]
    assert "market-neutral" in field["value"]   # zero delta, in plain English


def test_risk_field_unavailable():
    field = ceo_brief._risk_field({"available": False})
    assert "unavailable" in field["value"]


# --------------------------------------------------------------------------
# The card + the notifier seam
# --------------------------------------------------------------------------

def test_build_brief_card_shape(logs, tmp_path):
    (logs / "market.log").write_text("all fine\n")
    card = ceo_brief.build_brief_card(
        logs_dir=logs, state_path=_warm_state(tmp_path / "s.json"),
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
        logs_dir=logs, state_path=_warm_state(tmp_path / "s.json"),
        deploy_log_path=tmp_path / "none.jsonl", repo_root=tmp_path,
        journal_path=tmp_path / "none.jsonl", clock=_clock(),
    )
    # No expected jobs ran in an empty tmp logs dir -> not a clean day.
    assert "Attention needed" in card["description"]


# --------------------------------------------------------------------------
# Readability — the 2026-07-20 card was a wall of machine text
# --------------------------------------------------------------------------

def test_clip_preserves_line_breaks():
    """The bug that made the first real card unreadable: the whole Issues
    body went through `_truncate`, which collapses ALL whitespace, so every
    bullet ran into one paragraph. `_clip` must trim without flattening."""
    body = "\n".join(f"• line number {i}" for i in range(200))
    out = ceo_brief._clip(body, 1000)
    assert len(out) <= 1000
    assert "\n" in out                       # structure survived
    assert out.startswith("• line number 0")
    assert "trimmed" in out                  # says it was cut

    short = "one\ntwo\nthree"
    assert ceo_brief._clip(short, 1000) == short   # untouched under the cap


def test_humanize_issue_reads_the_real_2026_07_20_lines():
    """Each input is verbatim from the logs behind the first CEO brief."""
    h = ceo_brief.humanize_issue

    bad_flags = h("corporate_events.py: error: unrecognized arguments: "
                  "--backfill 2019-01-01 --throttle 6")
    assert "corporate_events.py" in bad_flags
    assert "does not accept" in bad_flags and "did nothing" in bad_flags

    dhan = h("Dhan historical returned: {'status': 'failure', 'remarks': "
             "{'error_code': 'DH-905', 'error_type': 'Input_Exception'}}")
    assert "DH-905" in dhan and "malformed" in dhan
    assert "'status'" not in dhan            # the dict noise is gone

    intraday = h('{"ts": "2026-07-17T09:15:01+05:30", "captured": 8, '
                 '"failed": 2, "tickers": 10, "out": "/home/x/y.json"}')
    assert "8 of 10" in intraday and "2 failed" in intraday

    failopen = h("deals tracker: NSE live fetch failed [The read operation "
                 "timed out] — falling open to the local snapshot")
    assert "timed out" in failopen and "stale" in failopen


def test_humanize_issue_falls_back_to_the_raw_line():
    """Unknown shape: show the truth, clipped — never invent a reading."""
    odd = "some entirely novel subsystem exploded in a new way"
    assert ceo_brief.humanize_issue(odd) == odd
    assert ceo_brief.humanize_issue("") == ""


def test_humanize_issue_maps_the_token_expiry_code():
    """DH-901 is the token error the owner has actually hit before."""
    out = ceo_brief.humanize_issue("historical failed: DH-901")
    assert "expired" in out and "DH-901" in out


def test_plain_subject_strips_conventional_commit_prefix():
    assert ceo_brief._plain_subject(
        "feat(analysis): darling shadow leg — RIPE names paper-trade"
    ).startswith("Darling shadow leg")
    assert ceo_brief._plain_subject("fix: survive Gemini's array reply") == \
        "Survive Gemini's array reply"
    # No prefix: left alone apart from sentence-casing.
    assert ceo_brief._plain_subject("merge branch main") == "Merge branch main"


def test_human_time_is_readable_and_never_raises():
    assert ceo_brief._human_time("2026-07-20T15:33:04+05:30") == \
        "20 Jul, 3:33 PM"
    assert ceo_brief._human_time("not a timestamp")            # no raise
    assert ceo_brief._human_time("") == "time unknown"


def test_issues_field_is_readable_prose_not_log_dump():
    issues = {"available": True, "total": 15,
              "buckets": {"Dhan API / data": [
                  {"log": "suggest.log", "count": 14,
                   "line": "Dhan historical returned: {'error_code': "
                           "'DH-905', 'error_type': 'Input_Exception'}"}]}}
    value = ceo_brief._issues_field(issues)["value"]
    assert "\n" in value                       # not one paragraph
    assert "happened 14 times" in value        # not "x14"
    assert "DH-905" in value and "malformed" in value
    assert "'error_type'" not in value         # raw dict never reaches the card
    assert len(value) <= 1024                  # Discord's field cap


# --------------------------------------------------------------------------
# Cold start — the root cause of the 63-line first card (2026-07-20)
# --------------------------------------------------------------------------

def test_first_ever_brief_baselines_instead_of_replaying_history(logs, tmp_path):
    """THE 2026-07-20 bug. The brief keeps its own offset file; on its first
    run that file did not exist, so `sweep_logs` read every log from byte 0
    and reported weeks of history as "since the last brief" — including a
    corporate_events crash already fixed by 6d89eb4. A first run must record
    where the logs currently END, not indict the past."""
    (logs / "market.log").write_text(
        "  Dhan quote error for ANCIENT.NS: timed out\n"
        "margin gate denied proposal\n")
    state = tmp_path / "state.json"
    assert not state.exists()

    res = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert res["available"]
    assert res["cold_start"] is True
    assert res["total"] == 0            # history NOT replayed
    assert res["buckets"] == {}

    # And it says so, rather than claiming the day was clean.
    value = ceo_brief._issues_field(res)["value"]
    assert "First brief on this box" in value
    assert "No problem lines" not in value


def test_cold_start_is_not_reported_as_a_clean_day(logs, tmp_path):
    """A baseline is 'we did not look', which must never be banked as
    'nothing broke' — the card would be taking credit for an unrun check."""
    card = ceo_brief.build_brief_card(
        logs_dir=logs, state_path=tmp_path / "cold.json",
        deploy_log_path=tmp_path / "none.jsonl", repo_root=tmp_path,
        journal_path=tmp_path / "none.jsonl", clock=_clock())
    assert "Clean day" not in card["description"]
    assert "First brief" in card["description"]


def test_the_run_after_a_cold_start_reports_normally(logs, tmp_path):
    """Baselining defers reporting by one run — it must not disable it."""
    log = logs / "market.log"
    log.write_text("  Dhan quote error for OLD.NS: timed out\n")
    state = tmp_path / "state.json"

    first = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert first["cold_start"] and first["total"] == 0

    with open(log, "a") as fh:
        fh.write("margin gate denied proposal\n")
    second = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert second["cold_start"] is False
    assert second["total"] == 1                      # the NEW line only
    assert "Margin / capital" in second["buckets"]


def test_cold_start_does_not_blind_the_2030_ops_card(logs, tmp_path):
    """The load-bearing constraint still holds through the new path: the
    brief baselining must not consume anything ops_monitor needs."""
    from src import ops_monitor
    (logs / "market.log").write_text("  Dhan quote error for X: timed out\n")

    ceo_brief.collect_issues(logs_dir=logs, state_path=tmp_path / "ceo.json")

    problems, _ = ops_monitor.sweep_logs(
        logs, ops_monitor._load_state(tmp_path / "ops.json"))
    assert len(problems) == 1
    assert "Dhan quote error" in problems[0]["line"]


def test_corrupt_offset_file_replays_rather_than_baselining(logs, tmp_path):
    """A corrupt file EXISTS, so it is not a cold start. Baselining past a
    corruption would silently swallow real problems — replay instead."""
    state = tmp_path / "state.json"
    state.write_text("{not json")
    (logs / "market.log").write_text("margin gate denied proposal\n")

    res = ceo_brief.collect_issues(logs_dir=logs, state_path=state)
    assert res["cold_start"] is False
    assert res["total"] == 1


def test_ops_monitor_keeps_its_historical_replay_by_default(logs):
    """The option is opt-in: ops_monitor's own nightly sweep is unchanged."""
    from src.ops_monitor import sweep_logs
    (logs / "market.log").write_text("margin gate denied proposal\n")

    replayed, _ = sweep_logs(logs, {})
    assert len(replayed) == 1

    baselined, state = sweep_logs(logs, {}, baseline_cold_start=True)
    assert baselined == []
    assert state["market.log"] > 0        # offset parked at EOF


# --------------------------------------------------------------------------
# Scheduled jobs are not "services" (2026-07-20 false split-deploy alarm)
# --------------------------------------------------------------------------

def _job_line(service, sha, ts="2026-07-20T09:10:00+05:30", kind="job"):
    return json.dumps({"ts": ts, "service": service, "kind": kind, "sha": sha,
                       "subject": "s", "committed": "", "dirty": False,
                       "event": "deploy"})


def test_finished_cron_job_is_not_a_half_finished_deploy(tmp_path):
    """THE 2026-07-20 false alarm, verbatim: master_scheduler started 09:10
    on c80c10d and EXITED at 15:30; the two real services restarted 15:33 on
    facd767. That is a normal day with a commit in it, not a split deploy.
    Comparing an exited job's sha against live services would cry wolf every
    single day a commit lands mid-session."""
    p = tmp_path / "deploy_log.jsonl"
    p.write_text("\n".join([
        _job_line("master_scheduler", "c80c10d"),
        json.dumps({"ts": "2026-07-20T15:33:00+05:30", "service": "api_server",
                    "kind": "service", "sha": "facd767", "subject": "s",
                    "committed": "", "dirty": False, "event": "deploy"}),
        json.dumps({"ts": "2026-07-20T15:33:00+05:30", "service": "discord_bot",
                    "kind": "service", "sha": "facd767", "subject": "s",
                    "committed": "", "dirty": False, "event": "deploy"}),
    ]) + "\n")

    live = ceo_brief.live_version(p)
    assert live["consistent"] is True                 # no false alarm
    assert set(live["services"]) == {"api_server", "discord_bot"}
    assert set(live["jobs"]) == {"master_scheduler"}

    value = ceo_brief._deploy_field(
        {"live": live, "work": {"available": False}, "host": "vm"})["value"]
    assert "half-finished" not in value
    assert "master_scheduler" in value                # reported, not hidden
    assert "already finished" in value


def test_a_genuine_service_split_still_shouts(tmp_path):
    """Guard the fix from over-reaching: two LONG-RUNNING services on
    different shas is a real half-finished deploy and must still fire."""
    p = tmp_path / "deploy_log.jsonl"
    p.write_text(_deploy_line("api_server", "facd767") + "\n"
                 + _deploy_line("discord_bot", "c80c10d") + "\n")
    live = ceo_brief.live_version(p)
    assert live["consistent"] is False
    value = ceo_brief._deploy_field(
        {"live": live, "work": {"available": False}, "host": "vm"})["value"]
    assert "half-finished" in value


def test_old_entries_without_kind_fall_back_to_the_name_list(tmp_path):
    """The existing deploy log predates the `kind` field — master_scheduler
    must still be recognised as a job by name."""
    p = tmp_path / "deploy_log.jsonl"
    p.write_text("\n".join([
        json.dumps({"ts": "2026-07-20T09:10:00+05:30",
                    "service": "master_scheduler", "sha": "c80c10d",
                    "subject": "s", "committed": "", "dirty": False,
                    "event": "deploy"}),                     # no "kind"
        _deploy_line("api_server", "facd767"),
    ]) + "\n")
    live = ceo_brief.live_version(p)
    assert set(live["jobs"]) == {"master_scheduler"}
    assert live["consistent"] is True


def test_a_single_service_is_trivially_consistent(tmp_path):
    p = tmp_path / "deploy_log.jsonl"
    p.write_text(_deploy_line("api_server", "facd767") + "\n")
    assert ceo_brief.live_version(p)["consistent"] is True


def test_deploy_log_records_the_kind_field():
    """The writer must emit `kind` so future entries need no name list."""
    import inspect
    from src import deploy_log
    sig = inspect.signature(deploy_log.record_startup)
    assert sig.parameters["kind"].default == "service"
    src = inspect.getsource(deploy_log.record_startup)
    assert '"kind": kind' in src


def test_master_scheduler_declares_itself_a_job():
    """It self-terminates at 15:30 — it must not be logged as a service."""
    src = (ceo_brief.ROOT / "src" / "master_scheduler.py").read_text()
    assert 'record_startup("master_scheduler", kind="job")' in src

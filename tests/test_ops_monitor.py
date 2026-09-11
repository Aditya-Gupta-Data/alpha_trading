"""
Ops monitor tests — fully offline against temp log directories. No
Discord (notify injected), no real logs/ touched.

Run from the project folder:
    python tests/test_ops_monitor.py      (simple, no extra installs)
    python -m pytest tests/               (if you have pytest)
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import ops_monitor as om

MONDAY = datetime(2026, 7, 6, 20, 30)
SUNDAY = datetime(2026, 7, 5, 20, 30)


def make_logs(tmp, files: dict) -> Path:
    logs = Path(tmp) / "logs"
    logs.mkdir()
    for name, text in files.items():
        (logs / name).write_text(text)
    return logs


def test_sweep_finds_problem_lines_and_ignores_clean_ones():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": (
            "[Market Loop] NIFTY 50: no proposal (regime gate).\n"
            "  (margin gate unavailable — failing open: db locked)\n"
            "[Live Bridge] cycle failed (feed down) — loop continues.\n"
            "healthy line about profits\n")})
        problems, state = om.sweep_logs(logs, {})
        assert len(problems) == 2
        assert all(p["log"] == "a.log" for p in problems)
        assert any("unavailable" in p["line"] for p in problems)
        assert any("failed" in p["line"] for p in problems)
        assert state["a.log"] > 0


def test_sweep_is_incremental_and_never_rereports():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "first error line\n"})
        problems, state = om.sweep_logs(logs, {})
        assert len(problems) == 1
        # same state, nothing new appended -> silence
        problems, state = om.sweep_logs(logs, state)
        assert problems == []
        # append one new problem -> exactly that one is reported
        with open(logs / "a.log", "a") as f:
            f.write("clean line\nsecond error line\n")
        problems, state = om.sweep_logs(logs, state)
        assert len(problems) == 1 and "second" in problems[0]["line"]


def test_sweep_recovers_from_a_truncated_log():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "x" * 500 + "\n"})
        _, state = om.sweep_logs(logs, {})
        (logs / "a.log").write_text("fresh error after rotation\n")
        problems, state = om.sweep_logs(logs, state)
        assert len(problems) == 1 and "rotation" in problems[0]["line"]


def test_repeated_identical_lines_collapse_with_a_count():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "same error\n" * 7})
        problems, _ = om.sweep_logs(logs, {})
        assert len(problems) == 1 and problems[0]["count"] == 7


def test_sweep_never_scans_its_own_output_log():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {
            "ops_monitor.log": "• `a.log`: quoted error from last night\n",
            "a.log": "clean\n"})
        problems, _ = om.sweep_logs(logs, {})
        assert problems == []


def test_sweep_never_scans_the_ceo_briefs_output_log():
    """The brief's rendered card (written to ceo_brief.log) quotes problem
    lines. Sweeping it makes each day's card re-flag the previous day's card
    — the 2026-07-20 self-echo where a fixed corporate_events crash kept
    resurfacing 'from ceo_brief.log'. Both reports' logs are excluded."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {
            "ceo_brief.log": "• a script was started with options it does "
                             "not accept (--backfill ...) — that run did "
                             "nothing.\n",
            "a.log": "clean\n"})
        problems, _ = om.sweep_logs(logs, {})
        assert problems == []


def test_heartbeats_flag_silent_jobs_weekday_aware():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"renew_token.log": "ok\n"})
        expected = {"renew_token.log": False, "master_scheduler.log": True}
        # Monday: the missing weekday job is flagged. Freshness is
        # mtime-date == now-date, so pin BOTH to a fixed Monday — with a
        # real datetime.now() this test only passed on weekdays.
        monday = datetime(2026, 7, 6, 20, 30)
        os.utime(logs / "renew_token.log",
                 (monday.timestamp(), monday.timestamp()))
        missing = om.check_heartbeats(logs, now=monday,
                                      expected=expected)
        assert missing == ["master_scheduler.log — did not run today"]
        # weekend: weekday-only jobs are excused
        missing = om.check_heartbeats(logs, now=SUNDAY, expected=expected)
        assert all("master_scheduler" not in m for m in missing)


def test_expected_jobs_env_override(monkeypatch=None):
    import os
    saved = os.environ.get("OPS_EXPECTED_JOBS")
    try:
        os.environ["OPS_EXPECTED_JOBS"] = "only_this.log:0, weekday_job.log:1"
        with tempfile.TemporaryDirectory() as tmp:
            logs = make_logs(tmp, {"only_this.log": "ok\n"})  # fresh today
            # freshness is mtime-vs-today, so the clock must be real "now";
            # use a weekday-only flag that is checked on any weekday run
            now = datetime.now()
            missing = om.check_heartbeats(logs, now=now)
            expected_missing = ([] if now.weekday() >= 5
                                else ["weekday_job.log — did not run today"])
            # default EXPECTED_JOBS is fully replaced by the env list
            assert missing == expected_missing
        os.environ["OPS_EXPECTED_JOBS"] = ""
        assert om._expected_jobs_from_env() is None   # empty -> default
    finally:
        if saved is None:
            os.environ.pop("OPS_EXPECTED_JOBS", None)
        else:
            os.environ["OPS_EXPECTED_JOBS"] = saved


def test_problems_land_in_the_jsonl_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "problems.jsonl"
        om.record_problems(
            [{"log": "a.log", "line": "boom", "count": 2}],
            when="2026-07-06 20:30", problems_path=ledger)
        rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        assert rows == [{"log": "a.log", "line": "boom", "count": 2,
                         "found": "2026-07-06 20:30"}]


def test_card_formats_clean_and_dirty_nights():
    clean = om.build_card([], [], "2026-07-06 20:30")
    assert "✅" in clean and "no problem lines" in clean
    dirty = om.build_card(
        [{"log": "a.log", "line": "boom", "count": 3}],
        ["main.log — did not run today"], "2026-07-06 20:30")
    assert "🩺" in dirty and "x3" in dirty and "did not run" in dirty
    # long problem lists cap for Discord readability
    many = om.build_card(
        [{"log": "a.log", "line": f"e{i}", "count": 1} for i in range(20)],
        [], "2026-07-06 20:30")
    assert "more — see logs/problems.jsonl" in many


def test_a_clean_staleness_scan_leaves_the_card_byte_identical():
    """The guard is additive: stale=None (a clean scan) must produce exactly
    the card that existed before staleness_guard was written."""
    for args in ([], [{"log": "a.log", "line": "boom", "count": 3}]):
        before = om.build_card(args, [], "2026-07-06 20:30")
        assert om.build_card(args, [], "2026-07-06 20:30", stale=None) == before


def test_a_stale_artifact_screams_on_the_health_card():
    stale = {"count": 1, "disabled": 1, "names": ["sector_index_bars"],
             "text": "🚨 **STALE DATA — 1 artifact(s), 1 component(s) "
                     "SELF-DISABLED**\n• 🔴 DISABLED `sector_index_bars` — 20 days"}
    clean_night = om.build_card([], [], "2026-08-05 20:30", stale=stale)
    assert "✅" in clean_night           # jobs still ran — a different disease
    assert "STALE DATA" in clean_night
    assert "SELF-DISABLED" in clean_night

    bad_night = om.build_card([{"log": "a.log", "line": "boom", "count": 1}],
                              [], "2026-08-05 20:30", stale=stale)
    assert "STALE DATA" in bad_night
    # a stale FILE is never folded into the problem-LINE count
    assert "1 problem line(s)" in bad_night


def test_a_broken_staleness_guard_costs_its_section_not_the_sweep(monkeypatch):
    """Fail-open on the REPORT: the ops sweep must survive a guard that
    explodes, because the sweep is how we learn anything at all."""
    from src import staleness_guard
    monkeypatch.setattr(staleness_guard, "scan",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "an error appeared\n"})
        out = om.run_sweep(logs_dir=logs,
                           state_path=Path(tmp) / "state.json",
                           problems_path=Path(tmp) / "problems.jsonl",
                           now=datetime(2026, 8, 5, 20, 30),
                           notify_fn=lambda text: True,
                           staleness_root=tmp)
    assert out["problem_lines"] >= 1
    assert out["stale_artifacts"] == 0


def test_run_sweep_end_to_end_persists_state_and_notifies():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "an error appeared\n"})
        state_path = logs / ".state.json"
        ledger = logs / "problems.jsonl"
        cards = []
        summary = om.run_sweep(logs_dir=logs, state_path=state_path,
                               problems_path=ledger, now=MONDAY,
                               notify_fn=cards.append, staleness_root=tmp)
        assert summary["distinct_problems"] == 1
        assert len(cards) == 1 and "Ops sweep" in cards[0]
        assert ledger.exists() and state_path.exists()
        # second run same night: nothing new, still a (clean-ish) card
        summary = om.run_sweep(logs_dir=logs, state_path=state_path,
                               problems_path=ledger, now=MONDAY,
                               notify_fn=cards.append, staleness_root=tmp)
        assert summary["distinct_problems"] == 0
        assert len(cards) == 2


def test_a_broken_notifier_never_breaks_the_sweep():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "error\n"})

        def boom(text):
            raise RuntimeError("discord down")

        summary = om.run_sweep(logs_dir=logs, state_path=logs / ".s.json",
                               problems_path=logs / "p.jsonl", now=MONDAY,
                               notify_fn=boom, staleness_root=tmp)
        assert summary["distinct_problems"] == 1  # sweep completed anyway


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")


def test_zero_valued_failure_stats_are_not_problems():
    """2026-07-14 false alarm: a HEALTHY sleep-phase stats dict tripped
    the card via the literal word 'failed' at count zero. Zero-valued
    failure counters are scrubbed; any nonzero count still fires."""
    from src.ops_monitor import is_problem_line
    healthy = ("A. ingestion:     {'ingested': 2, 'skipped_duplicate': 5, "
               "'skipped_empty': 0, 'failed': 0, 'skipped_no_llm': 18}")
    assert not is_problem_line(healthy)
    assert is_problem_line(healthy.replace("'failed': 0", "'failed': 3"))
    assert is_problem_line("NSE fetch failed [HTTP Error 403: Forbidden]")


def test_count_first_zero_failures_are_not_problems():
    """2026-07-20 false alarm: the CEO brief reported '63 problem line(s)'
    when 40 of them were SUCCESSFUL backfills announcing '0 failed'. The
    zero-stat scrubber only knew the "'failed': 0" word order, not the
    "0 window(s) failed" one our completion summaries actually use."""
    from src.ops_monitor import is_problem_line

    # Verbatim from the 2026-07-20 logs — both are successes.
    assert not is_problem_line(
        "(corporate_events backfill DONE: 62725 flagged events across "
        "92 windows, 0 failed; flags {'CATALYST': 8399})")
    assert not is_problem_line(
        "(backfill: 84919 deal(s) across 2351 day(s) appended; "
        "0 window(s) failed)")
    assert not is_problem_line("run complete: 0 errors")

    # The scrubber must not swallow a REAL failure standing next to a zero.
    assert is_problem_line("backfill: 12 window(s) failed")
    assert is_problem_line("0 rows written, upload failed")
    assert is_problem_line("0 errors ingesting, but the NSE fetch timed out")


def test_empty_list_failure_stats_are_not_problems():
    """2026-07-27 false alarm: macro_nightly writes clean runs as
    '"failed": []' — an empty LIST, not a zero count — and two of them
    reached the CEO brief as problem lines. Empty brackets are scrubbed;
    a populated list still fires."""
    from src.ops_monitor import is_problem_line

    # Verbatim shape from macro_nightly.log — a clean run.
    assert not is_problem_line('"failed": []')
    assert not is_problem_line(
        '{"ts": "2026-07-25T01:48:04", "as_of": "2026-07-25", "stages": '
        '{"fred": {"ok": ["BRENT", "DXY"], "failed": []}}}')
    assert not is_problem_line("'errors': [ ]")   # spaced brackets too

    # A NON-empty failure list is a genuine problem and must still fire.
    assert is_problem_line('"failed": ["TCS.NS", "INFY.NS"]')
    assert is_problem_line('{"stages": {"fred": {"failed": ["DXY"]}}}')


# --------------------------------------------------------------------------
# RED ALARMS (2026-09-11, ledger Issues 26 + 27): auth/data-access refusals,
# zero-capture blindness, low memory. Each was invisible to the sweep before.
# --------------------------------------------------------------------------

DH902_LINE = ("  Dhan historical returned for id=13 IDX_I/INDEX 2024-06-22->2026-09-10: "
              "{'status': 'failure', 'remarks': {'error_code': 'DH-902', "
              "'error_type': 'Invalid_Access', 'error_message': 'HTTP Status 451. "
              "User has not subscribed to Data APIs'}}")


def _capture_line(day: str, slot: str, captured: int, skipped: bool = False) -> str:
    rec = {"ts": f"{day}T{slot}:01.000000+05:30", "captured": captured,
           "failed": 0 if captured else 84, "tickers": 84}
    if skipped:
        rec = {"skipped": "market_closed", "ts": rec["ts"], "captured": 0, "failed": 0}
    return json.dumps(rec)


def _capture_log(days: dict) -> str:
    """{date: [captured per slot]} -> the tracker's JSON-lines log, with the
    market-closed slots that every real day also carries."""
    out = []
    for day, slots in days.items():
        out.append(_capture_line(day, "09:00", 0, skipped=True))
        for i, n in enumerate(slots):
            out.append(_capture_line(day, f"{9 + i // 4:02d}:{15 * (i % 4):02d}", n))
        out.append(_capture_line(day, "15:45", 0, skipped=True))
    return "\n".join(out) + "\n"


def test_a_dhan_code_is_a_problem_line_even_without_problem_words():
    assert om.is_problem_line("Dhan historical returned {'error_code': 'DH-906'}")
    assert om.is_problem_line("id=13 -> DH-902 Invalid_Access")
    assert not om.is_problem_line("captured 88 of 88, 0 failed")


def test_auth_codes_raise_a_red_alarm_above_the_problem_cap():
    with tempfile.TemporaryDirectory() as tmp:
        noise = "\n".join(f"worker w{i} error: boom" for i in range(20))
        logs = make_logs(tmp, {"master_scheduler.log": noise + "\n" + DH902_LINE + "\n"
                                                        + DH902_LINE + "\n"})
        problems, _ = om.sweep_logs(logs, {})
        alarms = om.auth_alarms(problems)
        assert len(alarms) == 1
        assert alarms[0]["code"] == "DH-902" and alarms[0]["count"] == 2
        assert "subscription" in alarms[0]["text"] and "token will NOT fix" in alarms[0]["text"]
        card = om.build_card(problems, [], "2026-09-08 20:30", alarms=alarms)
        assert card.startswith("🚨")
        assert "1 RED alarm(s)" in card
        # the alarm sits ABOVE the twelve capped problem lines
        assert card.index("AUTH/DATA ACCESS") < card.index("worker w0 error")
        assert "…and" in card                        # cap still applies to the rest


def test_capture_blindness_counts_consecutive_all_zero_sessions_only():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {om.CAPTURE_LOG: _capture_log({
            "2026-09-03": [88] * 5 + [0] * 3,      # partial day: NOT blind
            "2026-09-04": [88] * 8,
            "2026-09-07": [0] * 8,                  # blind
            "2026-09-08": [0] * 8,                  # blind
            "2026-09-09": [0] * 8,                  # blind
        })})
        blind = om.capture_blindness(logs, now=datetime(2026, 9, 9, 20, 30))
        assert blind["blind_streak"] == 3
        assert blind["blind_dates"] == ["2026-09-07", "2026-09-08", "2026-09-09"]
        assert blind["sessions"]["2026-09-03"] == {"slots": 8, "zero": 3}
        alarm = om.capture_alarm(blind)
        assert alarm["red"] is True and "3 consecutive" in alarm["text"]


def test_one_blind_session_is_amber_two_are_red():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {om.CAPTURE_LOG: _capture_log({
            "2026-09-04": [88] * 8, "2026-09-07": [0] * 8})})
        one = om.capture_alarm(om.capture_blindness(logs, now=datetime(2026, 9, 7, 20, 30)))
        assert one["red"] is False and "one more" in one["text"]
        (Path(logs) / om.CAPTURE_LOG).write_text(_capture_log({
            "2026-09-04": [88] * 8, "2026-09-07": [0] * 8, "2026-09-08": [0] * 8}))
        two = om.capture_alarm(om.capture_blindness(logs, now=datetime(2026, 9, 8, 20, 30)))
        assert two["red"] is True and two["streak"] == 2


def test_a_recovered_session_ends_the_blind_streak():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {om.CAPTURE_LOG: _capture_log({
            "2026-09-07": [0] * 8, "2026-09-08": [0] * 8, "2026-09-10": [0, 0, 88, 88]})})
        blind = om.capture_blindness(logs, now=datetime(2026, 9, 10, 20, 30))
        assert blind["blind_streak"] == 0 and om.capture_alarm(blind) is None


def test_blindness_reads_nothing_when_the_capture_log_is_absent():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "fine\n"})
        blind = om.capture_blindness(logs, now=MONDAY)
        assert blind == {"sessions": {}, "blind_streak": 0, "blind_dates": [],
                         "latest_session": None}


def test_low_memory_is_a_red_alarm_and_a_healthy_box_is_not():
    with tempfile.TemporaryDirectory() as tmp:
        meminfo = Path(tmp) / "meminfo"
        meminfo.write_text("MemTotal:  987712 kB\nMemFree: 40000 kB\n"
                           "MemAvailable: 61440 kB\nSwapTotal: 1048572 kB\nSwapFree: 900000 kB\n")
        t = om.system_telemetry(meminfo_path=str(meminfo), loadavg_path=str(Path(tmp) / "nope"))
        assert t["mem_available_mb"] == 60
        alarm = om.memory_alarm(t)
        assert alarm["red"] is True and "60 MB" in alarm["text"]
        meminfo.write_text("MemTotal:  987712 kB\nMemAvailable: 492316 kB\n")
        assert om.memory_alarm(om.system_telemetry(meminfo_path=str(meminfo))) is None
        assert om.memory_alarm({"mem_available_mb": None}) is None   # unknown != low


def test_alarms_forbid_the_all_ok_card():
    clean = om.build_card([], [], "2026-09-08 20:30")
    assert clean.startswith("✅")
    alarmed = om.build_card([], [], "2026-09-08 20:30",
                            alarms=[om.memory_alarm({"mem_available_mb": 42})])
    assert alarmed.startswith("🚨") and "LOW MEMORY" in alarmed and "✅" not in alarmed


def test_run_sweep_carries_alarms_in_the_summary_and_card(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {
            "master_scheduler.log": DH902_LINE + "\n",
            om.CAPTURE_LOG: _capture_log({"2026-09-07": [0] * 8, "2026-09-08": [0] * 8}),
        })
        monkeypatch.setattr(om, "system_telemetry",
                            lambda *a, **k: {"mem_available_mb": 50, "mem_used_pct": 95})
        cards = []
        summary = om.run_sweep(logs_dir=logs, state_path=logs / ".s.json",
                               problems_path=logs / "p.jsonl",
                               now=datetime(2026, 9, 8, 20, 30),
                               notify_fn=cards.append, staleness_root=tmp)
        assert summary["alarms"] == ["auth", "zero_capture", "low_memory"]
        assert summary["red_alarms"] == 3
        assert summary["auth_failures"] == 1 and summary["blind_sessions"] == 2
        assert summary["low_memory"] is True
        assert cards[0].startswith("🚨") and "DATA BLIND" in cards[0]


def test_health_verdict_is_stateless_and_names_the_red_lines():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {
            "intraday_15m.log": _capture_log({"2026-09-07": [0] * 8, "2026-09-08": [0] * 8}),
            "master_scheduler.log": DH902_LINE + "\n",
            "ops_monitor.log": DH902_LINE + "\n",          # a report log: never read
        })
        lines = om.health_verdict(logs, now=datetime(2026, 9, 8, 20, 30),
                                  telemetry={"mem_available_mb": 480})
        text = "\n".join(lines)
        assert "AUTH/DATA ACCESS: DH-902 x1" in text
        assert "DATA BLIND" in text
        assert "LOW MEMORY" not in text
        assert "last capture session 2026-09-08: 0/8 slots captured" in text
        assert not (Path(logs) / ".ops_monitor_state.json").exists()
        sub = Path(tmp) / "b"
        sub.mkdir()
        clean = om.health_verdict(make_logs(sub, {"a.log": "fine\n"}),
                                  now=MONDAY, telemetry={"mem_available_mb": 480})
        assert clean[0].startswith("  ✅")

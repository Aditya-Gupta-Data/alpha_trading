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


def test_run_sweep_end_to_end_persists_state_and_notifies():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "an error appeared\n"})
        state_path = logs / ".state.json"
        ledger = logs / "problems.jsonl"
        cards = []
        summary = om.run_sweep(logs_dir=logs, state_path=state_path,
                               problems_path=ledger, now=MONDAY,
                               notify_fn=cards.append)
        assert summary["distinct_problems"] == 1
        assert len(cards) == 1 and "Ops sweep" in cards[0]
        assert ledger.exists() and state_path.exists()
        # second run same night: nothing new, still a (clean-ish) card
        summary = om.run_sweep(logs_dir=logs, state_path=state_path,
                               problems_path=ledger, now=MONDAY,
                               notify_fn=cards.append)
        assert summary["distinct_problems"] == 0
        assert len(cards) == 2


def test_a_broken_notifier_never_breaks_the_sweep():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_logs(tmp, {"a.log": "error\n"})

        def boom(text):
            raise RuntimeError("discord down")

        summary = om.run_sweep(logs_dir=logs, state_path=logs / ".s.json",
                               problems_path=logs / "p.jsonl", now=MONDAY,
                               notify_fn=boom)
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

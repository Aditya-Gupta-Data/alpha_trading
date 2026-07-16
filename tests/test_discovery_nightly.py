"""
Tests for the gated nightly discovery pass (src/discovery/nightly.py,
decision #76): the health gate (silent jobs / today's ingestion problem
lines), the depth gate (daily_context floor), the anti-silent-death skip
ledger (Discord note every 7th consecutive skip), and the green path
(run_miners actually invoked, skip counter reset). Offline — temp dirs,
in-memory DBs, injected clock/notifier/runner.

Run:
    python tests/test_discovery_nightly.py
    pytest tests/test_discovery_nightly.py -v
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import daily_context as dc
from src.discovery import nightly

# The REAL clock: check_heartbeats compares file mtimes (which are truly
# "now") against now.date(), so a fixed past date would flag every freshly
# written temp log as silent. Weekday variability doesn't matter here —
# every expected-job flag in these tests is False (daily).
NOW = datetime.now()
TODAY = NOW.strftime("%Y-%m-%d")


def _env(tmp, *, touched=("deals_tracker.log",), problems=None, frames=80):
    """One disposable nightly environment: a logs dir with the expected
    job logs `touched` today, an optional problems.jsonl, and an
    in-memory brain with `frames` daily_context rows. Returns
    (logs_dir, conn, state_path, expected)."""
    logs = Path(tmp) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    expected = {"deals_tracker.log": False}          # one daily job to watch
    for name in touched:
        (logs / name).write_text("ran\n")            # mtime = today
    if problems:
        with open(logs / "problems.jsonl", "w") as f:
            for log, count in problems:
                f.write(json.dumps({"log": log, "line": "boom",
                                    "count": count,
                                    "found": f"{TODAY} 20:30"}) + "\n")
    conn = brain_map.connect(":memory:")
    dc.ensure_schema(conn)
    for i in range(frames):
        conn.execute("INSERT OR REPLACE INTO daily_context (date, payload) "
                     "VALUES (?, '{}')", (f"2026-{4 + i // 28:02d}-{i % 28 + 1:02d}",))
    conn.commit()
    return logs, conn, Path(tmp) / "state.json", expected


def _run(tmp, **kw):
    logs, conn, state, expected = _env(tmp, **{k: kw.pop(k) for k in
                                               ("touched", "problems", "frames")
                                               if k in kw})
    calls = {"notify": [], "mined": 0}
    def fake_run_all(conn=None, today=None):
        calls["mined"] += 1
        return {"summary": "ok", "totals": {}}
    from unittest import mock
    with mock.patch("src.discovery.run_miners.run_all",
                    side_effect=fake_run_all):
        result = nightly.run_nightly(
            conn=conn, logs_dir=logs, now=NOW, state_path=state,
            notify_fn=lambda text: calls["notify"].append(text),
            expected=expected, **kw)
    conn.close()
    return result, calls, state


# ------------------------------------------------------------- the gates

def test_green_path_runs_the_miners_and_resets_the_skip_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        # Pre-existing skips must reset on a successful run.
        state = Path(tmp) / "state.json"
        state.write_text(json.dumps({"consecutive_skips": 5}))
        result, calls, state = _run(tmp)
        assert result["ran"] is True
        assert calls["mined"] == 1
        assert result["consecutive_skips"] == 0
        assert json.loads(state.read_text())["consecutive_skips"] == 0


def test_silent_job_blocks_mining():
    with tempfile.TemporaryDirectory() as tmp:
        result, calls, _ = _run(tmp, touched=())    # watched log never ran
    assert result["ran"] is False
    assert calls["mined"] == 0
    assert result["gates"]["health"]["silent_jobs"]


def test_todays_ingestion_problem_lines_block_mining():
    with tempfile.TemporaryDirectory() as tmp:
        result, calls, _ = _run(
            tmp, problems=[("deals_tracker.log", 3)])
    assert result["ran"] is False
    assert calls["mined"] == 0
    assert result["gates"]["health"]["ingestion_problems"] == 3


def test_ledger_gate_reads_the_latest_sweep_slice():
    """The pass runs pre-sweep (20:20), so the freshest ledger slice is
    the LAST sweep's. Older sweeps' problems must not gate forever: a
    ledger whose latest slice is clean runs, even with ingestion problems
    on earlier dates."""
    with tempfile.TemporaryDirectory() as tmp:
        logs, conn, state, expected = _env(tmp)
        with open(logs / "problems.jsonl", "w") as f:
            f.write(json.dumps({"log": "deals_tracker.log", "line": "old",
                                "count": 5, "found": "2026-01-02 20:30"}) + "\n")
            f.write(json.dumps({"log": "master_scheduler.log", "line": "x",
                                "count": 1, "found": f"{TODAY} 20:30"}) + "\n")
        health = nightly.health_gate(logs, now=NOW, expected=expected)
        conn.close()
    # Latest slice (today) holds only a NON-ingestion line -> clean gate;
    # the January ingestion problems are history, not tonight's verdict.
    assert health["ingestion_problems"] == 0
    assert health["ok"] is True


def test_non_ingestion_problem_lines_do_not_block():
    """Scope: a fail-open note in the scheduler is real but not
    discovery's business — only the miners' UPSTREAM gates mining."""
    with tempfile.TemporaryDirectory() as tmp:
        result, calls, _ = _run(
            tmp, problems=[("master_scheduler.log", 9)])
    assert result["ran"] is True
    assert calls["mined"] == 1


def test_depth_gate_blocks_below_the_frame_floor():
    with tempfile.TemporaryDirectory() as tmp:
        result, calls, _ = _run(tmp, frames=12)
    assert result["ran"] is False
    assert calls["mined"] == 0
    assert result["gates"]["depth"] == {"ok": False, "frames": 12,
                                        "min_frames": nightly.MIN_CONTEXT_FRAMES}


# ------------------------------------------------- anti-silent-death

def test_every_seventh_consecutive_skip_notifies_once():
    with tempfile.TemporaryDirectory() as tmp:
        notified = []
        for i in range(15):
            logs, conn, state, expected = _env(Path(tmp) / "e", touched=())
            result = nightly.run_nightly(
                conn=conn, logs_dir=logs, now=NOW,
                state_path=Path(tmp) / "state.json",
                notify_fn=lambda t: notified.append(t), expected=expected)
            conn.close()
            assert result["ran"] is False
        # Notes fired on skips 7 and 14 — and only then.
        assert len(notified) == 2
        assert "7 nights" in notified[0] and "14 nights" in notified[1]


def test_skip_counts_persist_across_invocations():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        for expected_count in (1, 2, 3):
            logs, conn, _, expected = _env(Path(tmp) / "e", touched=())
            result = nightly.run_nightly(
                conn=conn, logs_dir=logs, now=NOW, state_path=state_path,
                notify_fn=lambda t: None, expected=expected)
            conn.close()
            assert result["consecutive_skips"] == expected_count


# ------------------------------------------------------------- honesty

def test_never_raises_even_when_everything_is_missing():
    """Fail-open contract: empty logs dir, no ledger, no state file, no
    daily_context rows — a quiet skip, never an exception."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = brain_map.connect(":memory:")
        result = nightly.run_nightly(
            conn=conn, logs_dir=Path(tmp) / "nonexistent", now=NOW,
            state_path=Path(tmp) / "state.json", notify_fn=lambda t: None,
            expected={"deals_tracker.log": False})
        conn.close()
    assert result["ran"] is False


def test_own_heartbeat_is_excluded_from_the_gate():
    """discovery_nightly.log is heartbeat-monitored by ops_monitor, but
    the gate must not self-flag before tonight's run has happened."""
    with tempfile.TemporaryDirectory() as tmp:
        logs, conn, state, _ = _env(tmp)
        # Hand the gate the REAL expected map shape including our own log.
        expected = {"deals_tracker.log": False, "discovery_nightly.log": False}
        health = nightly.health_gate(logs, now=NOW, expected=expected)
        conn.close()
    assert health["ok"] is True          # our own absent log did not flag


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{passed}/{len(tests)} tests passed.")

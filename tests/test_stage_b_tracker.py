"""scripts/stage_b_tracker.py — the Stage-B clock and its pace arithmetic.

These numbers are load-bearing: the 2026-07-30 owner ruling (DECISIONS #86,
target moved Oct 1 -> ~Oct 13) was made ON this tool's output, so a silent
miscount here would misdate a governance decision. The session-counting
rule in particular is the one that mattered — raw rows over-count.

Hermetic: tmp_path fixtures only, `--today` pinned so the calendar cannot
drift the assertions (the 07-22 journal-drift lesson).
"""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "stage_b_tracker",
    Path(__file__).resolve().parent.parent / "scripts" / "stage_b_tracker.py")
tracker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tracker)

TODAY = date(2026, 7, 30)


def _repo(tmp_path, decls=(), scores=(), board=None, runs=()):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "logs" / "macro_regime_declarations.jsonl").write_text(
        "".join(json.dumps(d) + "\n" for d in decls))
    if scores:
        (tmp_path / "logs" / "macro_strategy_scores.jsonl").write_text(
            "".join(json.dumps(s) + "\n" for s in scores))
    if board is not None:
        (tmp_path / "data" / "strategy_scoreboard.json").write_text(
            json.dumps(board))
    if runs:
        (tmp_path / "logs" / "macro_nightly.log").write_text(
            "".join(json.dumps(r) + "\n" for r in runs))
    return tmp_path


def _decl(session, horizons=("shock", "slow_burn")):
    return {"as_of_session": session, "run_at": f"{session}T19:50:00",
            "declared": True,
            "horizons": {h: {"declared": True, "phase": "P1"} for h in horizons}}


# ------------------------------------------------ the counting rule

def test_sessions_count_DISTINCT_days_not_raw_rows():
    """THE BUG THIS TOOL CAUGHT. The 07-22..24 build era wrote 2-4 rows per
    session during manual verification runs; counting rows said 12 when the
    true clock was 7. A governance date was set on this number."""
    tmp = Path(pytest.importorskip("tempfile").mkdtemp())
    decls = [_decl("2026-07-22"), _decl("2026-07-22"),          # dupes
             _decl("2026-07-23"), _decl("2026-07-23"), _decl("2026-07-23"),
             _decl("2026-07-24")]
    s = tracker.collect(_repo(tmp, decls=decls), TODAY)
    assert s["sessions_on_ledger"] == 3          # not 6
    assert s["declared_calls"] == 12             # calls counted separately


def test_only_declared_horizons_count_as_calls(tmp_path):
    d = _decl("2026-07-30")
    d["horizons"]["slow_burn"]["declared"] = False      # abstained
    s = tracker.collect(_repo(tmp_path, decls=[d]), TODAY)
    assert s["declared_calls"] == 1
    assert s["sessions_on_ledger"] == 1


def test_pending_is_declared_minus_graded(tmp_path):
    s = tracker.collect(
        _repo(tmp_path, decls=[_decl("2026-07-29"), _decl("2026-07-30")],
              scores=[{"ref": "a"}]), TODAY)
    assert s["declared_calls"] == 4
    assert s["graded_calls"] == 1
    assert s["pending_calls"] == 3


def test_missing_scores_file_is_zero_graded_not_a_crash(tmp_path):
    """A missing macro_strategy_scores.jsonl is CORRECT until the first
    window matures — it must never read as a fault."""
    s = tracker.collect(_repo(tmp_path, decls=[_decl("2026-07-30")]), TODAY)
    assert s["graded_calls"] == 0
    assert "zero graded is CORRECT" in tracker.render(s)


# ------------------------------------------------ the uptime canary

def test_stale_cron_is_flagged(tmp_path):
    s = tracker.collect(_repo(tmp_path, decls=[_decl("2026-07-20")]), TODAY)
    assert s["weekdays_since_last_session"] > tracker.STALE_AFTER_WEEKDAYS
    assert "STALE" in tracker.render(s)


def test_fresh_cron_is_not_flagged(tmp_path):
    s = tracker.collect(_repo(tmp_path, decls=[_decl("2026-07-30")]), TODAY)
    assert s["weekdays_since_last_session"] == 0
    assert "STALE" not in tracker.render(s)


def test_failed_stages_surface(tmp_path):
    runs = [{"run_at": "2026-07-30T19:50:00", "all_ok": False,
             "stages": {"fred": {"ok": []},
                        "declare": {"error": "CacheMiss: boom"}}}]
    s = tracker.collect(_repo(tmp_path, decls=[_decl("2026-07-30")],
                              runs=runs), TODAY)
    assert s["failed_stages"] == ["declare"]
    assert "FAILED STAGES" in tracker.render(s)


# ------------------------------------------------ the ruling's arithmetic

def test_weekday_math_skips_weekends():
    # Thu 2026-07-30 -> Mon 2026-08-03 is 2 weekdays (Fri, Mon).
    assert tracker._weekdays_between(date(2026, 7, 30), date(2026, 8, 3)) == 2
    assert tracker._weekdays_between(date(2026, 7, 30), date(2026, 7, 30)) == 0
    assert tracker._weekdays_between(date(2026, 8, 3), date(2026, 7, 30)) == 0


def test_the_ruling_dates_are_what_decision_86_says():
    """Guards against a silent edit re-slipping the standard."""
    assert tracker.TARGET_SESSIONS == 60                  # bar NEVER moves
    assert tracker.TARGET_DATE == date(2026, 10, 13)
    assert tracker.PRELIM_READ_DATE == date(2026, 10, 1)


def test_behind_and_on_track_both_render(tmp_path):
    behind = tracker.collect(_repo(tmp_path, decls=[_decl("2026-07-30")]),
                             TODAY)
    assert "BEHIND" in tracker.render(behind)

    many = [_decl(d.isoformat()) for d in
            (date(2026, 7, 30) - __import__("datetime").timedelta(days=i)
             for i in range(70)) if d.weekday() < 5]
    ok = tracker.collect(_repo(tmp_path, decls=many), TODAY)
    assert ok["sessions_on_ledger"] >= 50
    out = tracker.render(ok)
    assert "BEHIND" not in out or ok["sessions_remaining"] == 0


def test_preliminary_read_is_labelled_non_binding(tmp_path):
    s = tracker.collect(_repo(tmp_path, decls=[_decl("2026-07-30")]), TODAY)
    out = tracker.render(s)
    assert "2026-10-01" in out and "NON-BINDING" in out
    assert "does not slip" in out

"""
Internal bug ledger (#84 Directive 5) — hermetic tests. Run:
    python -m pytest tests/test_bug_ledger.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.bug_ledger as bl
from src import portfolio_manager as pm


def _seed(tmp):
    logs = Path(tmp)
    (logs / "problems.jsonl").write_text(json.dumps(
        {"log": "x.log", "line": "Dhan quote error", "count": 2,
         "found": "2026-07-21 20:30"}) + "\n")
    (logs / "treasury_ledger.jsonl").write_text(
        json.dumps({"ts": "T1", "action": "hold"}) + "\n"
        + json.dumps({"ts": "T2", "action": "vm_unreachable",
                      "detail": "no report"}) + "\n")
    (logs / "sizing_adjustments.jsonl").write_text(
        json.dumps({"ts": "T3", "key": "darling_buy/weak_buy",
                    "action": "penalty", "detail": "d"}) + "\n"
        + json.dumps({"ts": "T4", "key": "option/iron_condor",
                      "action": "veto", "detail": "burned"}) + "\n")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pm.get_account(conn)
    pm.log_event(conn, "margin_exhaustion", "entry X rejected")
    pm.log_event(conn, "treasury_rotation", "normal op — not collected")
    return logs, conn


def test_collect_takes_bugs_skips_normal_ops_and_dedups():
    with tempfile.TemporaryDirectory() as tmp:
        logs, conn = _seed(tmp)
        report = logs / "report.jsonl"
        res = bl.collect(logs_dir=logs, conn=conn, report_path=report)
        assert res["added"] == 4
        assert res["by_source"] == {"ops_problems": 1, "account_events": 1,
                                    "treasury": 1, "sizing_veto": 1}
        rows = [json.loads(l) for l in report.read_text().splitlines()]
        assert all(r.get("reported") for r in rows)
        assert not any("hold" == r.get("action") for r in rows)
        assert not any("penalty" in str(r.get("detail")) for r in rows)
        # Idempotent: the second sweep adds nothing.
        res = bl.collect(logs_dir=logs, conn=conn, report_path=report)
        assert res["added"] == 0
        conn.close()


def test_collect_fails_open_on_missing_everything():
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(":memory:")   # no account tables at all
        res = bl.collect(logs_dir=Path(tmp) / "nope", conn=conn,
                         report_path=Path(tmp) / "r.jsonl")
        assert res["added"] == 0
        conn.close()


def test_report_renders_grouped_or_all_clear():
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "r.jsonl"
        assert "empty" in bl.render_report(report)
        logs, conn = _seed(tmp)
        bl.collect(logs_dir=logs, conn=conn, report_path=report)
        out = bl.render_report(report)
        assert "4 item(s)" in out and "== sizing_veto" in out
        assert "burned" in out and "Dhan quote error" in out
        conn.close()


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_bug_ledger.py")


# ------------------------------------------- aging / prune (2026-08-05)
#
# The first Thursday-Protocol triage found 74 rows and every one traced to a
# root cause already fixed days earlier (DH-905 host throttle #85, the
# Secret-Manager token flow, the `"failed": []` scrubber, the ceo_brief
# self-echo exclusion, the Issue-4 Ollama wording). The ledger could not say
# so: append-only, dedup-forever, no last-seen. A fixed bug looked exactly
# like a live one, so the file grew until nobody opened it.

from datetime import date as _date


def _row(log, day, line="boom"):
    return {"source": "ops_problems", "fingerprint": f"{log}|{line}",
            "log": log, "line": line, "found": f"{day} 20:30"}


def test_rows_are_aged_PER_ROW_not_per_family():
    """The distinction that matters: the intraday family was still live (a
    failure on 08-03) but 43 of its 45 rows were occurrences the #85 fix
    superseded. Family-level aging would keep all 45 and bury the 2 that
    still mean something."""
    rows = ([_row("intraday_15m.log", "2026-07-20")] * 3
            + [_row("intraday_15m.log", "2026-08-03")])
    active, quiet = bl.partition_by_age(rows, today=_date(2026, 8, 5))
    assert len(active) == 1 and len(quiet) == 3
    assert bl._row_day(active[0]) == "2026-08-03"


def test_the_quiet_boundary_is_the_declared_window():
    rows = [_row("a.log", "2026-07-29"), _row("a.log", "2026-07-28")]
    active, quiet = bl.partition_by_age(rows, today=_date(2026, 8, 5))
    assert bl.QUIET_AFTER_DAYS == 7
    assert [bl._row_day(r) for r in active] == ["2026-07-29"]   # exactly 7d
    assert [bl._row_day(r) for r in quiet] == ["2026-07-28"]    # 8d


def test_an_undated_row_stays_ACTIVE_because_unknown_age_is_not_evidence():
    """The fail-safe direction, same as the staleness guard's."""
    rows = [{"source": "account_events", "fingerprint": "x", "event": "halt"}]
    active, quiet = bl.partition_by_age(rows, today=_date(2026, 8, 5))
    assert len(active) == 1 and quiet == []


def test_a_malformed_date_stays_ACTIVE():
    rows = [_row("a.log", "not-a-date")]
    active, _ = bl.partition_by_age(rows, today=_date(2026, 8, 5))
    assert len(active) == 1


def test_family_last_seen_tells_a_live_family_from_a_dead_one():
    rows = [_row("intraday_15m.log", "2026-08-03"),
            _row("renew_token.log", "2026-07-10")]
    seen = bl.family_last_seen(rows)
    assert seen == {"intraday_15m.log": "2026-08-03",
                    "renew_token.log": "2026-07-10"}


def test_prune_MOVES_rows_it_never_deletes_them(tmp_path):
    active_p = tmp_path / "bugs.jsonl"
    resolved_p = tmp_path / "bugs.resolved.jsonl"
    rows = [_row("a.log", "2026-07-01"), _row("b.log", "2026-08-04", "live")]
    active_p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    res = bl.prune(report_path=active_p, resolved_path=resolved_p,
                   today=_date(2026, 8, 5))
    assert res["retired"] == 1 and res["active"] == 1 and res["written"]

    left = [json.loads(x) for x in active_p.read_text().splitlines()]
    gone = [json.loads(x) for x in resolved_p.read_text().splitlines()]
    assert [r["log"] for r in left] == ["b.log"]
    assert [r["log"] for r in gone] == ["a.log"]
    assert gone[0]["retired_at"]              # the evidence is stamped


def test_prune_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "bugs.jsonl"
    p.write_text(json.dumps(_row("a.log", "2026-07-01")) + "\n")
    before = p.read_text()
    res = bl.prune(report_path=p, resolved_path=tmp_path / "r.jsonl",
                   today=_date(2026, 8, 5), dry_run=True)
    assert res["retired"] == 1 and res["written"] is False
    assert p.read_text() == before
    assert not (tmp_path / "r.jsonl").exists()


def test_a_retired_family_that_COMES_BACK_is_collected_afresh(tmp_path):
    """Pruning must not become permanent amnesia: dedup memory lives in the
    ACTIVE file, so a recurrence after a prune is a new row."""
    p = tmp_path / "bugs.jsonl"
    logs = tmp_path / "logs"
    logs.mkdir()
    p.write_text(json.dumps(_row("a.log", "2026-07-01")) + "\n")
    bl.prune(report_path=p, resolved_path=tmp_path / "r.jsonl",
             today=_date(2026, 8, 5))
    assert p.read_text().strip() == ""

    (logs / "problems.jsonl").write_text(json.dumps(
        {"log": "a.log", "line": "boom", "count": 1,
         "found": "2026-08-05 20:30"}) + "\n")
    res = bl.collect(logs_dir=logs, conn=sqlite3.connect(":memory:"),
                     report_path=p)
    assert res["added"] == 1


def test_prune_on_an_all_quiet_ledger_empties_it_safely(tmp_path):
    p = tmp_path / "bugs.jsonl"
    p.write_text("\n".join(json.dumps(_row("a.log", "2026-07-01"))
                           for _ in range(5)) + "\n")
    res = bl.prune(report_path=p, resolved_path=tmp_path / "r.jsonl",
                   today=_date(2026, 8, 5))
    assert res["retired"] == 5 and res["active"] == 0
    assert p.read_text() == ""


def test_the_report_leads_with_the_active_quiet_split(tmp_path):
    p = tmp_path / "bugs.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        _row("old.log", "2026-07-01"), _row("new.log", "2026-08-04")]) + "\n")
    text = bl.render_report(report_path=p, today=_date(2026, 8, 5))
    assert "ACTIVE 1" in text and "QUIET 1" in text
    assert "family last seen" in text
    assert "new.log 2026-08-04" in text


def test_an_empty_ledger_still_reads_as_the_honest_all_clear(tmp_path):
    p = tmp_path / "bugs.jsonl"
    p.write_text("")
    assert "empty" in bl.render_report(report_path=p)

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

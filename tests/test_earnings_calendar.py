"""
Tests for the earnings calendar (Phase 1). Fully offline.

Run either of these from the project folder:
    python tests/test_earnings_calendar.py
    python -m pytest tests/test_earnings_calendar.py
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import lake
from src.ingestion import earnings_calendar as ec


_ROWS = [
    {"symbol": "TCS", "purpose": "Financial Results", "date": "14-Jul-2026"},
    {"symbol": "TCS", "purpose": "Financial Results", "date": "20-Oct-2026"},
    {"symbol": "INFY", "purpose": "Board Meeting - Quarterly Results",
     "date": "23-Jul-2026"},
    {"symbol": "RELIANCE", "purpose": "Dividend", "date": "15-Jul-2026"},
    {"symbol": "", "purpose": "Financial Results", "date": "15-Jul-2026"},
    {"symbol": "XYZ", "purpose": "Financial Results", "date": "junk"},
]


def test_normalize_keeps_earliest_results_date_per_ticker():
    events = ec.normalize_calendar(_ROWS)
    assert events["TCS.NS"] == "2026-07-14"     # earliest of the two wins
    assert events["INFY.NS"] == "2026-07-23"
    assert "RELIANCE.NS" not in events          # dividend != results
    assert len(events) == 2                     # junk rows dropped


def test_days_to_results_semantics():
    cal = {"TCS.NS": "2026-07-14", "INFY.NS": "2026-07-08"}
    today = date(2026, 7, 10)
    assert ec.days_to_results("TCS.NS", today, cal) == 4
    assert ec.days_to_results("tcs.ns", today, cal) == 4     # case-insensitive
    assert ec.days_to_results("INFY.NS", today, cal) is None  # already past
    assert ec.days_to_results("WIPRO.NS", today, cal) is None  # unknown
    assert ec.days_to_results("TCS.NS", date(2026, 7, 14), cal) == 0


def test_run_overwrites_whole_calendar_so_postponements_self_heal():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        out = tmp / "cal.json"
        original = ec._fetch_nse_calendar
        ec._fetch_nse_calendar = lambda *a, **k: (
            _ROWS, json.dumps(_ROWS).encode())
        try:
            c1 = ec.run(output_path=out, snapshot_path=tmp / "no.json",
                        lake_root=tmp / "lake", today=date(2026, 7, 10))
            # NSE moves TCS to the 18th — the next run must fully replace.
            moved = [{"symbol": "TCS", "purpose": "Financial Results",
                      "date": "18-Jul-2026"}]
            ec._fetch_nse_calendar = lambda *a, **k: (
                moved, json.dumps(moved).encode())
            c2 = ec.run(output_path=out, snapshot_path=tmp / "no.json",
                        lake_root=tmp / "lake", today=date(2026, 7, 11))
        finally:
            ec._fetch_nse_calendar = original
        assert c1["events"]["TCS.NS"] == "2026-07-14"
        assert c2["events"]["TCS.NS"] == "2026-07-18"
        assert "INFY.NS" not in c2["events"]            # full overwrite
        assert ec.load_calendar(out)["TCS.NS"] == "2026-07-18"
        # Both days' copies persist in the lake (date drift is history).
        assert lake.read_day("earnings", "2026-07-10", root=tmp / "lake")
        assert lake.read_day("earnings", "2026-07-11", root=tmp / "lake")


def test_snapshot_fallback_and_load_calendar_fail_open():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        snap = tmp / "snap.json"
        snap.write_text(json.dumps({"rows": _ROWS}))
        c = ec.run(output_path=tmp / "cal.json", snapshot_path=snap,
                   lake_root=tmp / "lake", today=date(2026, 7, 10),
                   use_live=False)
        assert c["source"] == "snapshot" and c["events"]["TCS.NS"] == "2026-07-14"
        assert ec.load_calendar(tmp / "ghost.json") == {}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")


def test_calendar_referer_is_its_own_page_not_the_deals_page():
    """Same 403 class as flows (ledger 2026-07-12)."""
    from src.ingestion import earnings_calendar as ec
    assert "event-calendar" in ec._EVENTS_HEADERS["Referer"]
    assert "bulk-and-block" not in ec._EVENTS_HEADERS["Referer"]

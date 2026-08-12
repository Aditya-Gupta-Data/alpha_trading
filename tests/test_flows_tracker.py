"""
Tests for the FII/DII flows tracker (Phase 1). Fully offline.

Run either of these from the project folder:
    python tests/test_flows_tracker.py
    python -m pytest tests/test_flows_tracker.py
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import lake
from src.ingestion import flows_tracker as ft


_NSE_ROWS = [
    {"category": "FII/FPI *", "date": "10-Jul-2026",
     "buyValue": "12,345.67", "sellValue": "10,000.00", "netValue": "2,345.67"},
    {"category": "DII **", "date": "10-Jul-2026",
     "buyValue": "8,000.00", "sellValue": "9,500.50", "netValue": "-1,500.50"},
]


def test_normalize_matches_categories_and_parses_crores():
    n = ft.normalize_flows(_NSE_ROWS)
    assert n["as_of"] == "2026-07-10"
    assert n["fii"]["net"] == 2345.67 and n["fii"]["buy"] == 12345.67
    assert n["dii"]["net"] == -1500.50


def test_normalize_derives_net_and_refuses_empty():
    rows = [{"category": "FII", "date": "10-Jul-2026",
             "buyValue": "100", "sellValue": "40"}]      # no netValue
    assert ft.normalize_flows(rows)["fii"]["net"] == 60.0
    assert ft.normalize_flows([]) is None                 # no guessed zeros
    assert ft.normalize_flows([{"category": "PRO"}]) is None
    assert ft.normalize_flows("junk") is None


def test_run_persists_json_lake_and_raw_archive():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw = json.dumps(_NSE_ROWS).encode()
        original = ft._fetch_nse_flows
        ft._fetch_nse_flows = lambda *a, **k: (_NSE_ROWS, raw)
        try:
            n = ft.run(output_path=tmp / "flows.json",
                       snapshot_path=tmp / "no-snap.json",
                       lake_root=tmp / "lake", today=date(2026, 7, 10))
        finally:
            ft._fetch_nse_flows = original
        assert n["source"] == "nse" and n["fii"]["net"] == 2345.67
        assert ft.load_flows(tmp / "flows.json")["dii"]["net"] == -1500.50
        assert lake.read_day("flows", "2026-07-10", root=tmp / "lake")
        blobs = list((tmp / "lake" / "flows_raw").rglob("*.json.gz"))
        assert len(blobs) == 1


def test_snapshot_fallback_and_none_day():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        snap = tmp / "snap.json"
        snap.write_text(json.dumps({"rows": _NSE_ROWS}))
        n = ft.run(output_path=tmp / "flows.json", snapshot_path=snap,
                   lake_root=tmp / "lake", today=date(2026, 7, 10),
                   use_live=False)
        assert n["source"] == "snapshot" and n["fii"]["net"] == 2345.67
        # Nothing anywhere -> honest none, no lake write.
        n = ft.run(output_path=tmp / "f2.json", snapshot_path=tmp / "ghost.json",
                   lake_root=tmp / "lake2", today=date(2026, 7, 10),
                   use_live=False)
        assert n["source"] == "none" and n["fii"] is None
        assert not (tmp / "lake2").exists()


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


# ------------------------------------------- ban the stale restamp (08-13)
# The audit signature: logs/flows_tracker.log repeats the same as_of on later
# runs (2026-07-24 ×3, 2026-07-31 ×4) and data/lake/flows jumps 08-03 -> 08-05.
# NSE keeps serving the previous session when today's is unpublished; the old
# code wrote a partition under THAT date, so a stale answer silently rewrote a
# captured day and the day actually asked about left no trace anywhere.

def test_expected_session_walks_back_over_a_weekend():
    assert ft.expected_session(date(2026, 8, 12)) == date(2026, 8, 12)  # Wed
    assert ft.expected_session(date(2026, 8, 8)) == date(2026, 8, 7)    # Sat
    assert ft.expected_session(date(2026, 8, 9)) == date(2026, 8, 7)    # Sun


def test_staleness_verdict_flags_old_future_and_unreadable():
    t = date(2026, 8, 4)
    assert ft.staleness_verdict("2026-08-04", t)["stale"] is False
    assert ft.staleness_verdict("2026-08-03", t)["stale"] is True
    assert ft.staleness_verdict("2026-08-05", t)["stale"] is True   # future
    assert ft.staleness_verdict(None, t)["stale"] is True           # fail-safe
    assert ft.staleness_verdict("garbage", t)["stale"] is True


def test_a_stale_source_writes_NO_partition_and_reaches_the_ops_card(capsys):
    """08-04 reproduced exactly: NSE serves 08-03, we asked for 08-04."""
    from src.ops_monitor import is_problem_line
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "snap.json").write_text(json.dumps(_NSE_ROWS))
        n = ft.run(output_path=tmp / "out.json", snapshot_path=tmp / "snap.json",
                   lake_root=tmp / "lake", today=date(2026, 7, 13),
                   use_live=False)
        # source says 2026-07-10, we asked for Monday 2026-07-13
        assert n["as_of"] == "2026-07-10"
        assert n["stale"] is True and n["expected_as_of"] == "2026-07-13"
        assert not (tmp / "lake" / "flows").exists()   # the restamp is banned
        assert (tmp / "out.json").exists()             # reader file untouched
    out = capsys.readouterr().out
    assert "FL-STALE" in out
    assert any(is_problem_line(ln) for ln in out.splitlines())


def test_a_fresh_source_still_writes_and_stays_silent(capsys):
    from src.ops_monitor import is_problem_line
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "snap.json").write_text(json.dumps(_NSE_ROWS))
        n = ft.run(output_path=tmp / "out.json", snapshot_path=tmp / "snap.json",
                   lake_root=tmp / "lake", today=date(2026, 7, 10),
                   use_live=False)
        assert n["stale"] is False
        assert lake.read_day("flows", "2026-07-10", root=tmp / "lake")
    assert not any(is_problem_line(ln)
                   for ln in capsys.readouterr().out.splitlines())


def test_a_weekend_run_on_fridays_numbers_is_not_stale():
    """The tracker is daily; Sat/Sun legitimately carry Friday. Calling
    that stale would fire a false alarm 104 times a year."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rows = [dict(r, date="10-Jul-2026") for r in _NSE_ROWS]  # Fri
        (tmp / "snap.json").write_text(json.dumps(rows))
        n = ft.run(output_path=tmp / "out.json", snapshot_path=tmp / "snap.json",
                   lake_root=tmp / "lake", today=date(2026, 7, 11),  # Sat
                   use_live=False)
        assert n["stale"] is False
        assert lake.read_day("flows", "2026-07-10", root=tmp / "lake")


def test_flows_referer_is_its_own_page_not_the_deals_page():
    """Ledger 2026-07-12: NSE 403s a JSON call whose Referer doesn't match
    the endpoint's owning page — this module must never borrow the deals
    page's Referer again."""
    from src.ingestion import flows_tracker as ft
    assert ft._FLOWS_HEADERS["Referer"] == "https://www.nseindia.com/reports/fii-dii"
    assert "bulk-and-block" not in ft._FLOWS_HEADERS["Referer"]

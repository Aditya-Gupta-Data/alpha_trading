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

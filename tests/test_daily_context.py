"""
Tests for the daily Market Frame (Phase 2, P2-4). Fully offline.

Run either of these from the project folder:
    python tests/test_daily_context.py
    python -m pytest tests/test_daily_context.py
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, daily_context as dc, lake


_MACRO = {"source": "dhan", "index_impact": {
    "NIFTY 50": {"SHORT": -0.4, "MEDIUM": -0.2},
    "NIFTY BANK": {"SHORT": 0.1, "MEDIUM": 0.0}}}
_NEWS = {"tickers": {
    "TCS.NS": {"sentiment_score": 3, "stale": False},
    "INFY.NS": {"sentiment_score": -1, "stale": False},
    "WIPRO.NS": {"sentiment_score": 5, "stale": True}}}   # stale — excluded
_CENSUS = {"as_of": "2026-07-10", "normalized": 42, "buy_legs": 25,
           "sell_legs": 17}
_FLOWS = {"as_of": "2026-07-10", "fii": {"net": -1200.5},
          "dii": {"net": 900.0}}
_AFFINITY = {"groups": {"ADANI": {"net_bias": "distribution"},
                        "TATA": {"net_bias": "accumulation"},
                        "JINDAL": {"net_bias": "distribution"}}}


def test_build_frame_joins_all_layers():
    f = dc.build_frame("2026-07-10", macro=_MACRO, news=_NEWS,
                       deals_census=_CENSUS, flows=_FLOWS,
                       affinity=_AFFINITY, vix=14.2)
    assert f["vix"] == 14.2 and f["vix_band"] == "mid"
    assert f["macro_nifty_short"] == -0.4 and f["macro_bank_short"] == 0.1
    assert f["news_net"] == 2 and f["news_fresh"] == 2      # stale excluded
    assert f["deals_rows"] == 42 and f["deals_sell_legs"] == 17
    assert f["affinity_distribution"] == 2
    assert f["affinity_accumulation"] == 1
    assert f["fii_net"] == -1200.5 and f["dii_net"] == 900.0


def test_null_honesty_absent_layers_and_mismatched_days():
    f = dc.build_frame("2026-07-10")
    for k, v in f.items():
        if k != "date":
            assert v is None, f"{k} fabricated {v!r}"
    # A census/flows artifact from ANOTHER day contributes nothing.
    f = dc.build_frame("2026-07-11", deals_census=_CENSUS, flows=_FLOWS)
    assert f["deals_rows"] is None and f["fii_net"] is None
    # Macro with source "none" contributes nothing.
    f = dc.build_frame("2026-07-10", macro={"source": "none"})
    assert f["macro_source"] is None


def test_record_frame_upserts_latest_wins():
    conn = brain_map.connect(":memory:")
    assert dc.record_frame(conn, dc.build_frame("2026-07-10", vix=14.0))
    assert dc.record_frame(conn, dc.build_frame("2026-07-10", vix=15.5,
                                                flows=_FLOWS))
    rows = conn.execute("SELECT * FROM daily_context").fetchall()
    assert len(rows) == 1
    assert rows[0]["vix"] == 15.5 and rows[0]["fii_net"] == -1200.5
    assert dc.record_frame(conn, {}) is False


def test_fold_lake_backfills_every_captured_day():
    with tempfile.TemporaryDirectory() as tmp:
        lake.write_partition("macro_daily", "2026-07-08", [_MACRO], root=tmp)
        lake.write_partition("flows", "2026-07-09",
                             [dict(_FLOWS, as_of="2026-07-09")], root=tmp)
        lake.write_partition("chains/nifty", "2026-07-09",
                             [{"vix": 13.1, "expiry": "x", "oc": {}}], root=tmp)
        conn = brain_map.connect(":memory:")
        written = dc.fold_lake(conn, lake_root=tmp)
        assert written == 2
        rows = {r["date"]: dict(r) for r in
                conn.execute("SELECT * FROM daily_context")}
        assert rows["2026-07-08"]["macro_nifty_short"] == -0.4
        assert rows["2026-07-08"]["vix"] is None            # no chain that day
        assert rows["2026-07-09"]["vix"] == 13.1
        assert rows["2026-07-09"]["fii_net"] == -1200.5
        assert rows["2026-07-09"]["macro_source"] is None   # honest NULL


def test_run_for_today_records_and_reports_fill():
    with tempfile.TemporaryDirectory() as tmp:
        lake.write_partition("macro_daily", "2026-07-10", [_MACRO], root=tmp)
        conn = brain_map.connect(":memory:")
        out = dc.run_for_today(conn, today=date(2026, 7, 10), lake_root=tmp)
        assert out["recorded"] is True and out["fields_filled"] >= 4
        row = conn.execute("SELECT macro_source FROM daily_context "
                           "WHERE date = '2026-07-10'").fetchone()
        assert row["macro_source"] == "dhan"


def test_sleep_phase_runs_task_g():
    from src import sleep_phase
    r = sleep_phase.run_sleep_phase(db_path=":memory:",
                                    today=date(2026, 7, 10))
    assert "daily_context" in r and r["daily_context"] is not None
    assert r["daily_context"]["date"] == "2026-07-10"


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

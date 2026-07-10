"""
Tests for the Phase-0 candle persistence tap (live_bridge.CandleSink).
Fully offline — synthetic packet playback, temp lake roots.

Run either of these from the project folder:
    python tests/test_candle_sink.py
    python -m pytest tests/test_candle_sink.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import lake
from src.live_bridge import CandleAggregator, CandleSink, live_cycle


def _pk(hh, mm, price):
    return {"ticker": "NIFTY 50", "price": price,
            "ts": datetime(2026, 7, 10, hh, mm)}


def test_only_completed_candles_seal_and_write_once():
    with tempfile.TemporaryDirectory() as tmp:
        agg = CandleAggregator(minutes=15)
        sink = CandleSink(lake_root=tmp)
        agg.ingest(_pk(9, 16, 100.0))
        agg.ingest(_pk(9, 20, 101.0))
        assert sink.observe("NIFTY 50", agg) == 0      # bucket still forming
        agg.ingest(_pk(9, 31, 102.0))                  # next bucket opens
        assert sink.observe("NIFTY 50", agg) == 1      # 09:15 candle sealed
        assert sink.observe("NIFTY 50", agg) == 0      # idempotent
        rows = lake.read_day("candles/nifty-50", "2026-07-10", root=tmp)
        assert len(rows) == 1
        c = rows[0]
        assert c["type"] == "candle" and c["source"] == "poll"
        assert c["open"] == 100.0 and c["high"] == 101.0 and c["close"] == 101.0
        assert c["minutes"] == 15


def test_missing_bucket_records_explicit_gap_marker():
    with tempfile.TemporaryDirectory() as tmp:
        agg = CandleAggregator(minutes=15)
        sink = CandleSink(lake_root=tmp)
        agg.ingest(_pk(9, 20, 100.0))     # 09:15 bucket
        agg.ingest(_pk(10, 5, 105.0))     # 10:00 bucket — 09:30/09:45 missing
        agg.ingest(_pk(10, 20, 106.0))    # 10:15 bucket opens -> seals both
        written = sink.observe("NIFTY 50", agg)
        rows = lake.read_day("candles/nifty-50", "2026-07-10", root=tmp)
        kinds = [r["type"] for r in rows]
        assert kinds == ["candle", "gap", "candle"]    # gap explicit, no fill
        gap = rows[1]
        assert gap["after"].endswith("09:15:00") and gap["before"].endswith("10:00:00")
        assert written == 3


def test_sink_failure_never_raises_into_the_cycle():
    agg = CandleAggregator(minutes=15)
    agg.ingest(_pk(9, 16, 100.0))
    agg.ingest(_pk(9, 31, 101.0))
    sink = CandleSink(lake_root="/dev/null/impossible")   # unwritable root
    assert sink.observe("NIFTY 50", agg) == 0             # logged, no raise


def test_live_cycle_accepts_and_drives_the_sink():
    with tempfile.TemporaryDirectory() as tmp:
        sink = CandleSink(lake_root=tmp)
        aggs = {}
        prices = iter([25000.0, 25010.0, 25020.0])

        def quote_fn(u):
            return {"current_price": next(prices)}

        # Three cycles across two buckets -> first candle seals in cycle 3.
        for hh, mm in ((9, 20), (9, 25), (9, 31)):
            live_cycle(("NIFTY 50",), quote_fn=quote_fn, entries=[],
                       aggregators=aggs, candle_sink=sink,
                       now_fn=lambda h=hh, m=mm: datetime(2026, 7, 10, h, m))
        rows = lake.read_day("candles/nifty-50", "2026-07-10", root=tmp)
        assert len(rows) == 1 and rows[0]["close"] == 25010.0
        # Legacy call without a sink stays a pure no-op path.
        live_cycle(("NIFTY 50",), quote_fn=lambda u: {"current_price": 1.0},
                   entries=[], aggregators={},
                   now_fn=lambda: datetime(2026, 7, 10, 9, 40))


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

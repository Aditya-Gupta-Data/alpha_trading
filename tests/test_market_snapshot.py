"""
Offline tests for the engine's published market read-model
(src/market_snapshot.py) and the dashboard's preference for it
(src/api.py GET /api/web/positions):

  * write/read round-trip, atomicity, and fail-safe behaviour on
    missing / corrupt / stale files;
  * the live loop publishes a snapshot when asked and stays silent
    otherwise (src/live_bridge.live_cycle);
  * the dashboard serves the engine's snapshot marks with ZERO Dhan
    calls when one is fresh, and falls back to a direct mark only when
    it isn't — reporting `mark_source` honestly either way.

Fully offline: no broker socket, no live token; every Dhan touch is
mocked and every file lives in a temp dir.

Run either of these from the project folder:
    python tests/test_market_snapshot.py
    python -m pytest tests/
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import market_snapshot as ms

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 7, 10, 13, 30, tzinfo=IST)

SPOTS = {"NIFTY 50": 24170.5, "NIFTY BANK": 58002.1}
MARKS = [
    {"short_id": "25da25ec", "ticker": "NIFTY 50", "strategy": "bear_put_spread",
     "signal": "hold", "live_pnl_rs": -836.56, "capture_pct": -9.0, "days_left": 11},
    {"short_id": "af18c8cf", "ticker": "NIFTY BANK", "strategy": "bear_put_spread",
     "signal": "hold", "live_pnl_rs": -1858.78, "capture_pct": -22.0, "days_left": 18},
]


# ------------------------------------------------------- write / read

def test_write_read_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "snap.json"
        assert ms.write(SPOTS, MARKS, now=NOW, path=p) is True
        snap = ms.read(path=p)
        assert snap["spots"] == SPOTS
        assert len(snap["marks"]) == 2
        assert snap["as_of"] == NOW.isoformat(timespec="seconds")
        assert ms.marks_by_id(snap)["25da25ec"]["live_pnl_rs"] == -836.56
        assert ms.spot_for(snap, "NIFTY BANK") == 58002.1


def test_read_missing_file_is_none():
    assert ms.read(path="/nonexistent/dir/snap.json") is None


def test_read_corrupt_file_is_none():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "snap.json"
        p.write_text("{not json")
        assert ms.read(path=p) is None


def test_stale_snapshot_reads_as_none_when_max_age_set():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "snap.json"
        ms.write(SPOTS, MARKS, now=NOW, path=p)
        later = NOW + timedelta(seconds=400)   # older than the 180s window
        assert ms.read(max_age_seconds=180, path=p, now=later) is None
        # fresh read (no max_age) still returns it
        assert ms.read(path=p, now=later) is not None
        # and within the window it is served
        assert ms.read(max_age_seconds=180, path=p,
                       now=NOW + timedelta(seconds=120)) is not None


def test_write_is_atomic_no_partial_file_left():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "snap.json"
        ms.write(SPOTS, MARKS, now=NOW, path=p)
        # only the final file exists — no *.tmp turds from the atomic swap
        leftovers = [f.name for f in Path(tmp).iterdir() if f.suffix == ".tmp"]
        assert leftovers == []
        assert json.loads(p.read_text())["epoch"] == NOW.timestamp()


def test_write_failure_returns_false_never_raises():
    # a directory that can't be created (a file sits where the parent dir
    # should be) — write must swallow it and report False
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "blocker"
        blocker.write_text("x")
        target = blocker / "snap.json"   # blocker is a file, not a dir
        assert ms.write(SPOTS, MARKS, now=NOW, path=target) is False


def test_marks_by_id_and_spot_for_tolerate_junk():
    assert ms.marks_by_id({"marks": ["notadict", {"no": "id"}]}) == {}
    assert ms.marks_by_id(None) == {}
    assert ms.spot_for(None, "NIFTY 50") is None
    assert ms.spot_for({"spots": {"NIFTY 50": None}}, "NIFTY 50") is None


# ---------------------------------------------- live_cycle publishing

def _quote(price):
    return lambda ticker: {"ticker": ticker, "current_price": price,
                           "prev_close": price, "percent_change": 0.0}


def test_live_cycle_publishes_snapshot_only_when_asked():
    from src import live_bridge
    open_market = datetime(2026, 7, 10, 11, 0, tzinfo=IST)  # inside 09:15-15:30
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "snap.json"
        with mock.patch("src.market_snapshot.SNAPSHOT_PATH", p):
            # default: no publish
            live_bridge.live_cycle(["NIFTY 50"], quote_fn=_quote(24000.0),
                                   entries=[], now_fn=lambda: open_market)
            assert not p.exists()
            # opt-in: publishes the cycle's spots (marks empty — no entries)
            live_bridge.live_cycle(["NIFTY 50"], quote_fn=_quote(24000.0),
                                   entries=[], now_fn=lambda: open_market,
                                   publish_snapshot=True)
            snap = ms.read(path=p)
    assert snap is not None
    assert snap["spots"]["NIFTY 50"] == 24000.0


# ------------------------------------ dashboard prefers engine snapshot

def _open_spread_entry(short_id, ticker):
    return {"short_id": short_id, "ticker": ticker, "decision": "approved",
            "outcome": None, "date": "2026-07-09",
            "signal": f"bearish {ticker}",
            "spread": {"strategy": "bear_put_spread", "direction": "bearish",
                       "legs": [{"side": "BUY", "option_type": "PE",
                                 "strike": 24050.0, "premium": 200.0},
                                {"side": "SELL", "option_type": "PE",
                                 "strike": 23850.0, "premium": 130.0}],
                       "expiry": "2026-07-21", "lots": 1,
                       "max_loss": 70.0, "max_profit": 130.0}}


def test_dashboard_serves_snapshot_marks_with_zero_dhan_calls():
    """A fresh engine snapshot exists -> the dashboard reports its P&L and
    NEVER constructs a SafeDhanClient (no Dhan call, no contention)."""
    import src.api as api
    entries = [_open_spread_entry("25da25ec", "NIFTY 50"),
               _open_spread_entry("af18c8cf", "NIFTY BANK")]
    with tempfile.TemporaryDirectory() as tmp:
        snap_path = Path(tmp) / "snap.json"
        ms.write(SPOTS, MARKS, now=datetime.now(IST), path=snap_path)
        with mock.patch("src.market_snapshot.SNAPSHOT_PATH", snap_path), \
             mock.patch("src.positions.journal.read_all", return_value=entries), \
             mock.patch("src.portfolio_report.journal.read_all", return_value=entries), \
             mock.patch("src.dhan_guard.SafeDhanClient") as SafeCls, \
             mock.patch("src.portfolio_report.read_exposure", return_value=None):
            res = api.web_positions()
    assert res["mark_source"] == "engine_snapshot"
    SafeCls.assert_not_called()      # the whole point: no Dhan fetch
    pnl = {p["trade_id"]: p["live_pnl_rs"] for p in res["positions"]}
    assert pnl["25da25ec"] == -836.56
    assert pnl["af18c8cf"] == -1858.78
    detail = {p["trade_id"]: p["live_detail"] for p in res["positions"]}
    assert "11d to expiry" in detail["25da25ec"]


def test_dashboard_falls_back_to_direct_when_snapshot_stale():
    """No fresh snapshot -> the dashboard marks directly and says so."""
    import src.api as api
    entries = [_open_spread_entry("25da25ec", "NIFTY 50")]
    direct = [{"short_id": "25da25ec", "ticker": "NIFTY 50",
               "strategy": "bear_put_spread", "live_pnl_rs": -500.0,
               "detail": "-6% of max profit, 11d to expiry"}]
    with tempfile.TemporaryDirectory() as tmp:
        stale_path = Path(tmp) / "snap.json"
        old = datetime.now(IST) - timedelta(seconds=600)
        ms.write(SPOTS, MARKS, now=old, path=stale_path)   # too old -> ignored
        with mock.patch("src.market_snapshot.SNAPSHOT_PATH", stale_path), \
             mock.patch("src.positions.journal.read_all", return_value=entries), \
             mock.patch("src.portfolio_report._open_entries",
                        return_value=(entries, [])), \
             mock.patch("src.portfolio_report.mark_positions",
                        return_value=direct) as mp, \
             mock.patch("src.dhan_guard.SafeDhanClient"), \
             mock.patch("src.portfolio_report.read_exposure", return_value=None):
            res = api.web_positions()
    assert res["mark_source"] == "direct_fetch"
    mp.assert_called_once()
    assert res["positions"][0]["live_pnl_rs"] == -500.0


def test_dashboard_no_snapshot_no_token_degrades_to_na():
    """Neither a snapshot nor a working quote -> n/a, never an error."""
    import src.api as api
    entries = [_open_spread_entry("25da25ec", "NIFTY 50")]
    with mock.patch("src.market_snapshot.SNAPSHOT_PATH",
                    Path("/nonexistent/snap.json")), \
         mock.patch("src.positions.journal.read_all", return_value=entries), \
         mock.patch("src.portfolio_report._open_entries",
                    return_value=(entries, [])), \
         mock.patch("src.portfolio_report.mark_positions", return_value=[]), \
         mock.patch("src.dhan_guard.SafeDhanClient"), \
         mock.patch("src.portfolio_report.read_exposure", return_value=None):
        res = api.web_positions()
    assert res["mark_source"] is None
    assert res["positions"][0]["live_pnl_rs"] is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}  {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

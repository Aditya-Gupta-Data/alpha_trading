"""Audit Chunk 3, Batch C: W1 — the regime advisory reaches the LIVE market fetch."""
import sqlite3
from datetime import datetime
from src import live_bridge as lb
from src.analysis import regime_filters as rf
from src.market_loop import IST

OPEN = datetime(2026, 7, 6, 11, 0, tzinfo=IST)
CLOSES = [100.0 + i * 0.1 for i in range(260)]


def _fetch(advisory_fn):
    return lb.fetch_live_market_state("NIFTY 50", quote_fn=lambda u: {"last_price": 126.0},
                                      closes_fn=lambda u: CLOSES, vix_fn=lambda: 14.0,
                                      now_fn=lambda: OPEN, advisory_fn=advisory_fn)


def test_w1_live_fetch_carries_the_advisory():
    seen = {}

    def adv(underlying, vix=None, as_of=None):
        seen.update(underlying=underlying, vix=vix, as_of=as_of)
        return {"block_bullish": True, "bullish_reason": "distribution", "crisis": False}
    state = _fetch(adv)
    assert state["advisory"]["block_bullish"] and seen == {"underlying": "NIFTY 50", "vix": 14.0, "as_of": OPEN.date()}
    assert "advisory" not in _fetch(lambda *a, **k: None)          # switched off / unavailable
    assert "advisory" not in _fetch(lambda *a, **k: 1 / 0)         # fail-open


def test_w1_advisory_door_honours_the_switch_and_abstains(monkeypatch):
    monkeypatch.setattr("src.config.REGIME_ADVISORY_ENABLED", False)
    assert rf.advisory_for("NIFTY 50", vix=30.0, as_of="2026-07-06", deals_by_ticker={}) is None
    monkeypatch.setattr("src.config.REGIME_ADVISORY_ENABLED", True)
    adv = rf.advisory_for("NIFTY 50", vix=30.0, as_of="2026-07-06", deals_by_ticker={}, prev_vix=20.0)
    assert adv is not None and adv.get("crisis")


def test_w1_prev_session_vix_reads_the_last_close_before_the_day():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE daily_context (date TEXT PRIMARY KEY, vix REAL, payload TEXT NOT NULL)")
    conn.executemany("INSERT INTO daily_context VALUES (?, ?, '{}')",
                     [("2026-07-01", 13.0), ("2026-07-02", 15.5), ("2026-07-03", None)])
    assert rf.prev_session_vix("2026-07-03", conn=conn) == 15.5
    assert rf.prev_session_vix("2026-07-01", conn=conn) is None

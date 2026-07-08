"""
Phase 6H live bridge tests — fully offline packet playback.

Recorded-style quote packets are replayed through parse_packet /
CandleAggregator / live_cycle, and open positions are marked against
injected spots — no network, no Dhan token, no real journal/portfolio
file is ever touched (runtime-spied).

Run from the project folder:
    python tests/test_live_bridge.py      (simple, no extra installs)
    python -m pytest tests/               (if you have pytest)
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import live_bridge as lb
from src.market_loop import IST

# Wed 2026-07-08, 10:30 IST — comfortably inside market hours.
MARKET_NOW = datetime(2026, 7, 8, 10, 30, tzinfo=IST)
SATURDAY = datetime(2026, 7, 11, 10, 30, tzinfo=IST)
AFTER_CLOSE = datetime(2026, 7, 8, 16, 0, tzinfo=IST)


def make_condor_entry(short_id="live0001", entry_date="2026-07-01",
                      expiry="2026-07-11", decision="approved",
                      outcome=None) -> dict:
    """A journal-shaped approved iron condor: 35 lot, one lot, entry
    credit 70/share (90-55+95-60), width 200 -> max_profit 70/share,
    max_loss 130/share. entry_spot 52000 (all legs OTM at entry)."""
    lot = 35
    return {
        "short_id": short_id, "date": entry_date, "action": "SPREAD",
        "ticker": "NIFTY BANK", "shares": lot, "price": 70.0,
        "signal": "test condor", "decision": decision, "why": "test",
        "outcome": outcome,
        "spread": {
            "strategy": "iron_condor", "lot_size": lot, "lots": 1,
            "expiry": expiry, "entry_spot": 52_000.0,
            "net_credit": 70.0, "net_debit": None, "spread_width": 200.0,
            "max_profit": 70.0 * lot, "max_loss": 130.0 * lot,
            "legs": [
                {"side": "SELL", "option_type": "PE", "strike": 51_000.0, "premium": 90.0},
                {"side": "BUY", "option_type": "PE", "strike": 50_800.0, "premium": 55.0},
                {"side": "SELL", "option_type": "CE", "strike": 53_000.0, "premium": 95.0},
                {"side": "BUY", "option_type": "CE", "strike": 53_200.0, "premium": 60.0},
            ],
        },
    }


# --- packet parsing ------------------------------------------------------

def test_parse_packet_accepts_the_get_quote_shape():
    p = lb.parse_packet({"ticker": "NIFTY 50", "current_price": 25_432.1,
                         "prev_close": 25_400.0, "percent_change": 0.13})
    assert p["ticker"] == "NIFTY 50"
    assert p["price"] == 25_432.1
    assert p["ts"] is not None


def test_parse_packet_accepts_raw_last_price_and_iso_ts():
    p = lb.parse_packet({"last_price": "51000.5", "ticker": "NIFTY BANK",
                         "ts": "2026-07-08T10:30:00"})
    assert p["price"] == 51_000.5
    assert p["ts"] == datetime(2026, 7, 8, 10, 30)


def test_parse_packet_drops_garbage():
    assert lb.parse_packet(None) is None
    assert lb.parse_packet("not a dict") is None
    assert lb.parse_packet({}) is None
    assert lb.parse_packet({"current_price": "n/a"}) is None
    assert lb.parse_packet({"last_price": 0}) is None
    assert lb.parse_packet({"last_price": -5}) is None


def test_parse_packet_survives_a_malformed_timestamp():
    p = lb.parse_packet({"ltp": 100.0, "ts": "yesterday-ish"})
    assert p["price"] == 100.0
    assert p["ts"] is not None  # fell back to the live clock


# --- candle aggregation --------------------------------------------------

def test_candle_playback_builds_ohlc_buckets():
    agg = lb.CandleAggregator(minutes=15)
    stream = [(datetime(2026, 7, 8, 10, 0), 100.0),
              (datetime(2026, 7, 8, 10, 5), 104.0),
              (datetime(2026, 7, 8, 10, 14), 98.0),
              (datetime(2026, 7, 8, 10, 15), 99.0),   # next bucket
              (datetime(2026, 7, 8, 10, 29), 101.0)]
    for ts, price in stream:
        agg.ingest({"ticker": "X", "price": price, "ts": ts})
    candles = agg.candles()
    assert len(candles) == 2
    first, second = candles
    assert first["start"] == datetime(2026, 7, 8, 10, 0)
    assert (first["open"], first["high"], first["low"], first["close"]) == \
        (100.0, 104.0, 98.0, 98.0)
    assert second["start"] == datetime(2026, 7, 8, 10, 15)
    assert (second["open"], second["high"], second["low"], second["close"]) == \
        (99.0, 101.0, 99.0, 101.0)
    assert agg.last_price() == 101.0


def test_candle_aggregator_ignores_dropped_packets():
    agg = lb.CandleAggregator()
    assert agg.ingest(None) is None
    assert agg.candles() == [] and agg.last_price() is None


# --- ENTRY: the live fetch_market_state ----------------------------------

def _closes(n=220, start=24_000.0, step=5.0):
    return [start + i * step for i in range(n)]


def test_fetch_live_state_appends_live_spot_to_the_trend_read():
    live = 25_432.1
    state = lb.fetch_live_market_state(
        "NIFTY 50",
        quote_fn=lambda u: {"ticker": u, "current_price": live},
        closes_fn=lambda u: _closes(),
        vix_fn=lambda: 13.5,
        now_fn=lambda: MARKET_NOW)
    assert state is not None
    # same contract as market_loop.fetch_market_state
    assert set(state) >= {"analysis", "vix"}
    assert state["vix"] == 13.5
    # the analysis's price IS the live snapshot, not yesterday's close
    assert state["analysis"]["price"] == live
    assert state["analysis"]["ticker"] == "NIFTY 50"
    assert state["analysis"]["uptrend"] is True  # rising synthetic series


def test_fetch_live_state_is_none_outside_market_hours():
    kwargs = dict(quote_fn=lambda u: {"current_price": 100.0},
                  closes_fn=lambda u: _closes(), vix_fn=lambda: 12.0)
    assert lb.fetch_live_market_state("NIFTY 50", now_fn=lambda: SATURDAY,
                                      **kwargs) is None
    assert lb.fetch_live_market_state("NIFTY 50", now_fn=lambda: AFTER_CLOSE,
                                      **kwargs) is None


def test_fetch_live_state_is_none_on_dead_quote_or_thin_history():
    assert lb.fetch_live_market_state(
        "NIFTY 50", quote_fn=lambda u: None, closes_fn=lambda u: _closes(),
        vix_fn=lambda: 12.0, now_fn=lambda: MARKET_NOW) is None
    assert lb.fetch_live_market_state(
        "NIFTY 50", quote_fn=lambda u: {"current_price": 100.0},
        closes_fn=lambda u: [], vix_fn=lambda: 12.0,
        now_fn=lambda: MARKET_NOW) is None
    assert lb.fetch_live_market_state(
        "NIFTY 50", quote_fn=lambda u: {"current_price": 100.0},
        closes_fn=lambda u: _closes(50),   # < 200-day SMA warmup
        vix_fn=lambda: 12.0, now_fn=lambda: MARKET_NOW) is None


def test_fetch_live_state_carries_vol_bridge_overrides_when_available():
    from src import vol_bridge
    saved = vol_bridge.compute_regime_overrides
    try:
        vol_bridge.compute_regime_overrides = lambda *a, **k: {"risk_pct": 7.0}
        state = lb.fetch_live_market_state(
            "NIFTY 50", quote_fn=lambda u: {"current_price": 25_000.0},
            closes_fn=lambda u: _closes(), vix_fn=lambda: 12.0,
            now_fn=lambda: MARKET_NOW)
        assert state["vol_overrides"] == {"risk_pct": 7.0}

        vol_bridge.compute_regime_overrides = _boom
        state = lb.fetch_live_market_state(
            "NIFTY 50", quote_fn=lambda u: {"current_price": 25_000.0},
            closes_fn=lambda u: _closes(), vix_fn=lambda: 12.0,
            now_fn=lambda: MARKET_NOW)
        assert state is not None and "vol_overrides" not in state
    finally:
        vol_bridge.compute_regime_overrides = saved


def _boom(*a, **k):
    raise RuntimeError("bridge down")


# --- EXIT: real-time position evaluation ---------------------------------
# The condor: entry 2026-07-01, expiry 2026-07-11 (10 days total),
# credit 70/share -> profit take at >= 45.5/share (65%).

def test_evaluate_position_holds_early_in_the_trade():
    sig = lb.evaluate_position(make_condor_entry(), spot=52_000.0,
                               today=date(2026, 7, 3))   # 8 of 10 days left
    # time decay so far: 70 x (1 - 0.8) = 14/share — nowhere near the take
    assert sig["signal"] == "hold"
    assert sig["capture_pct"] == 20.0
    assert sig["live_pnl_rs"] == 14.0 * 35


def test_evaluate_position_fires_the_65pct_profit_take_intraday():
    sig = lb.evaluate_position(make_condor_entry(), spot=52_000.0,
                               today=date(2026, 7, 8))   # 3 of 10 days left
    # decay profit: 70 x (1 - 0.3) = 49/share = 70% of max — take it
    assert sig["signal"] == "profit_take"
    assert sig["capture_pct"] == 70.0
    assert sig["live_pnl_rs"] == 49.0 * 35
    assert sig["days_left"] == 3


def test_evaluate_position_fires_the_pre_expiry_rule_when_underwater():
    sig = lb.evaluate_position(make_condor_entry(), spot=50_900.0,
                               today=date(2026, 7, 9))   # 2 days left
    # short put 100 ITM: profit = -(100 + 70x0.2) - (-70) = -44/share
    assert sig["signal"] == "pre_expiry_exit"
    assert sig["live_pnl_rs"] == -44.0 * 35


def test_evaluate_position_clamps_at_the_structures_max_loss():
    sig = lb.evaluate_position(make_condor_entry(), spot=50_000.0,
                               today=date(2026, 7, 6))   # crash through wing
    # unclamped model says -165/share; a 200-wide condor at 70 credit can
    # only ever lose 130/share — the clamp must hold
    assert sig["live_pnl_rs"] == -130.0 * 35
    assert sig["signal"] == "hold"  # defined risk: no stop, exit by rules


def test_evaluate_open_positions_matches_only_active_approved_spreads():
    entries = [
        make_condor_entry("open0001"),
        make_condor_entry("rejected1", decision="rejected"),
        make_condor_entry("pending01", decision="pending_approval"),
        make_condor_entry("resolved1", outcome={"resolution": "profit_take"}),
        dict(make_condor_entry("no-quote1"), ticker="NIFTY 50"),
    ]
    signals = lb.evaluate_open_positions({"NIFTY BANK": 52_000.0}, entries,
                                         today=date(2026, 7, 8))
    assert [s["short_id"] for s in signals] == ["open0001"]


# --- the live cycle (playback end-to-end) ---------------------------------

def test_live_cycle_playback_alerts_each_exit_signal_exactly_once():
    entries = [make_condor_entry()]         # profit-takeable on 2026-07-08
    aggregators, registry, notes = {}, lb.AlertRegistry(), []
    kwargs = dict(
        quote_fn=lambda u: {"ticker": u, "current_price": 52_000.0},
        entries=entries, aggregators=aggregators, registry=registry,
        notify_fn=notes.append, now_fn=lambda: MARKET_NOW)

    fired = lb.live_cycle(("NIFTY BANK",), **kwargs)
    assert len(fired) == 1 and fired[0]["signal"] == "profit_take"
    assert len(notes) == 1 and "profit take" in notes[0]
    assert "advisory" in notes[0].lower()   # never an execution
    # the packet also landed in the candle stream
    assert aggregators["NIFTY BANK"].last_price() == 52_000.0

    # same conditions next minute: alert must NOT repeat
    assert lb.live_cycle(("NIFTY BANK",), **kwargs) == []
    assert len(notes) == 1


def test_live_cycle_is_quiet_outside_market_hours():
    assert lb.live_cycle(("NIFTY BANK",),
                         quote_fn=lambda u: {"current_price": 52_000.0},
                         entries=[make_condor_entry()],
                         now_fn=lambda: SATURDAY) == []


def test_live_cycle_survives_a_dead_quote_feed():
    def broken(u):
        raise RuntimeError("feed down")
    assert lb.live_cycle(("NIFTY BANK",), quote_fn=broken,
                         entries=[make_condor_entry()],
                         now_fn=lambda: MARKET_NOW) == []


def test_live_bridge_never_mutates_trade_state():
    """The paper-sandbox guard: a full live cycle with exit signals firing
    must never write the journal, settle cash, or touch the portfolio."""
    from src import journal, portfolio
    from src import plan_tracker
    saved = (journal.log, journal.rewrite_all, portfolio.save,
             plan_tracker._settle_spread_cash)

    def forbidden(*a, **k):
        raise AssertionError("live_bridge mutated trade state!")

    try:
        journal.log = forbidden
        journal.rewrite_all = forbidden
        portfolio.save = forbidden
        plan_tracker._settle_spread_cash = forbidden
        fired = lb.live_cycle(
            ("NIFTY BANK",),
            quote_fn=lambda u: {"ticker": u, "current_price": 52_000.0},
            entries=[make_condor_entry()], notify_fn=lambda t: None,
            now_fn=lambda: MARKET_NOW)
        assert len(fired) == 1  # signals fired, state untouched
    finally:
        (journal.log, journal.rewrite_all, portfolio.save,
         plan_tracker._settle_spread_cash) = saved


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

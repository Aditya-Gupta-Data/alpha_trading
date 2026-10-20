"""Decision #151 (Architect, 2026-10-20): the earnings proximity gate and the
glassbreaking seam — trade_planner's pure rules, enforced in options_proposer. Offline."""
import json
from datetime import date, timedelta

from src import options_proposer as op, trade_planner as tp
from src.ingestion import earnings_calendar as ec
from tests.test_options_proposer import BIG_BOOK, FUTURE_EXPIRY, make_analysis, make_chain

TODAY = date.today()


# --------------------------------------------------------------- the rules

def test_the_gate_refuses_short_vol_and_the_bear_put_within_five_days():
    assert tp.EARNINGS_GATED_STRATEGIES == {"iron_condor", "iron_butterfly", "bull_put_spread",
                                            "bear_call_spread", "bear_put_spread"}
    for strategy in sorted(tp.EARNINGS_GATED_STRATEGIES):
        for days in (0, 2, 5):
            assert "earnings proximity gate" in tp.earnings_proximity_block(strategy, days)
        assert tp.earnings_proximity_block(strategy, 6) is None
        assert tp.earnings_proximity_block(strategy, None) is None          # unknown: no block
    assert tp.earnings_proximity_block("bull_call_spread", 1) is None
    assert "results in 2 day(s) (<= 5) — iron_butterfly refused" in tp.earnings_proximity_block(
        "iron_butterfly", 2)


def test_the_planner_turns_a_gated_plan_into_a_named_no_trade():
    state = {"spot": 2400.0, "underlying": "NIFTY BANK", "trend": "neutral", "iv_regime": "high"}
    assert tp.map_technical_to_strategy(state)["strategy"] == "iron_condor"
    gated = tp.map_technical_to_strategy(dict(state, days_to_results=3))
    assert gated["strategy"] == "no_trade" and "earnings proximity gate" in gated["rationale"]
    assert tp.map_technical_to_strategy(dict(state, days_to_results=9))["strategy"] == "iron_condor"
    broken = tp.map_technical_to_strategy(dict(state, glassbreaking={"fired": True, "reason": "gap-up +3.90%"}))
    assert broken["strategy"] == "no_trade" and "SMA trend read is bypassed" in broken["rationale"]


def _bars(n=21, close=100.0, volume=1000.0):
    return [{"close": close, "volume": volume} for _ in range(n)]


def test_glassbreaking_seam_fires_only_on_a_confirmed_gap_up():
    hist = _bars()
    no_feed = tp.glassbreaking_signals(hist, {"open": 103.9, "volume": None}, require_volume=True)
    assert not no_feed["fired"] and no_feed["gap_pct"] == 3.9 and "not wired" in no_feed["reason"]
    gap_only = tp.glassbreaking_signals(hist, {"open": 103.9}, require_volume=False)
    assert gap_only["fired"] and gap_only["volume_confirmed"] is None
    spike = tp.glassbreaking_signals(hist, {"open": 103.9, "volume": 2500.0}, require_volume=True)
    assert spike["fired"] and spike["volume_confirmed"] and spike["sma_bypassed"]
    thin = tp.glassbreaking_signals(hist, {"open": 103.9, "volume": 1200.0}, require_volume=True)
    assert not thin["fired"] and thin["volume_confirmed"] is False
    flat = tp.glassbreaking_signals(hist, {"open": 101.0, "volume": 9999.0})
    assert not flat["fired"] and "no gap-up" in flat["reason"]
    assert tp.glassbreaking_block("bear_put_spread", spike).endswith("bear_put_spread refused (#151)")
    assert tp.glassbreaking_block("bull_call_spread", spike) is None
    assert tp.glassbreaking_block("iron_condor", no_feed) is None


# ------------------------------------------------------- the proposer gate

def _calendar(tmp_path, monkeypatch, events):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"as_of": TODAY.isoformat(), "source": "nse", "events": events,
                               "past": {}, "last": {}}))
    monkeypatch.setattr(ec, "OUTPUT_PATH", cal)


def test_days_to_results_reads_a_stock_never_an_index(tmp_path, monkeypatch):
    _calendar(tmp_path, monkeypatch, {"TCS.NS": (TODAY + timedelta(days=2)).isoformat()})
    assert op._gate_days_to_results("TCS.NS", TODAY) == (2, None)
    assert op._gate_days_to_results("INFY.NS", TODAY) == (None, "no announced results date")
    assert op._gate_days_to_results("NIFTY 50", TODAY)[0] is None
    monkeypatch.setattr(ec, "OUTPUT_PATH", tmp_path / "missing.json")
    assert op._gate_days_to_results("TCS.NS", TODAY) == (None, "earnings calendar missing")


def _build(**kw):
    return op.build_proposal("NIFTY 50", analysis=make_analysis(uptrend=True, rsi=55), vix=13.0,
                             expiry=FUTURE_EXPIRY, chain=make_chain(), prices={}, **kw)


def test_the_proposer_refuses_a_gated_structure_and_says_why(monkeypatch):
    monkeypatch.setattr(op, "_gate_days_to_results", lambda u, t=None: (2, None))
    r = _build(book=dict(BIG_BOOK))
    assert r["proposal"] is None and r["reason"].startswith("earnings proximity gate: results in 2 day(s)")
    assert r["rejected_spread"]["strategy"] == "iron_condor" and r["event_gates"]["days_to_results"] == 2
    monkeypatch.setattr(op, "_gate_days_to_results", lambda u, t=None: (6, None))
    ok = _build(book=dict(BIG_BOOK))
    assert ok["proposal"] is not None
    assert ok["proposal"]["event_gates"]["block"] is None and ok["proposal"]["event_gates"]["days_to_results"] == 6


def test_a_bullish_structure_passes_the_gate(monkeypatch):
    monkeypatch.setattr(op, "_gate_days_to_results", lambda u, t=None: (1, None))
    r = op.build_proposal("NIFTY 50", analysis=make_analysis(uptrend=True, fresh_cross=True), vix=13.0,
                          expiry=FUTURE_EXPIRY, chain=make_chain(), prices={}, book=dict(BIG_BOOK))
    assert r["proposal"] is not None and r["proposal"]["spread"]["strategy"] == "bull_call_spread"


def test_on_a_real_run_the_learner_takes_the_gated_setup_and_capital_never_does(monkeypatch):
    from src import portfolio as pf
    monkeypatch.setattr(op, "_gate_days_to_results", lambda u, t=None: (2, None))
    monkeypatch.setattr(op, "_learner_enabled", lambda: True)
    monkeypatch.setattr(pf, "load", lambda *a, **k: dict(BIG_BOOK))
    r = _build(account_equity=2_000_000.0, account_cash=2_000_000.0)
    assert r["proposal"] is not None and r["advisory_block"].startswith("earnings proximity gate")
    monkeypatch.setattr(op, "_learner_enabled", lambda: False)
    assert _build(account_equity=2_000_000.0, account_cash=2_000_000.0)["proposal"] is None


def test_an_unreadable_gate_never_refuses(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendar exploded")
    monkeypatch.setattr(op, "_gate_days_to_results", boom)
    r = _build(book=dict(BIG_BOOK))
    assert r["proposal"] is not None
    assert "earnings gate unavailable" in r["proposal"]["event_gates"]["earnings_reason"]

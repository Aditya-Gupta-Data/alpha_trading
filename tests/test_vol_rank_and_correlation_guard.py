"""Decision #109 — the volatility-edge gate (HV rank ≥ 50 for short-vega
structures) and the correlation guard (max 2 open directional spreads per
thesis across the book, refused CORRELATION_GUARD_HIT)."""
import math
import random

import pytest

from src import exposure_gate as eg, options_proposer as op, vol_rank as vr
from tests.test_exposure_gate import _TempLedger, _open_entry, _proposal
from tests.test_options_proposer import BIG_BOOK, graded, make_chain


def _closes(n=320, daily_vol=0.01, seed=1, tail_vol=None, tail=20):
    rnd = random.Random(seed)
    px, out = 100.0, []
    for i in range(n):
        v = tail_vol if (tail_vol is not None and i >= n - tail) else daily_vol
        px *= math.exp(rnd.gauss(0.0, v))
        out.append(px)
    return out


# ----------------------------------------------------------- the math
def test_realized_vol_is_annualised_stdev_of_log_returns():
    flat = [100.0] * 30
    assert vr.realized_vol(flat, 14) == 0.0
    assert vr.realized_vol([100.0] * 10, 14) is None                 # too short
    up = [100.0 * (1.01 ** i) for i in range(30)]
    assert vr.realized_vol(up, 14) == pytest.approx(0.0, abs=1e-9)   # constant return = no dispersion


def test_hv_rank_is_the_position_of_todays_vol_inside_its_own_year():
    calm_now = vr.hv_rank(_closes(daily_vol=0.02, tail_vol=0.004), 14, 252)
    wild_now = vr.hv_rank(_closes(daily_vol=0.006, tail_vol=0.03), 14, 252)
    assert calm_now["available"] and wild_now["available"]
    assert calm_now["rank"] < 25 and wild_now["rank"] > 75
    assert 0 <= calm_now["percentile"] <= 100 and wild_now["percentile"] > calm_now["percentile"]
    assert calm_now["low"] <= calm_now["current"] <= calm_now["high"]
    short = vr.hv_rank(_closes(n=30), 14, 252)
    assert not short["available"]                                     # never a guessed rank


def test_neutral_allowed_reads_the_floor_and_abstains_without_history():
    ok, note = vr.neutral_allowed({"available": False}, 50.0)
    assert ok and "abstained" in note
    ok, note = vr.neutral_allowed({"available": True, "rank": 62.0}, 50.0)
    assert ok and "≥ 50" in note
    ok, note = vr.neutral_allowed({"available": True, "rank": 31.0, "current": 0.12,
                                   "low": 0.08, "high": 0.30}, 50.0)
    assert not ok and note.startswith("VOL_RANK_GATE: HV rank 31 < 50")


# ------------------------------------------------- the gate in the proposer
def _run(analysis, vix=13.0):
    return op.build_proposal("NIFTY 50", analysis=analysis, vix=vix, chain=make_chain(),
                             expiry="2026-11-26", book=dict(BIG_BOOK), prices={})


def test_low_vol_refuses_the_iron_condor_and_high_vol_builds_it():
    neutral = graded(fast_pct=0.4, slow_pct=-0.6)
    low = _run(dict(neutral, closes=_closes(daily_vol=0.02, tail_vol=0.004)))
    assert low["proposal"] is None and low["reason"].startswith("VOL_RANK_GATE")
    assert low["vol_rank"]["rank"] < 50 and "refused" in low["vol_rank"]["gate"]
    high = _run(dict(neutral, closes=_closes(daily_vol=0.006, tail_vol=0.03)))
    p = high["proposal"]
    assert p is not None and p["spread"]["strategy"] == "iron_condor"
    assert p["vol_rank"]["rank"] > 50 and "≥ 50" in p["vol_rank"]["gate"]


def test_directional_trades_are_never_vol_gated_but_carry_the_rank():
    bear = graded(fast_pct=-3.0, slow_pct=-6.0)
    r = _run(dict(bear, closes=_closes(daily_vol=0.02, tail_vol=0.004)), vix=12.0)
    p = r["proposal"]
    assert p["spread"]["strategy"] == "bear_put_spread" and p["vol_rank"]["rank"] < 50
    assert "gate" not in p["vol_rank"]


def test_no_close_history_is_an_abstention_not_a_block():
    r = _run(graded(fast_pct=0.4, slow_pct=-0.6))            # legacy analysis: no closes
    assert r["proposal"]["spread"]["strategy"] == "iron_condor"
    assert not r["proposal"]["vol_rank"]["available"] and "abstained" in r["proposal"]["vol_rank"]["gate"]


def test_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr("src.config.VOL_RANK_GATE_ENABLED", False)
    r = _run(dict(graded(fast_pct=0.4, slow_pct=-0.6), closes=_closes(daily_vol=0.02, tail_vol=0.004)))
    assert r["proposal"]["spread"]["strategy"] == "iron_condor"


# ------------------------------------------------------ correlation guard
def test_third_bear_put_across_the_book_is_refused_by_name():
    book = [_open_entry(ticker="NIFTY 50", short_id="aaaa0001"),
            _open_entry(ticker="NIFTY BANK", short_id="bbbb0002")]
    with _TempLedger():
        allowed, why = eg.gate_entry(_proposal(ticker="RELIANCE.NS"), entries=book,
                                     notify_fn=lambda *a, **k: None)
    assert not allowed and why.startswith("CORRELATION_GUARD_HIT: 2 open bearish")
    assert "aaaa0001" in why and "bbbb0002" in why and "max 2" in why


def test_second_bear_put_and_an_opposite_thesis_are_still_allowed():
    book = [_open_entry(ticker="NIFTY 50", short_id="aaaa0001")]
    with _TempLedger():
        assert eg.gate_entry(_proposal(ticker="RELIANCE.NS"), entries=book)[0]
        two = book + [_open_entry(ticker="NIFTY BANK", short_id="bbbb0002")]
        bull = _proposal(ticker="RELIANCE.NS", strategy="bull_call_spread",
                         direction="bullish", view="bullish")
        assert eg.gate_entry(bull, entries=two)[0]


def test_neutral_structures_are_not_correlation_capped():
    book = [_open_entry(ticker=t, strategy="iron_condor", direction="neutral", short_id=f"cc{i}00000"[:8])
            for i, t in enumerate(("NIFTY 50", "NIFTY BANK", "TCS.NS"))]
    with _TempLedger():
        ok, why = eg.gate_entry(_proposal(ticker="RELIANCE.NS", strategy="iron_condor",
                                          direction="neutral", view="neutral"), entries=book)
    assert ok and "CORRELATION" not in why


def test_per_underlying_rule_still_fires_first_and_the_cap_is_configurable(monkeypatch):
    book = [_open_entry(ticker="NIFTY 50", short_id="aaaa0001"),
            _open_entry(ticker="NIFTY BANK", short_id="bbbb0002")]
    with _TempLedger():
        ok, why = eg.gate_entry(_proposal(ticker="NIFTY 50"), entries=book,
                                notify_fn=lambda *a, **k: None)
        assert not ok and "max one per underlying+direction" in why
        monkeypatch.setattr("src.config.MAX_OPEN_PER_DIRECTION", 3)
        assert eg.gate_entry(_proposal(ticker="RELIANCE.NS"), entries=book)[0]

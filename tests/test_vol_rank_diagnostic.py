"""Decision #110: the realized-vol rank is a DIAGNOSTIC on every proposal —
the #109 entry gate on it and the #109 correlation guard are gone. Entries
are not filtered on volatility or on how many same-direction spreads the
book already holds."""
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


def test_realized_vol_and_hv_rank_math():
    assert vr.realized_vol([100.0] * 30, 14) == 0.0
    assert vr.realized_vol([100.0] * 10, 14) is None
    calm = vr.hv_rank(_closes(daily_vol=0.02, tail_vol=0.004), 14, 252)
    wild = vr.hv_rank(_closes(daily_vol=0.006, tail_vol=0.03), 14, 252)
    assert calm["available"] and wild["available"]
    assert calm["rank"] < 25 and wild["rank"] > 75 and wild["percentile"] > calm["percentile"]
    assert not vr.hv_rank(_closes(n=30), 14, 252)["available"]
    assert not hasattr(vr, "neutral_allowed")                          # the gate is gone


def _run(analysis, vix=13.0):
    return op.build_proposal("NIFTY 50", analysis=analysis, vix=vix, chain=make_chain(),
                             expiry="2026-11-26", book=dict(BIG_BOOK), prices={})


def test_low_vol_no_longer_refuses_the_iron_condor_but_the_rank_is_on_the_record():
    neutral = graded(fast_pct=0.4, slow_pct=-0.6)
    r = _run(dict(neutral, closes=_closes(daily_vol=0.02, tail_vol=0.004)))
    p = r["proposal"]
    assert p is not None and p["spread"]["strategy"] == "iron_condor"
    assert p["vol_rank"]["rank"] < 50 and "gate" not in p["vol_rank"]
    legacy = _run(neutral)["proposal"]
    assert legacy["spread"]["strategy"] == "iron_condor" and not legacy["vol_rank"]["available"]
    import src.config as cfg
    for name in ("VOL_RANK_GATE_ENABLED", "VOL_RANK_MIN_NEUTRAL", "MAX_OPEN_PER_DIRECTION"):
        assert not hasattr(cfg, name), name


def test_a_third_bear_put_across_the_book_is_allowed_again():
    book = [_open_entry(ticker="NIFTY 50", short_id="aaaa0001"),
            _open_entry(ticker="NIFTY BANK", short_id="bbbb0002")]
    with _TempLedger():
        ok, why = eg.gate_entry(_proposal(ticker="RELIANCE.NS"), entries=book)
        assert ok and why == "allowed"
        # the per-underlying rule (#68) is untouched
        ok, why = eg.gate_entry(_proposal(ticker="NIFTY 50"), entries=book,
                                notify_fn=lambda *a, **k: None)
        assert not ok and "max one per underlying+direction" in why
    assert "CORRELATION_GUARD" not in open(eg.__file__).read().split("was withdrawn")[-1]

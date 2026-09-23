"""Decision #104 — V1.2 SMART EXITS: thesis invalidation on the UNDERLYING
replaces the vetoed #103 premium-drawdown stop. One predicate, both EOD
resolvers and the live bridge; strictly forward-looking (Issue 31 ruling 1)."""
from datetime import date

import pytest

from src import live_bridge as lb, plan_tracker as pt


def _bull_call(entry_date="2026-09-24", expiry="2026-10-29"):
    # long 100 CE @ 10, short 110 CE @ 4 -> debit 6/share, max loss 6, max profit 4
    return {"short_id": "th000001", "ticker": "NIFTY 50", "date": entry_date,
            "decision": "approved", "outcome": None, "signal": "t",
            "spread": {"strategy": "bull_call_spread", "direction": "bullish",
                       "expiry": expiry, "lot_size": 10, "lots": 1,
                       "max_loss": 60.0, "max_profit": 40.0, "entry_spot": 100.0,
                       "legs": [{"side": "BUY", "option_type": "CE", "strike": 100.0, "premium": 10.0},
                                {"side": "SELL", "option_type": "CE", "strike": 110.0, "premium": 4.0}]}}


def _condor():
    return {"strategy": "iron_condor", "direction": "neutral", "entry_spot": 100.0,
            "legs": [{"side": "BUY", "option_type": "PE", "strike": 80.0},
                     {"side": "SELL", "option_type": "PE", "strike": 90.0},
                     {"side": "SELL", "option_type": "CE", "strike": 110.0},
                     {"side": "BUY", "option_type": "CE", "strike": 120.0}]}


def _bars(start: date, closes, rng=1.0):
    """Flat-range daily bars (day, low, high, close); ATR == rng."""
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d = date.fromordinal(d.toordinal() + 1)
        out.append((d.isoformat(), c - rng / 2, c + rng / 2, c))
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_structural_stop_directional_is_anchored_at_entry_not_trailed():
    s = _bull_call()["spread"]
    stop = pt.structural_stop(s, atr=2.0, atr_mult=2.0)
    assert stop == {"kind": "atr_from_entry", "level": 96.0, "direction": "bullish", "atr": 2.0}
    assert pt.thesis_invalidated(s, 95.9, 2.0, atr_mult=2.0)
    assert not pt.thesis_invalidated(s, 96.0, 2.0, atr_mult=2.0)
    bear = dict(s, strategy="bear_put_spread", direction="bearish")
    assert pt.structural_stop(bear, 2.0, 2.0)["level"] == 104.0
    assert pt.thesis_invalidated(bear, 104.1, 2.0, atr_mult=2.0)


def test_structural_stop_neutral_is_the_short_strikes():
    stop = pt.structural_stop(_condor(), atr=None)
    assert stop == {"kind": "short_strikes", "lower": 90.0, "upper": 110.0}
    assert pt.thesis_invalidated(_condor(), 89.9, None)
    assert pt.thesis_invalidated(_condor(), 110.5, None)
    assert not pt.thesis_invalidated(_condor(), 100.0, None)


def test_unmeasurable_means_hold_never_a_guess():
    s = _bull_call()["spread"]
    assert pt.structural_stop(s, atr=None) is None            # no ATR yet
    assert pt.structural_stop(s, atr=2.0, atr_mult=0.0) is None   # knob off
    assert pt.structural_stop(dict(s, entry_spot=None), atr=2.0) is None
    assert not pt.thesis_invalidated(s, 0.0, None)
    assert pt.structural_stop(dict(_condor(), legs=[]), None) is None


def test_forward_looking_guard_never_judges_a_bar_before_the_effective_date():
    s = _bull_call()["spread"]
    assert not pt.thesis_invalidated(s, 50.0, 2.0, day="2026-09-22", atr_mult=2.0)
    assert pt.thesis_invalidated(s, 50.0, 2.0, day="2026-09-23", atr_mult=2.0)


def test_resolver_exits_on_thesis_break_with_the_bars_date(monkeypatch):
    monkeypatch.setattr("src.config.THESIS_STOP_ATR_MULT", 2.0)
    monkeypatch.setattr("src.config.THESIS_STOP_ATR_N", 3)
    e = _bull_call(entry_date="2026-09-24")
    # 20 pre-entry bars (ATR measurable from bar one), then a slide through 96
    pre = _bars(date(2026, 8, 25), [100.0] * 20)
    post = _bars(date(2026, 9, 24), [100.0, 99.0, 97.0, 95.0, 90.0])
    hit = pt._resolve_spread(e, pre + post)
    assert hit is not None and hit[0] == "thesis_break"
    assert hit[3] == post[3][0]                       # the first close < 96
    assert hit[3] >= "2026-09-23"


def test_resolver_never_settles_retroactively_on_pre_effective_bars(monkeypatch):
    """Issue 31 replay: an old trade whose underlying broke the structure
    in August is NOT stopped out on that August bar; the first bar the rule
    may judge is the effective date."""
    monkeypatch.setattr("src.config.THESIS_STOP_ATR_MULT", 2.0)
    monkeypatch.setattr("src.config.THESIS_STOP_ATR_N", 3)
    e = _bull_call(entry_date="2026-08-03", expiry="2026-10-29")
    pre = _bars(date(2026, 7, 6), [100.0] * 20)
    aug = _bars(date(2026, 8, 3), [100.0, 90.0, 85.0, 85.0, 85.0])   # broken in Aug
    sept = _bars(date(2026, 9, 23), [85.0, 85.0])
    hit = pt._resolve_spread(e, pre + aug + sept)
    assert hit is not None and hit[0] == "thesis_break"
    assert hit[3] == "2026-09-23"                     # never an August date
    assert pt._resolve_spread(e, pre + aug) is None   # no eligible bar -> still open


def test_no_premium_stop_remains_anywhere():
    import src.config as cfg
    assert not hasattr(cfg, "OPTION_STOP_LOSS_FRACTION")
    assert not hasattr(pt, "spread_stop_hit")
    monkeypatch_free = _bull_call(entry_date="2026-09-24")
    pre = _bars(date(2026, 8, 25), [100.0] * 20, rng=50.0)   # huge ATR: stop far away
    # premium collapses (deep loss) but the underlying holds -> no exit
    post = _bars(date(2026, 9, 24), [100.0, 100.0, 100.0], rng=50.0)
    assert pt._resolve_spread(monkeypatch_free, pre + post) is None


def test_live_bridge_uses_the_same_predicate():
    e = _bull_call()
    assert lb.evaluate_position(e, 95.0, today=date(2026, 9, 25), atr=2.0)["signal"] == "thesis_break"
    assert lb.evaluate_position(e, 95.0, today=date(2026, 9, 25))["signal"] == "hold"   # no ATR: hold
    assert lb.evaluate_position(e, 95.0, today=date(2026, 9, 22), atr=2.0)["signal"] == "hold"


def test_outcome_rows_carry_a_wall_clock_settled_at_and_the_cards_key_on_it():
    from src import eod_summary
    assert eod_summary.settled_today({"outcome": {"exit_date": "2026-08-17",
                                                  "settled_at": "2026-09-23T13:06:31"}}, "2026-09-23")
    assert not eod_summary.settled_today({"outcome": {"exit_date": "2026-09-23",
                                                      "settled_at": "2026-09-24T09:00:00"}}, "2026-09-23")
    assert eod_summary.settled_today({"outcome": {"exit_date": "2026-09-23"}}, "2026-09-23")  # legacy row
    assert not eod_summary.settled_today({"outcome": None}, "2026-09-23")


def test_verdict_names_the_thesis_break():
    e = _bull_call()
    assert "THESIS BROKEN" in pt._spread_verdict(e, "thesis_break", -300.0, 0.0)

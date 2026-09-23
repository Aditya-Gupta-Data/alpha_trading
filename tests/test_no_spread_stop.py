"""Decision #105 (2026-09-23, architect mandate): a defined-risk options
spread has NO mid-trade stop — not a premium-drawdown stop (#103, reversed)
and not an underlying-break stop (#104, withdrawn). The structure is the
stop; the trade is held to the 65% profit take or the pre-expiry exit.
These tests fail the build if either ever comes back. They also pin the
Issue 31 reporting fix (settled_at) that stays."""
from datetime import date

from src import live_bridge as lb, plan_tracker as pt
import src.config as cfg


def _bull_call(entry_date="2026-09-24", expiry="2026-10-29"):
    return {"short_id": "ns000001", "ticker": "NIFTY 50", "date": entry_date,
            "decision": "approved", "outcome": None, "signal": "t",
            "spread": {"strategy": "bull_call_spread", "direction": "bullish",
                       "expiry": expiry, "lot_size": 10, "lots": 1,
                       "max_loss": 60.0, "max_profit": 40.0, "entry_spot": 100.0,
                       "legs": [{"side": "BUY", "option_type": "CE", "strike": 100.0, "premium": 10.0},
                                {"side": "SELL", "option_type": "CE", "strike": 110.0, "premium": 4.0}]}}


def _bars(start: date, closes, rng=1.0):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d = date.fromordinal(d.toordinal() + 1)
        out.append((d.isoformat(), c - rng / 2, c + rng / 2, c))
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_no_stop_predicate_or_knob_exists_anywhere():
    for name in ("OPTION_STOP_LOSS_FRACTION", "THESIS_STOP_ATR_MULT",
                 "THESIS_STOP_ATR_N", "THESIS_STOP_EFFECTIVE_DATE"):
        assert not hasattr(cfg, name), name
    for name in ("spread_stop_hit", "thesis_invalidated", "structural_stop",
                 "THESIS_ATR_LOOKBACK_DAYS"):
        assert not hasattr(pt, name), name


def test_a_deep_loss_is_held_to_the_pre_expiry_exit_not_stopped():
    """Premium collapses AND the underlying crashes through the long strike:
    the spread still rides to the pre-expiry exit at its capped max loss."""
    e = _bull_call()
    bars = _bars(date(2026, 9, 24), [100.0, 80.0, 60.0, 50.0, 50.0, 50.0, 50.0] + [50.0] * 30)
    hit = pt._resolve_spread(e, bars)
    assert hit is not None and hit[0] == "pre_expiry_exit"
    assert hit[3] >= "2026-10-26"                       # index: 2 days before expiry
    # the whole walk: no other resolution is ever possible on a loser
    assert {pt._resolve_spread(e, bars[:k])[0] for k in range(2, len(bars))
            if pt._resolve_spread(e, bars[:k])} <= {"pre_expiry_exit"}


def test_live_bridge_never_signals_a_stop():
    e = _bull_call()
    for spot in (50.0, 80.0, 95.0, 100.0):
        assert lb.evaluate_position(e, spot, today=date(2026, 9, 25))["signal"] == "hold"
    assert lb.evaluate_position(e, 50.0, today=date(2026, 10, 28))["signal"] == "pre_expiry_exit"


def test_outcome_rows_carry_settled_at_and_the_cards_key_on_it():
    from src import eod_summary
    assert eod_summary.settled_today({"outcome": {"exit_date": "2026-08-17",
                                                  "settled_at": "2026-09-23T13:06:31"}}, "2026-09-23")
    assert not eod_summary.settled_today({"outcome": {"exit_date": "2026-09-23",
                                                      "settled_at": "2026-09-24T09:00:00"}}, "2026-09-23")
    assert eod_summary.settled_today({"outcome": {"exit_date": "2026-09-23"}}, "2026-09-23")  # legacy row
    assert not eod_summary.settled_today({"outcome": None}, "2026-09-23")

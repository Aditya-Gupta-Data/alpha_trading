"""Decision #103 — the per-trade OPTIONS STOP: one predicate, both EOD
resolvers and the live bridge. fraction 0 = the pre-#103 resolver."""
import pytest

from src import live_bridge as lb, plan_tracker as pt


def _bull_call(entry_date="2026-09-01", expiry="2026-09-30", lots=1):
    # long 100 CE @ 10, short 110 CE @ 4 -> debit 6/share, max loss 6, max profit 4
    return {"short_id": "st000001", "ticker": "NIFTY 50", "date": entry_date,
            "decision": "approved", "outcome": None, "signal": "t",
            "spread": {"strategy": "bull_call_spread", "direction": "bullish",
                       "expiry": expiry, "lot_size": 10, "lots": lots,
                       "max_loss": 60.0, "max_profit": 40.0, "entry_spot": 100.0,
                       "legs": [{"side": "BUY", "option_type": "CE", "strike": 100.0, "premium": 10.0},
                                {"side": "SELL", "option_type": "CE", "strike": 110.0, "premium": 4.0}]}}


def test_predicate_is_the_one_rule():
    assert pt.spread_stop_hit(-3.0, 6.0, 0.5)
    assert not pt.spread_stop_hit(-2.9, 6.0, 0.5)
    assert not pt.spread_stop_hit(-6.0, 6.0, 0.0)          # off
    assert not pt.spread_stop_hit(-6.0, 0.0, 0.5)          # unmeasurable loss


def test_eod_resolver_cuts_the_loser_at_half_the_max_loss(monkeypatch):
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.5)
    e = _bull_call()
    # a slow bleed: spot falls to 70, time value decays -> mark falls below entry - 3
    # both legs OTM: the linear model bleeds 6 x (1 - frac_left) per share;
    # half the 6.0 max loss is reached once frac_left <= 0.5 -> 09-18 (12/29)
    bars = [("2026-09-01", 99, 101, 100), ("2026-09-05", 95, 99, 96),
            ("2026-09-10", 88, 92, 90), ("2026-09-15", 70, 75, 72),
            ("2026-09-18", 70, 75, 72), ("2026-09-28", 70, 75, 72)]
    hit = pt._resolve_spread(e, bars)
    assert hit[0] == "stop_loss" and hit[3] == "2026-09-18"
    assert hit[3] < "2026-09-28"                             # before the pre-expiry exit
    # the trailed resolver (no plan.trailing) is the same resolver
    assert pt._resolve_spread_trailed(e, bars)[0] == "stop_loss"


def test_fraction_zero_is_the_pre_103_resolver(monkeypatch):
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.0)
    e = _bull_call()
    bars = [("2026-09-01", 99, 101, 100), ("2026-09-15", 70, 75, 72), ("2026-09-28", 70, 75, 72)]
    hit = pt._resolve_spread(e, bars)
    assert hit[0] == "pre_expiry_exit" and hit[3] == "2026-09-28"


def test_profit_take_still_wins_over_the_stop(monkeypatch):
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.5)
    e = _bull_call()
    bars = [("2026-09-01", 99, 101, 100), ("2026-09-20", 118, 122, 120)]
    assert pt._resolve_spread(e, bars)[0] == "profit_take"


def test_live_bridge_fires_the_same_stop_signal(monkeypatch):
    from datetime import date
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.5)
    e = _bull_call()
    sig = lb.evaluate_position(e, spot=72.0, today=date(2026, 9, 18))
    assert sig["signal"] == "stop_loss" and sig["live_pnl_rs"] < 0
    assert lb.evaluate_position(e, spot=72.0, today=date(2026, 9, 15))["signal"] == "hold"
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.0)
    assert lb.evaluate_position(e, spot=72.0, today=date(2026, 9, 18))["signal"] == "hold"


def test_verdict_names_the_stop(monkeypatch):
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.5)
    e = _bull_call()
    assert pt._spread_verdict(e, "stop_loss", -31.0, -50.0).startswith("LOSS — stop hit at 50%")
    e["decision"] = "rejected"
    assert pt._spread_verdict(e, "stop_loss", -31.0, -50.0).startswith("GOOD SKIP")


def test_intraday_square_off_is_profit_take_only(monkeypatch):
    """The stop settles at the close through the tracker; the live loop
    advises (🛑) and never squares off on a stop signal (decision #69's
    square-off stays profit-take only)."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.5)
    calls, notes = [], []
    fired = lb.live_cycle(["NIFTY 50"], quote_fn=lambda u: {"current_price": 72.0},
                          entries=[_bull_call()], notify_fn=notes.append,
                          now_fn=lambda: datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
                          square_off_fn=lambda s: calls.append(s) or {"status": "squared_off"})
    assert [f["signal"] for f in fired] == ["stop_loss"]
    assert calls == [] and len(notes) == 1 and notes[0].startswith("🛑") and "stop loss" in notes[0]


def test_a_crash_bar_settles_as_a_stop_at_the_clamped_max_loss(monkeypatch):
    """test_options_spreads.test_loss_is_clamped_to_defined_risk_max keeps
    the pre-#103 resolver (fraction 0); with the stop on, the same crash
    resolves as `stop_loss` on the crash bar, gross still clamped."""
    monkeypatch.setattr("src.config.OPTION_STOP_LOSS_FRACTION", 0.5)
    from tests.test_options_spreads import CRASH_BARS, make_open_spread, run_spread_tracker
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, settled, _ = run_spread_tracker(tmp, [make_open_spread()], CRASH_BARS)
    assert resolved == 1
    o = fj.rewritten[0]["outcome"]
    assert o["resolution"] == "stop_loss"
    assert round(o["pnl_rs"] + o["frictions_rs"] + o["slippage_rs"], 2) == -6225.0
    assert o["verdict"].startswith("LOSS — stop hit at 50%")

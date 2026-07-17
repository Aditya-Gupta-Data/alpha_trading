"""next_gen_engine/ (Stage 4/5 staging) — hermetic unit tests.

Pure logic only: no network, no files, no clocks beyond injected dates.
These modules are NOT wired into the live engine; the tests pin their
contracts down so the later canonical merge is mechanical.
"""
import pytest

from next_gen_engine import execution_algo as ex
from next_gen_engine import portfolio_risk_manager as prm
from next_gen_engine import trailing_stops as ts
from next_gen_engine import wealth_flywheel as wf


# ----------------------------------------------------- daily circuit breaker

def test_breaker_trips_at_the_daily_loss_limit():
    v = prm.check_daily_breaker(1_000_000, pnl_today=-30_000)  # exactly 3%
    assert v["halted"] is True
    assert v["daily_loss_pct"] == 3.0
    assert "TRIPPED" in v["reason"]


def test_breaker_stays_open_within_budget_and_on_profit():
    assert prm.check_daily_breaker(1_000_000, -29_999)["halted"] is False
    ok = prm.check_daily_breaker(1_000_000, +50_000)
    assert ok["halted"] is False and ok["daily_loss_pct"] == 0.0


def test_breaker_abstains_without_equity_but_says_so():
    v = prm.check_daily_breaker(0, -50_000)
    assert v["halted"] is False and "abstains" in v["error"]


def test_realized_pnl_counts_only_today():
    rows = [
        {"resolved_at": "2026-07-17T10:00:00+05:30", "pnl_net": -1000.0},
        {"closed_at": "2026-07-17T11:00:00+05:30", "pnl": -500.0},
        {"resolved_at": "2026-07-16T14:00:00+05:30", "pnl_net": -9999.0},
        {"resolved_at": "2026-07-17T12:00:00+05:30"},          # no pnl: skip
        {"pnl_net": -400.0},                                   # no stamp: skip
    ]
    assert prm.realized_pnl_today(rows, today="2026-07-17") == -1500.0
    v = prm.gate_entry(100_000, rows, today="2026-07-17",
                       max_daily_loss_pct=1.0)
    assert v["halted"] is True                                 # 1.5% >= 1%


# ------------------------------------------------------------ wealth flywheel

def test_sweep_order_sizes_whole_units_and_reports_residual():
    r = wf.build_sweep_order(pnl_net=10_000, etf_price=62.0)
    assert r["earmark_rs"] == 5000.0
    assert r["order"]["qty"] == 80                    # floor(5000/62)
    assert r["order"]["notional_rs"] == 4960.0
    assert r["cash_residual_rs"] == 40.0
    assert r["order"]["mode"] == "PAPER" and r["mode"] == "PAPER"
    assert r["order"]["symbol"] == "GOLDBEES"


def test_no_sweep_on_losses_and_deferred_sizing_without_a_quote():
    assert wf.build_sweep_order(-5_000, 62.0) is None
    assert wf.build_sweep_order(0, 62.0) is None
    r = wf.build_sweep_order(10_000, etf_price=None)
    assert r["order"] is None and r["cash_residual_rs"] == 5000.0
    tiny = wf.build_sweep_order(100, etf_price=62.0)   # earmark 50 < 1 unit
    assert tiny["order"] is None and "below one unit" in tiny["note"]


# ------------------------------------------------------------- trailing stops

def _bars(closes, spread=2.0):
    return [{"high": c + spread / 2, "low": c - spread / 2, "close": c}
            for c in closes]


def test_atr_abstains_on_short_history_and_computes_on_enough():
    assert ts.atr(_bars([100, 101]), period=14) is None
    bars = _bars([100 + i * 0.5 for i in range(20)])
    a = ts.atr(bars, period=14)
    assert a is not None and a > 0


def test_trailing_stop_ratchets_and_never_widens():
    rising = _bars([100 + i for i in range(20)])
    first = ts.update_trailing_stop(None, rising, side="long")
    assert first["stop"] is not None
    # market falls back: raw level drops, ratchet must hold the old stop
    fallen = rising + _bars([110, 105, 102])
    held = ts.update_trailing_stop(first["stop"], fallen, side="long")
    assert held["stop"] >= first["stop"]
    # data gap: uncomputable level retains the previous stop
    kept = ts.update_trailing_stop(77.7, _bars([100, 101]), side="long")
    assert kept["stop"] == 77.7 and "retained" in kept["note"]


def test_short_side_trails_downward_and_hit_check_abstains():
    falling = _bars([100 - i for i in range(20)])
    s = ts.update_trailing_stop(None, falling, side="short")
    assert s["stop"] > falling[-1]["close"]           # stop sits above price
    assert ts.stop_hit(s["stop"], falling[-1]["close"], "short") is False
    assert ts.stop_hit(None, 100.0, "long") is None
    assert ts.stop_hit(95.0, 94.0, "long") is True


# -------------------------------------------------------------- limit chasing

def test_chase_plan_walks_mid_to_touch_on_tick():
    p = ex.plan_limit_chase("buy", top_bid=100.0, top_ask=101.0,
                            window_s=30, steps=6)
    prices = [r["limit_price"] for r in p["rungs"]]
    assert prices[0] == 100.5                          # mid
    assert prices[-1] == 101.0                         # touch
    assert prices == sorted(prices)                    # monotonic walk
    assert all(round(x / 0.05, 6) == round(x / 0.05) for x in prices)
    assert p["rungs"][-1]["t_offset_s"] == 30.0
    sell = ex.plan_limit_chase("sell", 100.0, 101.0)
    sp = [r["limit_price"] for r in sell["rungs"]]
    assert sp[0] == 100.5 and sp[-1] == 100.0          # walks down to bid


def test_chase_refuses_garbage_books():
    assert "error" in ex.plan_limit_chase("buy", 0, 101.0)
    assert "error" in ex.plan_limit_chase("buy", 102.0, 101.0)  # crossed
    assert "error" in ex.plan_limit_chase("hold", 100.0, 101.0)


def test_chase_fill_takes_price_improvement_when_market_comes_in():
    p = ex.plan_limit_chase("buy", 100.0, 101.0, steps=4)
    # ask drops to 100.55 by rung 1: our 100.65 rung crosses -> fill 100.55
    quotes = [{"top_bid": 100.0, "top_ask": 101.0},
              {"top_bid": 100.0, "top_ask": 100.55}]
    f = ex.simulate_chase_fill(p, quotes)
    assert f["filled"] is True and f["fill_price"] == 100.55
    assert f["improvement_vs_touch"] == 0.45
    assert f["fill_basis"] == "chase"


def test_chase_reports_honest_miss_never_a_phantom_fill():
    p = ex.plan_limit_chase("buy", 100.0, 101.0, steps=3)
    runaway = [{"top_bid": 101.5, "top_ask": 102.5}] * 3   # market ran away
    f = ex.simulate_chase_fill(p, runaway)
    assert f["filled"] is False
    assert f["cross_now_price"] == 102.5               # the true cost now
    assert "never auto-filled at mid" in f["note"]


def test_protective_legs_execute_first_and_stably():
    legs = [{"side": "sell", "strike": 25000}, {"side": "buy", "strike": 25200},
            {"side": "sell", "strike": 24800}, {"side": "buy", "strike": 24600}]
    ordered = ex.sequence_spread_legs(legs)
    assert [l["side"] for l in ordered] == ["buy", "buy", "sell", "sell"]
    assert [l["strike"] for l in ordered] == [25200, 24600, 25000, 24800]

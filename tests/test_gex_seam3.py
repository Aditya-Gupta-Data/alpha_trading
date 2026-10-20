"""Decision #155 (GEX seam 3): the NSE INR-crore GEX figure, D4 gamma zones and D6 routing in trade_planner,
and the D7 second-order Taylor Greek P&L attribution. Offline."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import brain_map, lake, trade_planner as tp
from src.analysis import gamma_profile as gp
from src.analysis import greek_attribution as ga

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


# ------------------------------------------------------------- INR crore

def test_crore_figure_counts_the_lot_once():
    gex_1pct = 0.002 * 1_000_000 * 23_000 ** 2 * 0.01          # gamma x OI(quantity) x S^2 x 1%
    assert gp.to_crore(gex_1pct, 65, "quantity") == pytest.approx(23_000 ** 2 * 0.002 * 1_000_000 / 1e7)
    contracts = 1_000_000 / 65
    assert gp.to_crore(0.002 * contracts * 23_000 ** 2 * 0.01, 65, "contracts") == pytest.approx(
        23_000 ** 2 * 0.002 * contracts * 65 / 1e7)            # the Architect's S^2 x G x OI x L / 10^7
    assert gp.to_crore(gex_1pct, None, "contracts") is None and gp.to_crore(None) is None


def test_profiles_carry_the_crore_totals():
    oc = {f"{k:.6f}": {"ce": {"oi": 100_000, "greeks": {"gamma": 0.001}, "implied_volatility": 15},
                       "pe": {"oi": 50_000, "greeks": {"gamma": 0.001}, "implied_volatility": 15}}
          for k in range(22_500, 23_550, 50)}
    p = gp.profile(gp.strike_gex(oc, 23_000.0), 23_000.0, 5.0, lot_size=65)
    assert p["net_gex_cr"] == pytest.approx(p["net_gex"] / 1e5, rel=1e-6) and p["lot_size"] == 65
    assert p["abs_gex_cr"] == pytest.approx(p["abs_gex"] / 1e5, rel=1e-6) and p["oi_unit"] == "quantity"


# ---------------------------------------------------------------- D4 zones

@pytest.mark.parametrize("spot, regime, zone", [
    (100.4, "positive", "transition"), (99.6, "negative", "transition"),
    (100.6, "positive", "long_gamma"), (99.4, "negative", "short_gamma"),
    (101.0, "negative", "short_gamma"),          # the sign at spot decides, not the side of S* (#154 curve)
])
def test_gamma_zone_keys_on_the_half_percent_band(spot, regime, zone):
    z = tp.gamma_zone(spot, 100.0, regime)
    assert z["zone"] == zone and z["distance_pct"] == pytest.approx((spot / 100 - 1) * 100, abs=1e-3)


def test_gamma_zone_without_a_flip_or_a_read():
    assert tp.gamma_zone(100.0, None, "negative")["zone"] == "short_gamma"
    assert tp.gamma_zone(100.0, 100.0, None)["zone"] is None


# --------------------------------------------------------------- D6 routing

STATE = {"spot": 23_000.0, "underlying": "NIFTY 50", "trend": "neutral", "iv_regime": "high", "vix": 14.5}


def _short(momentum):
    return dict(STATE, momentum_pct=momentum, gex={"zero_gamma": 23_400.0, "regime_at_spot": "negative"})


def test_short_gamma_overrides_the_sma_matrix_with_momentum_or_long_vol():
    assert tp.map_technical_to_strategy(STATE)["strategy"] == "iron_condor"      # the matrix alone
    up = tp.map_technical_to_strategy(_short(0.8))
    assert up["strategy"] == "bull_call_spread" and up["view"] == "momentum_bullish"
    assert "SMA read overridden (D6)" in up["rationale"] and up["gamma_zone"]["zone"] == "short_gamma"
    down = tp.map_technical_to_strategy(_short(-0.8))
    assert down["strategy"] == "bear_put_spread" and down["legs"][0]["option_type"] == "PE"
    flat = tp.map_technical_to_strategy(_short(0.2))
    assert flat["strategy"] == "long_straddle" and flat["view"] == "long_volatility"
    assert [l["side"] for l in flat["legs"]] == ["BUY", "BUY"] and flat["max_profit"] is None
    assert flat["max_loss"] == pytest.approx(flat["net_debit"] * flat["lot_size"])
    assert tp.map_technical_to_strategy(_short(None))["strategy"] == "long_straddle"


def test_long_gamma_and_transition_keep_the_matrix():
    long_g = tp.map_technical_to_strategy(dict(STATE, gex={"zero_gamma": 22_800.0, "regime_at_spot": "positive"}))
    assert long_g["strategy"] == "iron_condor" and long_g["gamma_zone"]["zone"] == "long_gamma"
    tr = tp.map_technical_to_strategy(dict(STATE, momentum_pct=-1.0,
                                           gex={"zero_gamma": 23_050.0, "regime_at_spot": "negative"}))
    assert tr["strategy"] == "iron_condor" and tr["gamma_zone"]["zone"] == "transition"


def test_the_earnings_gate_still_rules_after_the_gex_route():
    gated = tp.map_technical_to_strategy(dict(_short(-0.8), days_to_results=3))
    assert gated["strategy"] == "no_trade" and "earnings proximity gate" in gated["rationale"]
    assert gated["gamma_zone"]["zone"] == "short_gamma"
    assert tp.map_technical_to_strategy(dict(_short(0.1), days_to_results=3))["strategy"] == "long_straddle"


def test_a_short_gamma_zone_refuses_mean_reversion_even_if_a_route_produced_one(monkeypatch):
    monkeypatch.setattr(tp, "_short_gamma_plan", lambda base, state, zone: tp._matrix_plan(STATE))
    plan = tp.map_technical_to_strategy(_short(0.0))
    assert plan["strategy"] == "no_trade" and "mean reversion, refused (D6" in plan["rationale"]


# ---------------------------------------------------------- D7 attribution

def _row(spot, iv, days_left, when, strikes=range(90, 112, 1)):
    oc = {}
    for k in strikes:
        node = {}
        for ot, key in (("CE", "ce"), ("PE", "pe")):
            px = ga.bs_price(spot, k, iv, days_left / 365.0, ot)
            node[key] = {"implied_volatility": iv, "last_price": px, "top_bid_price": px - 0.01,
                         "top_ask_price": px + 0.01, "oi": 1000, "greeks": {"gamma": 0}}
        oc[f"{float(k):.6f}"] = node
    return {"spot": spot, "expiry": "2026-11-26", "captured_at": when.isoformat(), "oc": oc}


def _days(path):
    base = datetime(2026, 11, 2, 15, 40, tzinfo=IST)
    rows = []
    for i, (spot, iv) in enumerate(path):
        when = base + timedelta(days=i)
        left = (datetime(2026, 11, 26, 15, 30, tzinfo=IST) - when).total_seconds() / 86400
        rows.append((when.date().isoformat(), _row(spot, iv, left, when)))
    return rows


SPREAD = {"strategy": "bull_call_spread", "expiry": "2026-11-26", "lot_size": 50, "lots": 2,
          "legs": [{"side": "BUY", "option_type": "CE", "strike": 100.0},
                   {"side": "SELL", "option_type": "CE", "strike": 105.0}]}


def test_the_taylor_expansion_explains_a_bs_priced_path():
    res = ga.attribute(SPREAD, _days([(100.0, 20.0), (101.0, 20.5), (100.5, 19.8), (102.0, 20.2)]))
    assert res["covered"] == 3 and res["coverage"] == 1.0 and res["reason"] is None
    assert set(res["components"]) == set(ga.COMPONENTS)
    assert abs(res["residual"]) < 0.05 * abs(res["actual_window_pnl"])          # second order holds
    assert res["components"]["delta"] > 0 and res["components"]["theta"] != 0


def test_a_quiet_day_is_theta():
    res = ga.attribute(SPREAD, _days([(100.0, 20.0), (100.0, 20.0)]))
    c = res["components"]
    assert c["delta"] == 0 and c["vega"] == 0
    assert c["theta"] == pytest.approx(res["actual_window_pnl"], rel=0.05)


def test_the_d7_floor_skips_a_step_missing_a_primary_input():
    days = _days([(100.0, 20.0), (101.0, 20.0)])
    days[1][1]["oc"]["105.000000"]["ce"]["implied_volatility"] = 0
    days[1][1]["oc"]["105.000000"]["pe"]["implied_volatility"] = 0
    res = ga.attribute(SPREAD, days)
    assert res["covered"] == 0 and "D7 floor" in res["reason"] and res["components"] is None
    assert "fewer than two" in ga.attribute(SPREAD, days[:1])["reason"]


def test_settlement_records_the_attribution_once_and_never_touches_outcomes(monkeypatch):
    from src import analyst, plan_tracker
    root = ga.LAKE_ROOT                                   # conftest points it at tmp
    for day, row in _days([(100.0, 20.0), (101.0, 20.5)]):
        lake.write_partition("chains/nifty", day, [dict(row, underlying="NIFTY 50")], root=root)
    entry = {"short_id": "ga000001", "date": "2026-11-02", "ticker": "NIFTY 50", "action": "SPREAD",
             "signal": "bull call", "decision": "approved", "spread": SPREAD, "price": 2.0, "shares": 100,
             "why": "test", "view": "bullish", "pattern_tags": ["bull_call_spread"],
             "outcome": {"resolution": "target", "exit_date": "2026-11-03", "r_multiple": 0.4, "pnl_rs": 500.0,
                         "price": 1.0, "pct": 1.0, "days_in_trade": 1, "hypothetical": False, "verdict": "WIN"}}
    monkeypatch.setattr(analyst, "generate_post_mortem", lambda p, e: None)
    conn = brain_map.connect(":memory:")
    plan_tracker.record_post_mortem(entry, conn)
    row = conn.execute("SELECT coverage, components, trade_pnl, outside_window FROM greek_attribution").fetchone()
    assert row[0] == 1.0 and set(json.loads(row[1])) == set(ga.COMPONENTS) and row[2] == 500.0
    assert row[3] is not None
    assert not ga.record(conn, entry, {"coverage": 0.0})                      # first write wins
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1


# ------------------------------------------------------------- dry run

def test_the_dry_run_builds_a_plan_from_stored_gex_rows(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("dry", ROOT / "scripts" / "gex_planner_dry_run.py")
    dry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dry)
    def gex_row(day, spot):
        return {"date": day, "spot": spot, "underlying": "NIFTY 50", "vix": 14.0,
                "combined": {"repriced": {"zero_gamma": 23_600.0, "regime_at_spot": "positive"},
                             "calibrated": {"non_client": {"zero_gamma": 23_600.0, "regime_at_spot": "negative",
                                                           "net_gex_cr": -12.0}}}}
    lake.write_partition("gex/nifty", "2026-10-15", [gex_row("2026-10-15", 23_000.0)], root=tmp_path)
    lake.write_partition("gex/nifty", "2026-10-16", [gex_row("2026-10-16", 23_200.0)], root=tmp_path)
    st = dry.state_for("nifty", root=tmp_path)
    assert st["momentum_pct"] == pytest.approx(0.87, abs=0.01) and st["gex"]["regime_at_spot"] == "negative"
    assert dry.main(["--slug", "nifty", "--root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan"]["strategy"] == "bull_call_spread" and out["input"]["gex_basis"] == "calibrated (non_client)"
    assert dry.main(["--slug", "banknifty", "--root", str(tmp_path)]) == 1

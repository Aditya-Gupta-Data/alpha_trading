"""
Glassbreaking Profits (Phase 4 / M4A, 2026-09-11, decision #96) — SHADOW.
Falling-knife + early-breakout primitives, the strict defined-risk router,
the pyramid/trail plan every setup carries, the shadow ledger, and the
Proving Court enrolment (registry + trial, Wilson lower bound).

Fully offline: injected bars, injected F&O map, in-memory brain_map,
tmp_path ledger. Run:
    python -m pytest tests/test_glassbreaking.py -q
"""

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.strategies import glassbreaking as gb
from src.validation import registry as rg, trial

ROOT = Path(__file__).resolve().parent.parent
FO = {"as_of": "2026-09-11", "banned": ["BANNEDCO"],
      "symbols": {"RELIANCE": {"tier": "tier1"}, "SMALLCO": {"tier": "tier2"},
                  "BANNEDCO": {"tier": "tier1"}}}


def _bar(day, close, vol=1_000_000.0, high=None, low=None):
    return {"date": day, "open": close, "high": high or close + 1.0,
            "low": low or close - 1.0, "close": close, "volume": vol}


def _falling_series(n=40, start=1000.0, drop_per_day=6.0):
    """A knife: 40 sessions sliding from 1000 down ~24% with a volume
    climax on the way down (the anchor)."""
    bars = []
    for i in range(n):
        c = start - drop_per_day * i
        v = 5_000_000.0 if i == 20 else 1_000_000.0
        bars.append(_bar(f"2026-08-{(i % 28) + 1:02d}" if i < 28 else f"2026-09-{i - 27:02d}",
                         c, v))
    return bars


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    rg.ensure_schema(conn)
    trial.ensure_schema(conn)
    return conn


# ============================================================ primitives

def test_falling_knife_fires_on_rsi_oversold_only_after_a_real_drop():
    sig = gb.falling_knife_signal(_falling_series())
    assert sig is not None
    assert sig["primitive"] == "falling_knife" and sig["direction"] == "bullish"
    assert "rsi_oversold" in sig["triggers"] and sig["rsi"] <= gb.RSI_OVERSOLD
    assert sig["drawdown_pct"] >= gb.KNIFE_MIN_DRAWDOWN_PCT
    assert sig["anchor_date"] is not None and sig["anchored_vwap"] is not None


def test_falling_knife_is_none_when_the_name_has_not_fallen():
    flat = [_bar(f"2026-08-{i + 1:02d}", 1000.0 + (i % 3)) for i in range(30)]
    assert gb.falling_knife_signal(flat) is None


def test_falling_knife_avwap_support_trigger_without_rsi():
    bars = _falling_series()
    # push the last close back up to sit just above the anchored VWAP so RSI
    # recovers above 30 but price is resting on the anchor
    av = gb.anchored_vwap(bars, gb.anchor_index(bars))
    for k in range(1, 8):
        bars[-k]["close"] = av * (1 + 0.004 * k)
        bars[-k]["high"] = bars[-k]["close"] + 1
        bars[-k]["low"] = bars[-k]["close"] - 1
    # the rewritten bars move the VWAP a little; converge the last close
    # onto "half a percent above the anchor" against the recomputed value
    for _ in range(3):
        av = gb.anchored_vwap(bars, gb.anchor_index(bars))
        bars[-1]["close"] = av * 1.005
        bars[-1]["high"], bars[-1]["low"] = bars[-1]["close"] + 1, bars[-1]["close"] - 1
    sig = gb.falling_knife_signal(bars)
    assert sig is not None and "avwap_support" in sig["triggers"]
    assert sig["rsi"] is None or sig["rsi"] > gb.RSI_OVERSOLD or "rsi_oversold" in sig["triggers"]


def test_anchored_vwap_abstains_without_volume():
    bars = _falling_series()
    for b in bars[25:]:
        b.pop("volume")
    assert gb.anchored_vwap(bars, 20) is None
    assert gb.anchor_index([{"date": "d", "close": 1.0}]) is None


def test_early_breakout_needs_gap_and_institutional_volume_and_skips_sma():
    hist = [_bar(f"2026-08-{i + 1:02d}", 500.0, 1_000_000.0) for i in range(25)]
    today = {"date": "2026-08-26", "open": 515.0, "volume": 2_500_000.0}
    sig = gb.early_breakout_signal(hist, today)
    assert sig is not None and sig["primitive"] == "early_breakout"
    assert sig["gap_pct"] == pytest.approx(3.0) and "volume_spike" in sig["triggers"]
    assert sig["sma_bypassed"] is True
    # gap without volume: no
    assert gb.early_breakout_signal(hist, {"date": "d", "open": 515.0, "volume": 900_000.0}) is None
    # volume without gap: no
    assert gb.early_breakout_signal(hist, {"date": "d", "open": 504.0, "volume": 9_000_000.0}) is None
    # a bulk/block print stands in for the volume spike; premarket price stands in for open
    d = gb.early_breakout_signal(hist, {"date": "d", "premarket_price": 512.0,
                                        "bulk_block_deal": True})
    assert d is not None and "bulk_block_deal" in d["triggers"]


# ============================================================ routing + risk

def test_router_maps_both_primitives_to_a_debit_spread_and_nothing_else():
    assert gb.route_structure("falling_knife") == ("bull_call_spread", "ok")
    assert gb.route_structure("early_breakout") == ("bull_call_spread", "ok")
    s, why = gb.route_structure("naked_put_write")
    assert s is None and why.startswith("no_route")
    assert set(gb.STRUCTURES.values()) <= gb.DEFINED_RISK_STRUCTURES


def test_assert_defined_risk_rejects_naked_and_unhedged_shorts():
    ok, _ = gb.assert_defined_risk({"strategy": "bull_call_spread", "max_loss": 100.0,
                                    "legs": [{"side": "BUY", "option_type": "CE"},
                                             {"side": "SELL", "option_type": "CE"}]})
    assert ok
    bad = [
        {"strategy": "naked_put", "max_loss": 1e9, "legs": [{"side": "SELL", "option_type": "PE"}]},
        {"strategy": "bull_call_spread", "max_loss": 100.0, "legs": [{"side": "SELL", "option_type": "CE"}]},
        {"strategy": "bull_call_spread", "max_loss": 100.0,
         "legs": [{"side": "BUY", "option_type": "CE"}, {"side": "SELL", "option_type": "PE"}]},
        {"strategy": "bull_call_spread", "max_loss": float("inf"),
         "legs": [{"side": "BUY", "option_type": "CE"}, {"side": "SELL", "option_type": "CE"}]},
        None,
    ]
    for spread in bad:
        ok, why = gb.assert_defined_risk(spread)
        assert not ok and why.startswith("naked_forbidden")


def _sig(day="2026-09-10", spot=1000.0):
    return {"primitive": "falling_knife", "direction": "bullish", "date": day,
            "spot": spot, "triggers": ["rsi_oversold"]}


def test_build_setup_accepts_a_tier1_name_with_a_pyramid_and_trail_plan():
    s = gb.build_setup(_sig(), "RELIANCE", 1000.0, 1050.0, 30.0, 12.0, 250,
                       "2026-09-30", pool_rupees=2_000_000.0, fo=FO,
                       halt_fn=lambda sym: (True, None))
    assert s["accepted"] and s["structure"] == "bull_call_spread"
    assert s["mode"] == "shadow" and s["halt_check"] == "passed"
    assert s["spread"]["strategy"] == "bull_call_spread" and s["spread"]["lots"] >= 1
    assert s["risk_rupees"] <= 2_000_000.0 * gb.RISK_PCT_PER_SETUP
    ladder = s["plan"]["tranches"]
    assert [lv["at_r"] for lv in ladder["levels"]] == [1.0, 2.0]
    assert ladder["max_lots"] == s["lots"] * gb.PYRAMID_MAX_MULT
    assert s["plan"]["trailing"] == {"atr_mult": 2.0, "atr_n": 14, "on": "underlying"}
    assert s["time_exit_on"] > s["date"]


def test_build_setup_rejections_are_named():
    base = dict(buy_strike=1000.0, sell_strike=1050.0, buy_premium=30.0,
                sell_premium=12.0, lot_size=250, expiry="2026-09-30",
                pool_rupees=2_000_000.0, fo=FO)
    assert gb.build_setup(_sig(), "SMALLCO", **base)["reason"] == "no_listed_options"
    assert gb.build_setup(_sig(), "BANNEDCO", **base)["reason"] == "fo_banned"
    assert gb.build_setup(_sig(), "NOTFO", **base)["reason"] == "not_in_fo"
    r = gb.build_setup(_sig(), "RELIANCE", halt_fn=lambda s: (False, "CIRP filed"), **base)
    assert r["reason"].startswith("corporate_risk_halt")
    r = gb.build_setup(_sig(), "RELIANCE", halt_fn=lambda s: 1 / 0, **base)
    assert r["reason"].startswith("corporate_risk_halt: halt_check_error")   # fails CLOSED
    r = gb.build_setup(_sig(), "RELIANCE", **dict(base, pool_rupees=10_000.0))
    assert r["reason"].startswith("risk_cap")
    r = gb.build_setup(dict(_sig(), primitive="mystery"), "RELIANCE", **base)
    assert r["reason"].startswith("no_route")
    r = gb.build_setup(_sig(), "RELIANCE", **dict(base, sell_strike=900.0))
    assert not r["accepted"]                                     # incoherent spread
    idx = gb.build_setup(_sig(), "NIFTY 50", is_index=True,
                         **dict(base, lot_size=65, buy_strike=24000.0, sell_strike=24200.0,
                                buy_premium=140.0, sell_premium=70.0))
    assert idx["accepted"] and idx["halt_check"] == "not_run"


# ============================================================ run + ledger + court

def _chain():
    return {"buy_strike": 1000.0, "sell_strike": 1050.0, "buy_premium": 30.0,
            "sell_premium": 12.0, "lot_size": 250, "expiry": "2026-09-30"}


def test_run_writes_every_verdict_to_the_ledger_and_enrols_accepted_fires(tmp_path):
    ledger = tmp_path / "gb.jsonl"
    conn = _mem_conn()
    hist = [_bar(f"2026-08-{i + 1:02d}", 500.0) for i in range(25)]
    out = gb.run("2026-09-10", [
        {"symbol": "RELIANCE", "bars": _falling_series(), "chain": _chain()},
        {"symbol": "SMALLCO", "bars": _falling_series(), "chain": _chain()},
        {"symbol": "RELIANCE", "bars": hist,
         "today": {"date": "2026-09-10", "open": 515.0, "volume": 3_000_000.0},
         "chain": _chain()},
        {"symbol": "RELIANCE", "bars": [_bar("2026-09-09", 500.0)] * 30, "chain": _chain()},
        "garbage",
    ], pool_rupees=2_000_000.0, conn=conn, ledger_path=ledger, fo=FO)
    rows = gb.read_ledger(ledger)
    assert len(rows) == 4                                   # 3 verdicts + 1 error row
    accepted = [r for r in rows if r.get("accepted")]
    assert {r["primitive"] for r in accepted} == {"falling_knife", "early_breakout"}
    assert all(r["ref"].startswith("shadow:") for r in accepted)
    assert any(r["reason"] == "no_listed_options" for r in rows)
    assert any(str(r["reason"]).startswith("error:") for r in rows)
    assert all(r["mode"] == "shadow" for r in rows)
    # the court holds one TRIAL row per primitive and one fire per accepted setup
    for p in ("falling_knife", "early_breakout"):
        pid = rg.pattern_id_for(gb.court_definition(p))
        assert rg.get(conn, pid)["status"] == "TRIAL"
    n = conn.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
    assert n == len(accepted) == 2


def test_grade_resolves_with_the_tracker_math_and_reports_to_the_court(tmp_path):
    ledger = tmp_path / "gb.jsonl"
    conn = _mem_conn()
    setups = gb.run("2026-09-01", [{"symbol": "RELIANCE", "bars": _falling_series(),
                                     "chain": _chain()}],
                    pool_rupees=2_000_000.0, conn=conn, ledger_path=ledger, fo=FO)
    assert setups and setups[0]["accepted"]
    setups[0]["date"] = "2026-09-01"
    # the underlying rips: the bull call spread reaches its 65% profit-take
    bars = [(f"2026-09-{d:02d}", 1000.0 + 10 * d, 1020.0 + 10 * d, 1010.0 + 10 * d)
            for d in range(2, 20)]
    out = gb.grade(setups, lambda sym, start: bars, today=date(2026, 9, 20),
                   conn=conn, ledger_path=ledger)
    assert len(out) == 1
    o = out[0]["outcome"]
    assert o["resolution"] in ("profit_take", "trail_hit") and o["r_multiple"] > 0
    assert o["hypothetical"] is True and "tranches" in o and o["tranches"]["booked"] is False
    assert out[0]["court_resolved"] is True
    card = gb.court_scorecard(conn, "falling_knife")
    assert card["n"] == 1 and card["wins"] == 1 and card["promote"] is False
    assert card["reason"].startswith("insufficient n") and 0 < card["wilson_lb"] < 1
    # grading twice never double-resolves: the setup now carries its outcome
    assert setups[0]["outcome"] is o
    assert gb.grade(setups, lambda s, st: bars, today=date(2026, 9, 20), conn=conn,
                    ledger_path=ledger) == []


def test_grade_falls_back_to_the_expiry_backstop_and_time_exit(tmp_path):
    ledger = tmp_path / "gb.jsonl"
    s = gb.build_setup(_sig(day="2026-09-01"), "RELIANCE", 1000.0, 1050.0, 30.0, 12.0, 250,
                       "2026-09-30", pool_rupees=2_000_000.0, fo=FO)
    # no bars at all, long past expiry: settles conservatively, r = -1
    out = gb.grade([s], lambda sym, start: [], today=date(2026, 10, 15), ledger_path=ledger)
    assert out[0]["outcome"]["resolution"] == "expiry_backstop"
    assert out[0]["outcome"]["r_multiple"] == pytest.approx(-1.0)
    # flat bars past the time exit: time_stop on the first bar on/after it
    s2 = gb.build_setup(_sig(day="2026-09-01"), "RELIANCE", 1000.0, 1050.0, 30.0, 12.0, 250,
                        "2026-10-30", pool_rupees=2_000_000.0, fo=FO)
    flat = [(f"2026-09-{d:02d}", 999.0, 1001.0, 1000.0) for d in range(2, 30)]
    out = gb.grade([s2], lambda sym, start: flat, today=date(2026, 9, 29), ledger_path=ledger)
    assert out[0]["outcome"]["resolution"] == "time_stop"
    assert out[0]["outcome"]["exit_date"] >= s2["time_exit_on"]


def test_court_scorecard_uses_the_courts_own_bar(tmp_path):
    conn = _mem_conn()
    pid = gb.enrol_in_court(conn, "early_breakout")
    assert gb.enrol_in_court(conn, "early_breakout") == pid          # idempotent
    for i in range(12):
        ref = trial.record_shadow_fire(conn, pid, f"2026-08-{i + 1:02d}", "X")["ref"]
        trial.resolve_shadow(conn, ref, "win" if i < 10 else "loss", 1.0 if i < 10 else -1.0,
                             f"2026-08-{i + 2:02d}")
    card = gb.court_scorecard(conn, "early_breakout")
    assert card["n"] == 12 and card["wins"] == 10
    assert card["wilson_lb"] > card["null_rate"] and card["promote"] is True


# ============================================================ shadow guard

def test_module_is_shadow_only_and_on_no_schedule():
    src = (ROOT / "src" / "strategies" / "glassbreaking.py").read_text()
    for forbidden in ("journal.log(", "firm_treasury", "portfolio_manager",
                      "request_entry", "paper_broker", "place_order"):
        assert forbidden not in src, forbidden
    assert gb.MODE == "shadow"
    assert "glassbreaking" not in (ROOT / "scripts" / "setup_cron.sh").read_text()
    assert gb.SHADOW_LEDGER.name == "glassbreaking_shadow.jsonl"

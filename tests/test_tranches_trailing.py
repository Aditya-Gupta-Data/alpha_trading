"""
M4A pyramiding prep (2026-09-11, decision #96): tranche ladders are
OBSERVED by the tracker (never booked), the ATR ratchet is one pure
function shared by the equity trail and the opt-in spread trail, and the
live spread sweep stays trail-free (the 2026-08-05 lock).

Offline; fake journal; temp Brain Map. Run:
    python -m pytest tests/test_tranches_trailing.py -q
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import journal
import src.plan_tracker as PT
from tests.test_expiry_backstop import run_tracker_on, _restore_seams  # noqa: F401
from tests.test_options_spreads import make_open_spread, RANGE_BARS


# ------------------------------------------------------------ the ratchet

def test_atr_trailing_stop_only_rises_for_longs_and_only_falls_for_shorts():
    s1 = PT.atr_trailing_stop(110.0, 2.0, 2.0, floor=100.0)
    assert s1 == 106.0
    assert PT.atr_trailing_stop(105.0, 2.0, 2.0, floor=100.0, current=s1) == 106.0   # never down
    assert PT.atr_trailing_stop(120.0, 2.0, 2.0, floor=100.0, current=s1) == 116.0
    assert PT.atr_trailing_stop(101.0, 2.0, 2.0, floor=100.0) == 100.0             # never below floor
    b1 = PT.atr_trailing_stop(90.0, 2.0, 2.0, bullish=False)
    assert b1 == 94.0
    assert PT.atr_trailing_stop(95.0, 2.0, 2.0, current=b1, bullish=False) == 94.0  # never up
    assert PT.atr_trailing_stop(80.0, 2.0, 2.0, current=b1, bullish=False) == 84.0


def test_journal_plan_whitelist_now_carries_trailing_and_tranches():
    assert "trailing" in journal._PLAN_KEYS and "tranches" in journal._PLAN_KEYS


# ------------------------------------------------------------ tranche config

def test_tranche_config_normalises_and_rejects_junk():
    e = {"plan": {"tranches": {"levels": [{"at_r": 2.0, "add_lots": 1},
                                          {"at_r": 1.0, "add_lots": "2"},
                                          {"at_r": 0, "add_lots": 1},
                                          {"at_r": 3.0, "add_lots": 0},
                                          "junk"],
                               "max_lots": "5"}}}
    cfg = PT.tranche_config(e)
    assert cfg == {"levels": [{"at_r": 1.0, "add_lots": 2}, {"at_r": 2.0, "add_lots": 1}],
                   "max_lots": 5}
    assert PT.tranche_config({"plan": None}) is None
    assert PT.tranche_config({"plan": {"tranches": {"levels": []}}}) is None
    assert PT.tranche_config({"plan": {"tranches": "no"}}) is None


def test_tranche_events_fire_once_in_order_and_respect_the_cap():
    cfg = {"levels": [{"at_r": 1.0, "add_lots": 2}, {"at_r": 2.0, "add_lots": 2}],
           "max_lots": 5}
    path = [("d1", 0.5, 101.0), ("d2", 1.4, 103.0), ("d3", 0.9, 102.0),
            ("d4", 2.5, 105.0), ("d5", 3.0, 106.0)]
    ev = PT.tranche_events(cfg, base_lots=2, r_path=path)
    assert [(e["at_r"], e["date"], e["add_lots"]) for e in ev] == [(1.0, "d2", 2), (2.0, "d4", 1)]
    # a single bar that clears BOTH levels fires both, once
    ev = PT.tranche_events(cfg, base_lots=1, r_path=[("d1", 2.2, 105.0)])
    assert [e["at_r"] for e in ev] == [1.0, 2.0]


# ------------------------------------------------------------ equity observation

def _plan_entry(tranches=None, trailing=None):
    plan = {"stop_loss": {"price": 97.0}, "target": {"price": 120.0}}
    if tranches:
        plan["tranches"] = tranches
    if trailing:
        plan["trailing"] = trailing
    return {"short_id": "eq000001", "date": "2026-07-01", "action": "BUY",
            "ticker": "TCS.NS", "shares": 10, "price": 100.0, "signal": "t",
            "decision": "approved", "why": "w", "pattern_tags": [], "plan": plan,
            "outcome": None}


def _bars(closes, start_day=2):
    return [(f"2026-07-{start_day + i:02d}", c - 1.0, c + 1.0, c)
            for i, c in enumerate(closes)]


def test_equity_outcome_carries_tranche_observation_without_touching_pnl():
    entry = _plan_entry(tranches={"levels": [{"at_r": 1.0, "add_lots": 5},
                                             {"at_r": 2.0, "add_lots": 5}],
                                  "max_lots": 20})
    # risk/share = 3 -> +1R at high >= 103 (07-03: high 103.5), +2R at
    # high >= 106 (07-05: high 108); target 120 hits on 07-07
    bars = _bars([101.0, 102.5, 104.0, 107.0, 110.0, 121.0])
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, *_ = run_tracker_on(tmp, [entry], bars, date(2026, 7, 10))
    assert resolved == 1
    o = fj.rewritten[0]["outcome"]
    assert o["resolution"] == "target_hit"
    tr = o["tranches"]
    assert [(e["at_r"], e["date"], e["add_lots"]) for e in tr["events"]] == \
        [(1.0, "2026-07-03", 5), (2.0, "2026-07-05", 5)]
    assert tr["added_lots"] == 10 and tr["booked"] is False
    # add-on P&L is measured from the add price to the exit, kept apart
    assert tr["pyramid_pnl_rs"] == pytest.approx(5 * (120 - 103) + 5 * (120 - 106))
    assert o["pnl_rs"] < 10 * 20 + 1                 # base only (10 sh x Rs.20, less costs)


def test_no_ladder_means_no_tranche_key_at_all():
    with tempfile.TemporaryDirectory() as tmp:
        _, fj, *_ = run_tracker_on(tmp, [_plan_entry()], _bars([101.0, 121.0]),
                                   date(2026, 7, 10))
    assert "tranches" not in fj.rewritten[0]["outcome"]


def test_equity_time_stop_uses_the_today_seam():
    entry = _plan_entry()
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, *_ = run_tracker_on(tmp, [entry], _bars([101.0, 102.0]),
                                          date(2026, 9, 1))
    assert resolved == 1 and fj.rewritten[0]["outcome"]["resolution"] == "time_stop"


def test_trail_hit_has_a_digest_label_now():
    e = _plan_entry()
    e["outcome"] = {"resolution": "trail_hit", "price": 105.0, "exit_date": "2026-07-09",
                    "pct": 5.0, "r_multiple": 1.67, "days_in_trade": 8, "pnl_rs": 40.0,
                    "frictions_rs": 1.0, "slippage_rs": 0.5, "verdict": "WIN — trail",
                    "position_closed": True}
    assert PT._outcome_line(e).startswith("ATR TRAIL HIT")


# ------------------------------------------------------------ spread observation

def test_spread_outcome_observes_tranches_and_live_resolver_stays_trail_free():
    import inspect
    assert "trail" not in inspect.getsource(PT._resolve_spread).lower()   # the lock
    entry = make_open_spread(lots=1)
    entry["plan"] = {"tranches": {"levels": [{"at_r": 0.5, "add_lots": 1}], "max_lots": 3}}
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, *_ = run_tracker_on(tmp, [entry], RANGE_BARS, date(2026, 7, 27))
    assert resolved == 1
    o = fj.rewritten[0]["outcome"]
    assert o["resolution"] == "profit_take"
    assert o["tranches"]["booked"] is False
    assert o["tranches"]["events"] and o["tranches"]["events"][0]["at_r"] == 0.5


def test_trailed_spread_resolver_is_identical_without_a_trail_spec():
    entry = make_open_spread()
    assert PT._resolve_spread_trailed(entry, RANGE_BARS) == PT._resolve_spread(entry, RANGE_BARS)


def test_trailed_spread_resolver_exits_on_the_underlying_ratchet():
    entry = make_open_spread()
    entry["spread"]["max_profit"] = 0.0          # profit-take can never fire
    entry["plan"] = {"trailing": {"atr_mult": 1.0, "atr_n": 3}}
    # underlying grinds up for a week, then breaks down through the ratchet
    ups = [25000.0 + 20 * i for i in range(1, 8)]
    bars = [(f"2026-07-{7 + i:02d}", c - 10, c + 10, c) for i, c in enumerate(ups)]
    bars.append(("2026-07-15", 24900.0, 25150.0, 24950.0))
    hit = PT._resolve_spread_trailed(entry, bars)
    assert hit is not None and hit[0] == "trail_hit" and hit[3] == "2026-07-15"
    # the live resolver sees no exit on the same bars
    assert PT._resolve_spread(entry, bars) is None


def test_trailed_spread_resolver_trails_above_for_bearish_structures():
    entry = make_open_spread()
    entry["spread"]["max_profit"] = 0.0
    entry["spread"]["direction"] = "bearish"
    entry["plan"] = {"trailing": {"atr_mult": 1.0, "atr_n": 3}}
    downs = [25000.0 - 20 * i for i in range(1, 8)]
    bars = [(f"2026-07-{7 + i:02d}", c - 10, c + 10, c) for i, c in enumerate(downs)]
    bars.append(("2026-07-15", 24850.0, 25100.0, 25050.0))      # rips back up
    hit = PT._resolve_spread_trailed(entry, bars)
    assert hit is not None and hit[0] == "trail_hit"

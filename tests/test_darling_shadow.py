"""
Tests for the Darling shadow leg (F&O tranche step 5, re-wired to the
7-tier lifecycle system 2026-07-20): Buy-tier names -> PAPER_TELEMETRY
entries through the equity halt stack; Strong-Sell grades force-exit
open shadows (Directive 2, the No-Orphan rule); resolved offline
against bhavcopy closes.

Fully offline: kg journal, tier table, and levels all live in tmp paths;
the halt stack is injected. Run:
    python -m pytest tests/test_darling_shadow.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.equity_shadow_proposer as sp
from src import knowledge_graph_logger as kg

IST = timezone(timedelta(hours=5, minutes=30))


def _tier_row(sym="TCS", close=2269.0, stop=2085.0, val=35, forensic=64,
              tier="strong_buy", in_zone=True, pinned=None):
    return {"symbol": sym, "valuation": val, "forensic": forensic,
            "close": close, "buy_zone": [2189.72, 2293.28], "stop": stop,
            "extension": "normal", "tier": tier,
            "family": {"strong_buy": "buy", "weak_buy": "buy",
                       "strong_sell": "sell"}.get(tier, "hold"),
            "in_zone": in_zone, "pinned": pinned,
            "rule": (f"pinned (strong_sell): {pinned}" if pinned
                     else f"in zone + valuation {val} <= 45")}


def _level_row(sym="TCS", trims=(2460.0, 2510.0)):
    # Default trims sit past 1R (entry 2269, stop 2085 -> 1R = 2453) so
    # the first-qualifying-pivot rule picks 2460.
    return {"symbol": sym, "status": "ok", "trim_levels": list(trims),
            "anchored_vwap": 2240.0}


def _write_artifacts(tmp, rows_by_tier: dict, level_rows):
    tiers = tmp / "darling_tiers.json"
    levels = tmp / "darlings_levels.json"
    tiers.write_text(json.dumps({"tiers": rows_by_tier}))
    levels.write_text(json.dumps({"levels": level_rows}))
    return tiers, levels


def _allow_all(proposal):
    return {"allowed": True, "blocked_by": None, "reason": None}


def test_evaluate_darling_entry_frame_and_contract():
    e = sp.evaluate_darling_entry(_tier_row(), _level_row(), "2026-07-20")
    assert e["mode"] == "PAPER_TELEMETRY" and e["capital_allocated"] == 0
    assert e["ticker"] == "TCS.NS"
    assert e["kyu_trigger"]["setup"] == "darling_buy"
    assert e["kyu_trigger"]["tier"] == "strong_buy"
    assert e["kyu_trigger"]["valuation"] == 35
    assert "strong_buy" in e["kyu_trigger"]["signal"]
    a = e["kya_kara_action"]
    assert a["entry_price"] == 2269.0 and a["stop"] == 2085.0
    assert a["target"] == 2460.0            # first trim past 1R
    assert a["fill_basis"] == "eod_close"   # honesty: not a live quote
    assert a["time_stop_days"] == sp.DARLING_TIME_STOP_DAYS


def test_evaluate_darling_entry_2r_fallback_and_anomaly_refusal():
    fallback = round(2269.0 + sp.REWARD_RISK * (2269.0 - 2085.0), 2)
    # No trim above entry -> 2R fallback.
    e = sp.evaluate_darling_entry(_tier_row(), _level_row(trims=(2200.0,)),
                                  "2026-07-20")
    assert e["kya_kara_action"]["target"] == fallback
    # A trim above entry but under 1R is a near-zero instant "win" that
    # would poison the win-rate -> also the 2R fallback (the LTF catch).
    e = sp.evaluate_darling_entry(_tier_row(), _level_row(trims=(2270.0,)),
                                  "2026-07-20")
    assert e["kya_kara_action"]["target"] == fallback
    # A stop at/above close is a data anomaly, never a loggable thesis.
    assert sp.evaluate_darling_entry(_tier_row(stop=2270.0), _level_row(),
                                     "2026-07-20") is None
    assert sp.evaluate_darling_entry(_tier_row(close=None), _level_row(),
                                     "2026-07-20") is None


def test_propose_logs_buy_tiers_and_respects_halt_stack():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("TCS"),
                                 _tier_row("KAYNES", close=5900.0,
                                           stop=5500.0)]},
            [_level_row("TCS")])
        journal = tmp / "shadow.jsonl"

        def stack(proposal):
            if proposal["symbol"] == "KAYNES":
                return {"allowed": False, "blocked_by": "liquidity_filter",
                        "reason": "exchange F&O BAN list (MWPL) — blocked"}
            assert proposal == {"symbol": "TCS", "direction": "long",
                                "instrument": "delivery"}
            return _allow_all(proposal)

        logged = sp.propose_darling_entries(
            tiers_path=tiers, levels_path=levels, path=journal,
            as_of="2026-07-20", check_fn=stack, universe={},
            nifty_trend_fn=lambda: None)
        assert [e["ticker"] for e in logged] == ["TCS.NS"]
        assert kg.open_positions(path=journal).keys() == {"TCS.NS"}


def test_near_zone_weak_buy_is_never_chased():
    """A weak_buy row that is NOT in the zone (the 5% near-zone band) is
    watched, not bought — the patience doctrine survives the tiers."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiers, levels = _write_artifacts(
            tmp, {"weak_buy": [
                _tier_row("INZONE", tier="weak_buy", val=40),
                _tier_row("NEAR", tier="weak_buy", val=40, in_zone=False)]},
            [_level_row("INZONE"), _level_row("NEAR")])
        logged = sp.propose_darling_entries(
            tiers_path=tiers, levels_path=levels,
            path=tmp / "shadow.jsonl", as_of="2026-07-20",
            check_fn=_allow_all, universe={}, nifty_trend_fn=lambda: None)
        assert [e["ticker"] for e in logged] == ["INZONE.NS"]


def test_propose_dedups_open_and_same_day_exit():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiers, levels = _write_artifacts(tmp,
                                         {"strong_buy": [_tier_row("TCS")]},
                                         [_level_row("TCS")])
        journal = tmp / "shadow.jsonl"
        args = dict(tiers_path=tiers, levels_path=levels, path=journal,
                    as_of="2026-07-20", check_fn=_allow_all, universe={},
                    nifty_trend_fn=lambda: None)
        first = sp.propose_darling_entries(**args)
        assert len(first) == 1
        # Still open -> a repeat-Buy day logs nothing (no pyramiding).
        assert sp.propose_darling_entries(**args) == []
        # Exit today -> same-day re-entry stays blocked.
        kg.log_event({"event": "exit", "id": first[0]["id"],
                      "ticker": "TCS.NS",
                      "ts": "2026-07-20T15:40:00+05:30"}, path=journal)
        assert sp.propose_darling_entries(**args) == []


def test_darling_time_stop_override_respected():
    with tempfile.TemporaryDirectory() as tmp:
        journal = Path(tmp) / "shadow.jsonl"
        e = sp.evaluate_darling_entry(_tier_row(), _level_row(),
                                      "2026-07-01")
        kg.log_event(e, path=journal)
        quote = lambda t: 2200.0            # between stop and target
        # Day 12: past the block leg's 10d default, inside the darling 45d.
        now = datetime(2026, 7, 13, 16, 0, tzinfo=IST)
        exits = sp.track_open_shadows(quote_fn=quote, vix_fn=lambda: None,
                                      universe={}, path=journal, now=now)
        assert exits == []
        # Day 46: the darling time stop fires.
        now = datetime(2026, 8, 16, 16, 0, tzinfo=IST)
        exits = sp.track_open_shadows(quote_fn=quote, vix_fn=lambda: None,
                                      universe={}, path=journal, now=now)
        assert len(exits) == 1 and exits[0]["reason"] == "time_stop"


def test_strong_sell_force_exit_pinned_and_graded():
    """Directive 2: an open darling shadow graded strong_sell closes at
    the day's price — fundamental_break when pinned, strong_sell_tier
    when valuation-driven. Weak-sell never force-exits."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        journal = tmp / "shadow.jsonl"
        for sym in ("TCS", "INFY", "WIPRO"):
            kg.log_event(sp.evaluate_darling_entry(
                _tier_row(sym), _level_row(sym), "2026-07-15"),
                path=journal)
        tiers, _ = _write_artifacts(
            tmp, {"strong_sell": [
                _tier_row("TCS", tier="strong_sell",
                          pinned="weekly re-screen rejected: streak broke"),
                _tier_row("INFY", tier="strong_sell", val=88)],
                "weak_sell": [_tier_row("WIPRO", tier="weak_sell")]},
            [])
        exits = sp.force_exit_strong_sell(
            tiers_path=tiers, path=journal, quote_fn=lambda t: 2100.0,
            now=datetime(2026, 7, 25, 18, 0, tzinfo=IST))
        by = {e["ticker"]: e for e in exits}
        assert set(by) == {"TCS.NS", "INFY.NS"}   # WIPRO stays open
        assert by["TCS.NS"]["reason"] == "fundamental_break"
        assert "No-Orphan" in by["TCS.NS"]["kya_sikha_autopsy"]["category"]
        assert by["INFY.NS"]["reason"] == "strong_sell_tier"
        assert by["INFY.NS"]["kya_sikha_autopsy"]["r_multiple"] is not None
        assert kg.open_positions(path=journal).keys() == {"WIPRO.NS"}


def test_darling_autopsy_texts():
    entry = sp.evaluate_darling_entry(_tier_row(), _level_row(),
                                      "2026-07-20")
    assert "trim pivot" in sp.categorize_failure("target", 2350.0, entry,
                                                 None)
    stop_cat = sp.categorize_failure("stop_loss", 2084.0, entry, None)
    assert "Buy-zone defense failed" in stop_cat
    # Legacy pre-tier ledger rows (setup "darling_ripe") still autopsy
    # as darlings.
    legacy = {"kyu_trigger": {"setup": "darling_ripe"},
              "kya_kara_action": {"stop": 2085.0}}
    assert "trim pivot" in sp.categorize_failure("target", 2500.0, legacy,
                                                 None)
    # Block-leg texts unchanged.
    block_entry = {"kyu_trigger": {"setup": "block_vwap_pullback",
                                   "block_vwap": 100.0},
                   "kya_kara_action": {"stop": 98.0}}
    assert "block-VWAP floor defense held" in sp.categorize_failure(
        "target", 105.0, block_entry, None)


def test_run_darling_cycle_resolves_forces_then_proposes_offline():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("INFY", close=1096.5,
                                           stop=1000.0)],
                  "strong_sell": [
                      _tier_row("HCLTECH", tier="strong_sell", val=88,
                                close=1500.0, stop=1400.0)]},
            [_level_row("INFY", trims=(1150.0,))])
        journal = tmp / "shadow.jsonl"
        # An older open shadow whose stop the day's close broke...
        kg.log_event(sp.evaluate_darling_entry(
            _tier_row("TCS"), _level_row("TCS"), "2026-07-15"),
            path=journal)
        # ...and one that got graded strong_sell while still open.
        kg.log_event(sp.evaluate_darling_entry(
            _tier_row("HCLTECH", close=1500.0, stop=1400.0),
            _level_row("HCLTECH", trims=(1700.0,)), "2026-07-15"),
            path=journal)
        closes = {"TCS.NS": 2000.0, "INFY.NS": 1096.5, "HCLTECH.NS": 1480.0}
        res = sp.run_darling_cycle(tiers_path=tiers, levels_path=levels,
                                   path=journal,
                                   quote_fn=lambda t: closes[t],
                                   universe={}, check_fn=_allow_all,
                                   as_of="2026-07-20")
        reasons = {e["ticker"]: e["reason"] for e in res["exits"]}
        assert reasons == {"TCS.NS": "stop_loss",
                           "HCLTECH.NS": "strong_sell_tier"}
        assert [e["ticker"] for e in res["entries"]] == ["INFY.NS"]
        # Journal ends with exactly one open position: the new INFY line.
        assert kg.open_positions(path=journal).keys() == {"INFY.NS"}


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_darling_shadow.py")

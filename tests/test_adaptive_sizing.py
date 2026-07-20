"""
Adaptive sizing (owner Directive 2, decision #81) — hermetic tests.

The autopsy-driven feedback loop: break-even-centered Beta prior (neutral
at zero data — no coin-flip learning), fast Wilson-gated penalties with a
0.25x floor, EARNED vetoes (Wilson upper bound under break-even), slow
boosts (lower bound over break-even + margin), gap-shock half-weight,
ticker-level veto overlay, and the desk seams that fail open. Run:
    python -m pytest tests/test_adaptive_sizing.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.adaptive_sizing as az
import src.equity_desk as desk
from src import knowledge_graph_logger as kg
from src import portfolio_manager as pm

az.ADAPTIVE_SIZING_ENABLED = True
desk.EQUITY_DESK_CAPITAL_RS = 300000.0
desk.EQUITY_DESK_RISK_PER_TRADE_PCT = 1.0


def _rows(wins=0, losses=0, r_win=2.0, r_loss=1.0, gap=False,
          ticker="TCS.NS", key=("darling_buy", "weak_buy")):
    out = [{"key": key, "ticker": ticker, "r": r_win, "weight": 1.0}
           for _ in range(wins)]
    out += [{"key": key, "ticker": ticker, "r": -r_loss,
             "weight": az.GAP_SHOCK_WEIGHT if gap else 1.0}
            for _ in range(losses)]
    return out


def test_neutral_until_the_evidence_bar():
    assert az.evaluate([], "equity")["multiplier"] == 1.0
    v = az.evaluate(_rows(wins=1, losses=2), "equity")   # n=3 < 4
    assert v["action"] == "neutral" and v["multiplier"] == 1.0


def test_penalty_is_fast_monotone_and_floored():
    v6 = az.evaluate(_rows(losses=6), "equity")
    v7 = az.evaluate(_rows(losses=7), "equity")
    assert v6["action"] == v7["action"] == "penalty"
    assert v7["multiplier"] < v6["multiplier"] < 1.0     # worse = smaller
    # Options family (higher break-even) drives the floor.
    vf = az.evaluate(_rows(losses=7), "options")
    assert vf["multiplier"] == az.FLOOR


def test_veto_is_earned_at_the_bar_not_before():
    v7 = az.evaluate(_rows(losses=7), "equity")          # n=7 < 8
    assert v7["action"] == "penalty" and v7["multiplier"] > 0
    v8 = az.evaluate(_rows(losses=8), "equity")
    assert v8["action"] == "veto" and v8["multiplier"] == 0.0
    assert "break-even" in v8["detail"]


def test_boost_is_slow_capped_and_bar_gated():
    v9 = az.evaluate(_rows(wins=9), "equity")            # n=9 < 10
    assert v9["action"] != "boost"
    v10 = az.evaluate(_rows(wins=10), "equity")
    assert v10["action"] == "boost"
    assert 1.0 < v10["multiplier"] <= az.CAP == 1.5


def test_gap_shock_losses_count_half():
    hard = az.evaluate(_rows(losses=6), "equity")
    soft = az.evaluate(_rows(losses=6, gap=True), "equity")
    # Six gap-shock losses = 3 effective — still under the penalty bar.
    assert hard["action"] == "penalty"
    assert soft["action"] == "neutral" and soft["n_eff"] == 3.0


def test_break_even_empirical_needs_both_sides():
    thin = az._stats(_rows(wins=2, losses=8))            # wins side thin
    assert az.break_even(thin, "equity") == 0.40         # nominal holds
    full = az._stats(_rows(wins=4, losses=4, r_win=3.0, r_loss=1.0))
    assert az.break_even(full, "equity") == 0.25         # 1/(3+1)


def test_wilson_upper_mirrors_lower():
    assert az.wilson_upper_bound(0, 0) == 1.0
    ub = az.wilson_upper_bound(4, 10)
    from src.validation.stat_gates import wilson_lower_bound
    lb = wilson_lower_bound(4, 10)
    assert lb < 0.4 < ub


def test_ticker_veto_overlay_fires_independently():
    group = _rows(wins=5, losses=5)                      # family is fine-ish
    burned = _rows(losses=6, ticker="KAYNES.NS")
    v = az.evaluate(group, "equity", ticker="KAYNES.NS",
                    ticker_rows=burned)
    assert v["action"] == "veto" and "ticker veto" in v["detail"]
    ok = az.evaluate(group, "equity", ticker="TCS.NS",
                     ticker_rows=_rows(losses=2, ticker="TCS.NS"))
    assert ok["action"] != "veto"


def test_equity_history_pairs_weights_and_excludes():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "shadow.jsonl"
        kg.log_event({"event": "entry", "id": "a1", "ticker": "TCS.NS",
                      "kyu_trigger": {"setup": "darling_buy",
                                      "tier": "weak_buy"}}, path=ledger)
        kg.log_event({"event": "exit", "id": "a1", "ticker": "TCS.NS",
                      "kya_sikha_autopsy": {
                          "r_multiple": -1.0,
                          "category": "Gap-down shock: opened through"}},
                     path=ledger)
        kg.log_event({"event": "entry", "id": "b2", "ticker": "INFY.NS",
                      "kyu_trigger": {"setup": "block_vwap_pullback"}},
                     path=ledger)
        kg.log_event({"event": "exit", "id": "b2", "ticker": "INFY.NS",
                      "kya_sikha_autopsy": {"r_multiple": 2.0,
                                            "category": "Target hit"}},
                     path=ledger)
        kg.log_event({"event": "entry", "id": "c3", "ticker": "OPEN.NS",
                      "kyu_trigger": {"setup": "darling_buy"}}, path=ledger)
        rows = az.equity_history(ledger_path=ledger)
        assert len(rows) == 2                            # open c3 excluded
        by = {r["ticker"]: r for r in rows}
        assert by["TCS.NS"]["weight"] == 0.5             # gap-shock loss
        assert by["TCS.NS"]["key"] == ("darling_buy", "weak_buy")
        assert by["INFY.NS"]["weight"] == 1.0            # wins never halve


def test_options_history_mirrors_performance_exclusions():
    entries = [
        {"ticker": "NIFTY 50", "spread": {"strategy": "iron_condor"},
         "outcome": {"r_multiple": 0.8}},
        {"ticker": "NIFTY 50", "spread": {"strategy": "iron_condor"},
         "outcome": {"r_multiple": -1.2, "hypothetical": True}},   # out
        {"ticker": "NIFTY 50", "spread": {"strategy": "bear_put_spread"},
         "outcome": {}},                                           # no r
        {"ticker": "NIFTY 50", "spread": {"strategy": "bear_put_spread"},
         "outcome": {"r_multiple": -1.0}},
    ]
    rows = az.options_history(entries=entries)
    assert [(r["key"][1], r["r"]) for r in rows] == [
        ("iron_condor", 0.8), ("bear_put_spread", -1.0)]


def test_fund_entry_applies_multiplier_and_veto():
    entry = {"id": "z9", "ticker": "TCS.NS",
             "kyu_trigger": {"setup": "darling_buy", "tier": "weak_buy"},
             "kya_kara_action": {"entry_price": 2269.0, "stop": 2085.0}}
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "desk.db"
        real = az.equity_verdict
        try:
            az.equity_verdict = lambda e, **kw: {"multiplier": 0.5,
                                                 "action": "penalty",
                                                 "detail": "test"}
            f = desk.fund_entry(entry, db_path=db)
            assert f["funded"] and f["qty"] == 8         # 16 x 0.5 risk
            assert "x0.5" in f["reason"]
            az.equity_verdict = lambda e, **kw: {"multiplier": 0.0,
                                                 "action": "veto",
                                                 "detail": "burned"}
            f = desk.fund_entry(dict(entry, id="z10"), db_path=db)
            assert not f["funded"]
            assert "vetoed_by adaptive_sizing" in f["reason"]
        finally:
            az.equity_verdict = real


def test_adjust_option_lots_floors_and_vetoes():
    win = {"spread": {"strategy": "iron_condor"}, "ticker": "N",
           "outcome": {"r_multiple": 0.5}}
    loss = {"spread": {"strategy": "iron_condor"}, "ticker": "N",
            "outcome": {"r_multiple": -1.0}}
    with tempfile.TemporaryDirectory() as tmp:
        adj = Path(tmp) / "adj.jsonl"
        # Penalty (6 losses) floors at 1 lot, never 0.
        lots, v = az.adjust_option_lots("iron_condor", 2,
                                        entries=[loss] * 6,
                                        adjustments_path=adj,
                                        broadcast_fn=lambda c: None)
        assert v["action"] == "penalty" and lots == 1
        # Earned veto (8 losses) zeroes.
        lots, v = az.adjust_option_lots("iron_condor", 2,
                                        entries=[loss] * 8,
                                        adjustments_path=adj,
                                        broadcast_fn=lambda c: None)
        assert v["action"] == "veto" and lots == 0
        # Healthy record: untouched (and no boost — size_lots is the cap).
        lots, v = az.adjust_option_lots("iron_condor", 2,
                                        entries=[win] * 12,
                                        adjustments_path=adj,
                                        broadcast_fn=lambda c: None)
        assert lots == 2


def test_kill_switch_and_fail_open():
    az.ADAPTIVE_SIZING_ENABLED = False
    try:
        v = az.equity_verdict({"kyu_trigger": {}})
        assert v["multiplier"] == 1.0 and v["action"] == "neutral"
        lots, v = az.adjust_option_lots("iron_condor", 3)
        assert lots == 3 and v["action"] == "neutral"
    finally:
        az.ADAPTIVE_SIZING_ENABLED = True
    # A crashing reader degrades to neutral, never an exception.
    real = az.equity_history
    try:
        az.equity_history = lambda **kw: 1 / 0
        v = az.equity_verdict({"kyu_trigger": {"setup": "darling_buy"},
                               "ticker": "TCS.NS"})
        assert v["multiplier"] == 1.0 and "unavailable" in v["detail"]
    finally:
        az.equity_history = real


def test_record_ledgers_nonneutral_and_cards_only_transitions():
    with tempfile.TemporaryDirectory() as tmp:
        adj = Path(tmp) / "adj.jsonl"
        cards = []
        pen = {"multiplier": 0.5, "action": "penalty", "detail": "d",
               "n_eff": 6.0, "break_even": 0.4}
        az.record("darling_buy/weak_buy", pen, "e1", path=adj,
                  broadcast_fn=cards.append)
        az.record("darling_buy/weak_buy", pen, "e2", path=adj,
                  broadcast_fn=cards.append)             # same state
        neu = {"multiplier": 1.0, "action": "neutral", "detail": "ok",
               "n_eff": 9.0, "break_even": 0.4}
        az.record("darling_buy/weak_buy", neu, "e3", path=adj,
                  broadcast_fn=cards.append)             # recovery
        az.record("darling_buy/weak_buy", neu, "e4", path=adj,
                  broadcast_fn=cards.append)             # neutral again
        rows = [json.loads(l) for l in adj.read_text().splitlines()]
        assert [r["action"] for r in rows] == ["penalty", "penalty",
                                               "neutral"]
        assert len(cards) == 2                           # 2 transitions
        assert "penalty" in cards[0]["description"]
        assert "→ neutral" in cards[1]["description"]


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_adaptive_sizing.py")

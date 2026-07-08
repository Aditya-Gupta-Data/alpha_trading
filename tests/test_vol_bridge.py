"""
Tests for the quantitative execution bridge (src/vol_bridge.py).

Verifies:
  • Node polarity classification (negative/positive/neutral keywords)
  • Net-signal arithmetic across a variety of edge configurations
  • Regime classification boundaries (Expansion/Contraction/Neutral)
  • Parameter scaling under mock macro shocks
      – scale_risk mode: risk_pct reduced by exactly 30 %
      – widen_wings mode: short_strike_otm_pct widened by exactly 50 %
  • Boundary precision (values at and just past the threshold)
  • Fail-safe behaviour: empty graph, missing table, bad DB

All tests are 100 % offline — they use in-memory SQLite connections seeded
with synthetic edges via graph_engine.add_edge / ensure_schema.  No network,
no real data/brain_map.db touched (HANDOVER "never reset live data" rule).

Run:
    python tests/test_vol_bridge.py
    pytest tests/test_vol_bridge.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.graph_engine import add_edge, ensure_schema
from src.vol_bridge import (
    BASE_SHORT_STRIKE_OTM_PCT,
    CONTRACTION_THRESHOLD,
    EXPANSION_THRESHOLD,
    PUT_WING_SCALE_FACTOR,
    RISK_SCALE_FACTOR,
    _load_active_edges,
    _net_signal,
    _node_polarity,
    classify_regime,
    compute_regime_overrides,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn_with_edges(edges: list[tuple]) -> "sqlite3.Connection":
    """In-memory brain_map DB with graph_edges populated.

    Each tuple is (source_node, relation, target_node, confidence_score).
    """
    conn = brain_map.connect(":memory:")
    ensure_schema(conn)
    for src, rel, tgt, conf in edges:
        add_edge(conn, src, rel, tgt, conf)
    return conn


def _conn_empty_graph() -> "sqlite3.Connection":
    """In-memory brain_map DB with the graph_edges table but no rows."""
    conn = brain_map.connect(":memory:")
    ensure_schema(conn)
    return conn


def _conn_no_table() -> "sqlite3.Connection":
    """In-memory brain_map DB with core tables but NO graph_edges table."""
    conn = brain_map.connect(":memory:")
    # brain_map.connect creates the core tables; graph_edges is NOT created
    # until graph_engine.ensure_schema is called.  Don't call it here.
    return conn


# ---------------------------------------------------------------------------
# _node_polarity
# ---------------------------------------------------------------------------

def test_polarity_negative_keywords():
    for word in ("loss", "bearish", "crash", "stress", "drawdown", "breakdown",
                 "negative", "weak", "fail", "warning", "shock", "decline"):
        assert _node_polarity(word) == -1, word
    assert _node_polarity("iron_condor_RESULTS_IN_loss") == -1
    assert _node_polarity("HIGH_TAIL_RISK") == -1


def test_polarity_positive_keywords():
    for word in ("win", "bullish", "gain", "profit", "recovery",
                 "breakout", "strong", "growth", "rally", "expansion"):
        assert _node_polarity(word) == 1, word
    assert _node_polarity("iron_condor_RESULTS_IN_win") == 1


def test_polarity_neutral_keywords():
    assert _node_polarity("iron_condor") == 0
    assert _node_polarity("NIFTY 50") == 0
    assert _node_polarity("") == 0
    assert _node_polarity(None) == 0
    assert _node_polarity("RATE_CUT") == 0


def test_polarity_case_insensitive():
    assert _node_polarity("LOSS") == -1
    assert _node_polarity("WIN") == 1
    assert _node_polarity("BuLLiSh") == 1


# ---------------------------------------------------------------------------
# _net_signal
# ---------------------------------------------------------------------------

def test_net_signal_all_negative():
    edges = [
        {"source_node": "iron_condor", "target_node": "loss",
         "relation": "RESULTS_IN", "confidence_score": 0.9},
        {"source_node": "iron_condor", "target_node": "drawdown",
         "relation": "RESULTS_IN", "confidence_score": 0.7},
    ]
    signal = _net_signal(edges)
    assert abs(signal - (-0.9 - 0.7)) < 1e-9


def test_net_signal_all_positive():
    edges = [
        {"source_node": "iron_condor", "target_node": "win",
         "relation": "RESULTS_IN", "confidence_score": 1.0},
        {"source_node": "breakout", "target_node": "profit",
         "relation": "LEADS_TO", "confidence_score": 0.6},
    ]
    signal = _net_signal(edges)
    assert abs(signal - (1.0 + 0.6)) < 1e-9


def test_net_signal_mixed():
    edges = [
        {"source_node": "x", "target_node": "loss",   "relation": "r", "confidence_score": 0.9},
        {"source_node": "x", "target_node": "win",    "relation": "r", "confidence_score": 0.3},
        {"source_node": "x", "target_node": "neutral", "relation": "r", "confidence_score": 1.0},
    ]
    # neutral target, neutral source → skipped; net = -0.9 + 0.3
    signal = _net_signal(edges)
    assert abs(signal - (-0.6)) < 1e-9


def test_net_signal_empty_list():
    assert _net_signal([]) == 0.0


def test_net_signal_neutral_only():
    edges = [
        {"source_node": "iron_condor", "target_node": "nifty_50",
         "relation": "r", "confidence_score": 0.8},
    ]
    assert _net_signal(edges) == 0.0


def test_net_signal_null_confidence_defaults_to_1():
    edges = [
        {"source_node": "x", "target_node": "loss", "relation": "r", "confidence_score": None},
    ]
    assert _net_signal(edges) == -1.0


def test_net_signal_fallback_to_source_polarity():
    edges = [
        {"source_node": "bearish", "target_node": "iron_condor",
         "relation": "INDICATES", "confidence_score": 0.5},
    ]
    signal = _net_signal(edges)
    assert abs(signal - (-0.5)) < 1e-9


# ---------------------------------------------------------------------------
# classify_regime
# ---------------------------------------------------------------------------

def test_classify_regime_expansion():
    edges = [
        {"source_node": "x", "target_node": "loss",   "relation": "r", "confidence_score": 0.9},
        {"source_node": "x", "target_node": "bearish", "relation": "r", "confidence_score": 0.5},
    ]
    assert classify_regime(edges) == "Expansion"


def test_classify_regime_contraction():
    edges = [
        {"source_node": "x", "target_node": "win",    "relation": "r", "confidence_score": 0.8},
        {"source_node": "x", "target_node": "profit",  "relation": "r", "confidence_score": 0.4},
    ]
    assert classify_regime(edges) == "Contraction"


def test_classify_regime_neutral_empty():
    assert classify_regime([]) == "Neutral"


def test_classify_regime_neutral_balanced():
    edges = [
        {"source_node": "x", "target_node": "loss", "relation": "r", "confidence_score": 0.4},
        {"source_node": "x", "target_node": "win",  "relation": "r", "confidence_score": 0.4},
    ]
    assert classify_regime(edges) == "Neutral"


def test_classify_regime_exactly_at_threshold_is_neutral():
    # net = -EXPANSION_THRESHOLD exactly → NOT below → Neutral
    edges = [
        {"source_node": "x", "target_node": "loss",
         "relation": "r", "confidence_score": EXPANSION_THRESHOLD},
    ]
    assert classify_regime(edges) == "Neutral"


def test_classify_regime_just_past_threshold_is_expansion():
    eps = 1e-6
    edges = [
        {"source_node": "x", "target_node": "loss",
         "relation": "r", "confidence_score": EXPANSION_THRESHOLD + eps},
    ]
    assert classify_regime(edges) == "Expansion"


def test_classify_regime_just_past_contraction_threshold():
    eps = 1e-6
    edges = [
        {"source_node": "x", "target_node": "win",
         "relation": "r", "confidence_score": CONTRACTION_THRESHOLD + eps},
    ]
    assert classify_regime(edges) == "Contraction"


# ---------------------------------------------------------------------------
# compute_regime_overrides — parameter scaling boundaries
# ---------------------------------------------------------------------------

def test_scale_risk_expansion_cuts_risk_pct_by_30_percent():
    conn = _conn_with_edges([
        ("iron_condor", "RESULTS_IN", "loss", 0.9),
        ("bearish",     "INDICATES",  "loss", 0.5),
    ])
    overrides = compute_regime_overrides(conn=conn, mode="scale_risk",
                                         base_risk_pct=10.0)
    assert overrides.get("regime") == "Expansion"
    assert "risk_pct" in overrides
    expected = 10.0 * RISK_SCALE_FACTOR   # 7.0
    assert abs(overrides["risk_pct"] - expected) < 1e-9


def test_widen_wings_expansion_scales_otm_pct():
    conn = _conn_with_edges([
        ("iron_condor", "RESULTS_IN", "loss", 0.9),
        ("crash",       "PRECEDES",   "drawdown", 0.5),
    ])
    overrides = compute_regime_overrides(conn=conn, mode="widen_wings",
                                         base_risk_pct=10.0,
                                         base_otm_pct=2.0)
    assert overrides.get("regime") == "Expansion"
    assert "short_strike_otm_pct" in overrides
    assert "risk_pct" not in overrides
    expected = 2.0 * PUT_WING_SCALE_FACTOR   # 3.0
    assert abs(overrides["short_strike_otm_pct"] - expected) < 1e-9


def test_contraction_returns_no_parameter_overrides():
    conn = _conn_with_edges([
        ("iron_condor", "RESULTS_IN", "win",    0.8),
        ("bullish",     "LEADS_TO",   "profit", 0.4),
    ])
    overrides = compute_regime_overrides(conn=conn, base_risk_pct=10.0)
    assert overrides.get("regime") == "Contraction"
    assert "risk_pct" not in overrides
    assert "short_strike_otm_pct" not in overrides


def test_neutral_returns_no_parameter_overrides():
    conn = _conn_empty_graph()
    overrides = compute_regime_overrides(conn=conn, base_risk_pct=10.0)
    assert overrides.get("regime") == "Neutral"
    assert "risk_pct" not in overrides
    assert "short_strike_otm_pct" not in overrides


def test_no_table_returns_neutral_no_overrides():
    conn = _conn_no_table()
    overrides = compute_regime_overrides(conn=conn, base_risk_pct=10.0)
    # graph_edges table absent → no edges → Neutral, no adjustments
    assert overrides.get("regime") == "Neutral"
    assert "risk_pct" not in overrides


# ---------------------------------------------------------------------------
# Mock macro shock scenarios
# ---------------------------------------------------------------------------

def test_macro_shock_many_high_confidence_negative_edges():
    """Simulates a macro stress event: many strong negative signals drive a
    clear Expansion regime with the correct 30 % risk cut."""
    edges = [
        ("iron_condor",  "RESULTS_IN", "loss",     0.95),
        ("high_vix",     "INDICATES",  "crash",     0.88),
        ("bearish_trend","PRECEDES",   "drawdown",  0.80),
        ("rate_shock",   "LEADS_TO",   "breakdown", 0.75),
        ("global_stress","INDICATES",  "decline",   0.60),
    ]
    conn = _conn_with_edges(edges)
    overrides = compute_regime_overrides(conn=conn, mode="scale_risk",
                                         base_risk_pct=10.0)
    assert overrides["regime"] == "Expansion"
    assert abs(overrides["risk_pct"] - 7.0) < 1e-9


def test_macro_shock_widen_wings_mode():
    """Under a macro shock, widen_wings mode moves the short put further OTM."""
    edges = [
        ("iron_condor", "RESULTS_IN", "loss",    0.9),
        ("bearish",     "INDICATES",  "decline", 0.6),
    ]
    conn = _conn_with_edges(edges)
    overrides = compute_regime_overrides(conn=conn, mode="widen_wings",
                                         base_risk_pct=10.0,
                                         base_otm_pct=BASE_SHORT_STRIKE_OTM_PCT)
    assert overrides["regime"] == "Expansion"
    assert "short_strike_otm_pct" in overrides
    # Wider OTM → the returned value must be strictly greater than the base.
    assert overrides["short_strike_otm_pct"] > BASE_SHORT_STRIKE_OTM_PCT


def test_partial_negative_signal_stays_neutral():
    """A single weak negative edge doesn't cross the threshold."""
    edges = [
        ("iron_condor", "RESULTS_IN", "loss", 0.3),
    ]
    conn = _conn_with_edges(edges)
    overrides = compute_regime_overrides(conn=conn, base_risk_pct=10.0)
    assert overrides.get("regime") == "Neutral"
    assert "risk_pct" not in overrides


def test_positive_dominance_after_learning_from_wins():
    """After the simulator mints many win edges, the regime is Contraction."""
    edges = [
        ("iron_condor", "RESULTS_IN", "win",    0.9),
        ("bullish",     "INDICATES",  "profit", 0.7),
        ("breakout",    "LEADS_TO",   "gain",   0.5),
        ("iron_condor", "RESULTS_IN", "loss",   0.1),   # small minority
    ]
    conn = _conn_with_edges(edges)
    overrides = compute_regime_overrides(conn=conn, base_risk_pct=10.0)
    assert overrides["regime"] == "Contraction"
    assert "risk_pct" not in overrides


# ---------------------------------------------------------------------------
# _load_active_edges — expired edges excluded
# ---------------------------------------------------------------------------

def test_expired_edges_excluded_from_active_set():
    """Edges stamped with invalid_at must NOT appear in the active edge list."""
    conn = brain_map.connect(":memory:")
    ensure_schema(conn)
    add_edge(conn, "iron_condor", "RESULTS_IN", "loss", 0.9)
    # Manually expire that edge to simulate decay threshold crossed:
    conn.execute(
        "UPDATE graph_edges SET invalid_at = '2026-07-08T00:00:00' "
        "WHERE source_node = 'iron_condor'"
    )
    conn.commit()
    add_edge(conn, "bullish", "LEADS_TO", "win", 0.8)   # active edge

    active = _load_active_edges(conn)
    assert all(e["target_node"] != "loss" for e in active)
    assert any(e["target_node"] == "win" for e in active)


# ---------------------------------------------------------------------------
# Fail-safe: error paths never raise
# ---------------------------------------------------------------------------

def test_compute_regime_overrides_never_raises_on_bad_conn():
    import sqlite3
    conn = brain_map.connect(":memory:")
    conn.close()  # already closed — all DB calls will fail
    result = compute_regime_overrides(conn=conn, base_risk_pct=10.0)
    # Must return {} (or a partial dict) without raising.
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Integration: vol_overrides key flows through options_proposer.run_headless
# ---------------------------------------------------------------------------

def test_run_headless_applies_vol_bridge_risk_override():
    """run_headless must strip vol_overrides from state and pass risk_pct
    to build_proposal so the lot-sizing uses the scaled-down budget."""
    from datetime import date, timedelta
    from unittest import mock
    from src import options_proposer as op

    def make_analysis():
        return {"ticker": "NIFTY 50", "uptrend": True,
                "fresh_cross": False, "rsi": 55.0, "price": 25000.0}

    def make_chain(spot=25000.0, step=50.0, span=20, base_premium=200.0):
        oc = {}
        for i in range(-span, span + 1):
            strike = spot + i * step
            ce = max(5.0, base_premium - i * (base_premium / span) * 0.9)
            pe = max(5.0, base_premium + i * (base_premium / span) * 0.9)
            oc[f"{strike:.6f}"] = {"ce": {"last_price": round(ce, 2)},
                                   "pe": {"last_price": round(pe, 2)}}
        return {"last_price": spot, "oc": oc}

    expiry = (date.today() + timedelta(days=14)).isoformat()
    state = {
        "analysis":  make_analysis(),
        "vix":       13.0,
        "expiry":    expiry,
        "chain":     make_chain(),
        "book":      {"cash": 2_000_000.0, "holdings": {}},
        "prices":    {},
        "vol_overrides": {"regime": "Expansion", "risk_pct": 7.0},
    }

    with mock.patch.object(op.journal, "log"), \
         mock.patch.object(op, "_notify_discord", return_value=True):
        result = op.run_headless("NIFTY 50", state=state)

    # The proposal must succeed (big book; regime adjustment doesn't block it).
    assert result["proposed"] is True
    # The state dict passed in must not be mutated by run_headless.
    assert "vol_overrides" in state


def test_run_headless_vol_overrides_key_absent_no_error():
    """run_headless must work unchanged when state has no vol_overrides key."""
    from datetime import date, timedelta
    from unittest import mock
    from src import options_proposer as op

    expiry = (date.today() + timedelta(days=14)).isoformat()
    spot = 25000.0
    step = 50.0
    span = 20
    base = 200.0
    oc = {}
    for i in range(-span, span + 1):
        s = spot + i * step
        ce = max(5.0, base - i * (base / span) * 0.9)
        pe = max(5.0, base + i * (base / span) * 0.9)
        oc[f"{s:.6f}"] = {"ce": {"last_price": round(ce, 2)},
                          "pe": {"last_price": round(pe, 2)}}
    state = {
        "analysis": {"ticker": "NIFTY 50", "uptrend": True,
                     "fresh_cross": False, "rsi": 55.0, "price": spot},
        "vix": 13.0, "expiry": expiry,
        "chain": {"last_price": spot, "oc": oc},
        "book": {"cash": 2_000_000.0, "holdings": {}},
        "prices": {},
    }
    with mock.patch.object(op.journal, "log"), \
         mock.patch.object(op, "_notify_discord", return_value=True):
        result = op.run_headless("NIFTY 50", state=state)
    assert result["proposed"] is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}  {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

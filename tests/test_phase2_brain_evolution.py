"""
Hermetic test suite for Phase 1.5 (GraphRAG Resonance Consumer + Forward Placebo
Harness) and Phase 2 (Self-Evolving Brain Map + Cycle Hunter / Auto-Discovery
AD-1 Mahalanobis/Frobenius, AD-2 Motif Significance, AD-3 Registry Enrolment).
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import date, timedelta

import networkx as nx
import pytest

from src import brain_map, graph_engine
from src.analysis import auto_discovery as AD
from src.analysis import macro_features as MF
from src.calibration import mfe_mae_analyzer as mm
from src.knowledge_graph import (
    brain_evolution,
    graphrag,
    graphrag_eval,
    resonance,
    spreading_activation,
)
from src.validation import registry


@pytest.fixture(autouse=True)
def _fast_macro_baseline(monkeypatch):
    monkeypatch.setattr(MF, "Z_BASELINE_SESSIONS", 15)


# ---------------------------------------------------------------------------
# 1. Phase 1.5: Resonance read-only GraphRAG consumer
# ---------------------------------------------------------------------------

def test_resonance_enriches_with_graphrag_2nd_order_context_readonly(tmp_path):
    db_file = tmp_path / "bm.db"
    conn = brain_map.connect(db_file)
    graphrag.ensure_schema(conn)
    graphrag.seed_nodes(conn)
    # Seed a 2-hop supply chain: CRUDE -> ONGC -> TCS
    crude_node = graphrag.resolve_node(conn, "CRUDE", "commodity")
    ongc_node = graphrag.resolve_node(conn, "ONGC", "company")
    tcs_node = graphrag.resolve_node(conn, "TCS", "company")
    graphrag.upsert_graphrag_edge(
        conn, crude_node, "supplies_to", ongc_node,
        edge_class="supply", polarity=1, strength=1.0,
        content_hash="h1", quote="crude supplies ongc",
        extracted_at="2026-07-09T10:00:00+00:00",
    )
    graphrag.upsert_graphrag_edge(
        conn, ongc_node, "partners_with", tcs_node,
        edge_class="competitive", polarity=1, strength=1.0,
        content_hash="h2", quote="ongc partners with tcs",
        extracted_at="2026-07-09T10:00:00+00:00",
    )
    conn.close()

    entries = [{
        "short_id": "eq1",
        "ticker": "TCS",
        "decision": "approved",
        "outcome": None,
        "date": "2026-07-08",
        "signal": "Fresh Golden Cross",
        "price": 3600.0,
        "plan": {"variant": "swing", "stop_loss": 3490.0, "target": 3820.0},
    }]
    event = {
        "target_entity": "CRUDE",
        "event_classification": "supply_shock",
        "directional_bias": 0.9,
        "horizon_impact": "SHORT",
        "confidence_score": 0.9,
    }
    payloads = resonance.evaluate_portfolio_resonance(
        event, None, entries=entries, today=date(2026, 7, 10), db_path=db_file
    )
    assert len(payloads) == 1
    p = payloads[0]
    assert p["graphrag_context"] is not None
    assert p["graphrag_context"]["node_id"] == "TCS"
    assert p["graphrag_context"]["dominant_hop"] == 2
    assert p["graphrag_context"]["energy"] > 0.0
    assert "GraphRAG hop-2:" in p["advisory"]


# ---------------------------------------------------------------------------
# 2. Phase 1.5 / Phase 2: GraphRAG forward outcome pairing & placebo gate
# ---------------------------------------------------------------------------

def test_graphrag_eval_corporate_action_split_adjustment_and_placebo_gate(tmp_path):
    db_file = tmp_path / "eval.db"
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    graphrag.ensure_schema(conn)
    graphrag.seed_nodes(conn)

    # Build a directed graph where Policy:PLI -> TATAMOTORS (+1) and -> INFY (+1)
    g = nx.DiGraph()
    g.add_node("policy:PLI", in_universe=False)
    g.add_node("TATAMOTORS", in_universe=True)
    g.add_node("INFY", in_universe=True)
    g.add_node("HDFCBANK", in_universe=True)
    g.add_node("ICICIBANK", in_universe=True)
    g.add_edge("policy:PLI", "TATAMOTORS", relation="benefits_from",
               edge_weight=0.9, confidence_score=0.9, polarity=1)
    g.add_edge("policy:PLI", "INFY", relation="benefits_from",
               edge_weight=0.85, confidence_score=0.9, polarity=1)
    g.add_edge("policy:PLI", "HDFCBANK", relation="regulates",
               edge_weight=0.8, confidence_score=0.85, polarity=-1)
    g.add_edge("policy:PLI", "ICICIBANK", relation="regulates",
               edge_weight=0.1, confidence_score=0.2, polarity=-1)

    # First check: with only 2 activations, evaluate_promotion_readiness must
    # return status="ACCUMULATING" (< 60 minimum sample gate).
    d0 = date(2026, 1, 1)
    bars_tata = []
    bars_infy = []
    bars_hdfc = []
    for i in range(100):
        day = (d0 + timedelta(days=i)).isoformat()
        # Steadily rising prices for TATAMOTORS and INFY (+1% per day), falling for HDFCBANK
        # On day 20, simulate a 2-for-1 stock split on TATAMOTORS (raw price halves!)
        raw_mult = 0.5 if i >= 20 else 1.0
        bars_tata.append({
            "date": day,
            "open": round(100.0 * (1.01 ** i) * raw_mult, 4),
            "high": round(101.0 * (1.01 ** i) * raw_mult, 4),
            "low": round(99.0 * (1.01 ** i) * raw_mult, 4),
            "close": round(100.0 * (1.01 ** i) * raw_mult, 4),
        })
        bars_infy.append({
            "date": day,
            "open": round(200.0 * (1.008 ** i), 4),
            "high": round(202.0 * (1.008 ** i), 4),
            "low": round(198.0 * (1.008 ** i), 4),
            "close": round(200.0 * (1.008 ** i), 4),
        })
        bars_hdfc.append({
            "date": day,
            "open": round(500.0 * (0.992 ** i), 4),
            "high": round(502.0 * (0.992 ** i), 4),
            "low": round(498.0 * (0.992 ** i), 4),
            "close": round(500.0 * (0.992 ** i), 4),
        })

    split_actions = [{
        "symbol": "TATAMOTORS",
        "ex_date": (d0 + timedelta(days=20)).isoformat(),
        "kind": "split",
        "ratio": 2.0,
    }]
    bars_map = {"TATAMOTORS": bars_tata, "INFY": bars_infy, "HDFCBANK": bars_hdfc}

    # Log 1 activation across the split boundary (day 18 -> day 23 crosses day 20 split)
    spreading_activation.propagate(
        g, {"policy:PLI": 1.0}, conn=conn,
        run_at=(d0 + timedelta(days=18)).isoformat(),
        log_activation=True,
    )
    early_eval = graphrag_eval.evaluate_promotion_readiness(
        conn, g, bars_map, horizon_sessions=5, n_shuffles=20,
        actions=split_actions, rng=random.Random(1),
    )
    assert early_eval["status"] == "ACCUMULATING"
    assert early_eval["ready_for_dept5"] is False
    # Verify the split on day 20 was properly adjusted so TATAMOTORS was a WIN (+5.1%), not a -47% crash!
    tata_pair = [p for p in early_eval["paired"]["pairs"] if p["node_id"] == "TATAMOTORS"][0]
    assert tata_pair["forward_return_pct"] > 4.0
    assert tata_pair["hit"] is True

    # Now log 25 more runs (total 26 runs * 3 activated nodes = 78 paired activations >= 60)
    for step in range(1, 26):
        spreading_activation.propagate(
            g, {"policy:PLI": 1.0}, conn=conn,
            run_at=(d0 + timedelta(days=step * 2)).isoformat(),
            log_activation=True,
        )

    full_eval = graphrag_eval.evaluate_promotion_readiness(
        conn, g, bars_map, horizon_sessions=5, n_shuffles=30,
        actions=split_actions, rng=random.Random(42),
    )
    assert full_eval["paired"]["n_paired"] >= 60
    assert full_eval["paired"]["wilson_lb"] > 0.50
    assert full_eval["placebo"]["p_shuffled"] < 0.05
    assert full_eval["status"] == "READY_FOR_DEPT5_DECISION"
    assert full_eval["ready_for_dept5"] is True
    conn.close()


# ---------------------------------------------------------------------------
# 3. Phase 2: Cycle Hunter / Auto-Discovery (Mahalanobis, Frobenius, Motifs, AD-3)
# ---------------------------------------------------------------------------

def _write_lake_series(lake, key, pairs):
    lake.mkdir(parents=True, exist_ok=True)
    body = "date,value\n" + "".join(f"{d},{v}\n" for d, v in pairs)
    (lake / f"{key}.csv").write_text(body)


def test_mahalanobis_and_frobenius_detect_regime_breaks(tmp_path):
    lake = tmp_path / "macro"
    d0 = date(2020, 1, 1)
    n = 90
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(n)]
    for c, base, jump in (
        ("BRENT", 100.0, -22.0),
        ("DXY", 100.0, 8.0),
        ("USDINR", 75.0, 5.0),
        ("US10Y", 2.0, -0.7),
    ):
        vals = []
        for i in range(n):
            wobble = 0.15 * ((i % 5) - 2) if c != "USDINR" else -0.15 * ((i % 5) - 2)
            v = base + wobble
            if i >= 65:
                v += jump * (1 if i % 2 == 0 else 0.8)
            vals.append((dates[i], round(v, 4)))
        _write_lake_series(lake, c, vals)

    m_cands = AD.rank_shock_candidates(
        lake_dir=lake, top_n=3, min_gap_days=10, stat="mahalanobis"
    )
    assert m_cands
    assert m_cands[0]["stat"] == "mahalanobis_z20"
    assert m_cands[0]["stress"] > 1.0

    f_cands = AD.rank_shock_candidates(
        lake_dir=lake, top_n=3, min_gap_days=10, stat="frobenius"
    )
    assert f_cands
    assert f_cands[0]["stat"] == "frobenius_corr_break"
    assert f_cands[0]["stress"] > 0.0


def test_ad2_motif_significance_admits_true_repeat_and_enrols_in_registry(tmp_path):
    lake = tmp_path / "macro"
    d0 = date(2018, 1, 1)
    # Construct a distinctive multi-channel 12-day motif repeated in train and held-out tail
    rng = random.Random(7)
    motif_a = [0.0, 1.5, 3.2, 5.0, 2.5, -1.0, -3.5, -5.0, -2.0, 1.0, 2.8, 0.5]
    motif_b = [0.0, -2.0, -4.0, -1.5, 2.0, 4.5, 3.0, 0.5, -2.5, -4.5, -1.0, 0.2]
    n = 110
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(n)]

    for idx_c, (c, base) in enumerate((
        ("BRENT", 100.0), ("DXY", 100.0), ("USDINR", 75.0), ("US10Y", 20.0)
    )):
        template = motif_a if idx_c % 2 == 0 else motif_b
        seq = []
        for i in range(n):
            # Place motif at [25..36] and again in held-out tail at [75..86]
            if 25 <= i < 25 + len(template):
                val = base + template[i - 25]
            elif 75 <= i < 75 + len(template):
                val = base + template[i - 75]
            else:
                val = base + rng.gauss(0, 0.35) + 0.2 * ((i * (idx_c + 1)) % 5)
            seq.append((dates[i], round(val, 4)))
        _write_lake_series(lake, c, seq)

    cand = {
        "kind": "motif",
        "a": [dates[25], dates[36]],
        "b": [dates[75], dates[86]],
        "z_window": 3,
    }
    verdict = AD.significance_gate(
        cand, lake_dir=lake, n_surrogates=30, alpha=0.10, rng=random.Random(11)
    )
    assert verdict["kind"] == "motif"
    assert verdict["observed_dtw"] < 0.25
    assert verdict["stability_ok"] is True
    assert verdict["held_out_confirmed"] is True
    assert verdict["admitted"] is True

    # Route to court with a DB connection -> verifies AD-3 registry enrolment
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    out_ep = tmp_path / "discovered_episodes.json"
    court_doc = AD.route_to_court([verdict], out_path=out_ep, conn=conn)
    assert court_doc["n_admitted"] == 1
    assert len(court_doc["registry_enrolments"]) == 1
    enrol = court_doc["registry_enrolments"][0]
    assert enrol["status"] == "CANDIDATE"
    assert enrol["created"] is True

    # Re-enrolling the same candidate is idempotent (created=False)
    again = AD.enrol_in_registry([verdict], conn)
    assert again[0]["pattern_id"] == enrol["pattern_id"]
    assert again[0]["created"] is False
    conn.close()


# ---------------------------------------------------------------------------
# 4. Phase 2: Self-Evolving Brain Map (Apex Optimizer, "You Are Here", Bayesian + Brier)
# ---------------------------------------------------------------------------

def test_mfe_mae_expectancy_optimizer_and_cluster_conditioning():
    records = (
        [{"mfe_pct": 6.0, "mae_pct": 1.2, "pnl": 200.0, "strategy": "bull_call_spread"}
         for _ in range(18)]
        + [{"mfe_pct": 1.5, "mae_pct": 4.5, "pnl": -150.0, "strategy": "bull_call_spread"}
           for _ in range(7)]
        + [{"mfe_pct": 4.0, "mae_pct": 1.0, "pnl": 100.0, "strategy": "bear_put_spread"}
           for _ in range(5)]
    )
    summary = mm.summarize(records, min_samples=20)
    exp_apex = summary["expectancy_apex"]
    assert exp_apex["status"] == "ok"
    assert exp_apex["optimal_take_profit_pct"] > 0
    assert exp_apex["optimal_stop_loss_pct"] > 0
    assert exp_apex["net_expectancy_pct"] > 0
    # Cluster conditioning: bull_call_spread has 25 trades (>= 20) -> ok;
    # bear_put_spread has 5 trades (< 20) -> insufficient_data
    clusters = summary["by_strategy_apex"]
    assert clusters["bull_call_spread"]["expectancy_apex"]["status"] == "ok"
    assert clusters["bear_put_spread"]["expectancy_apex"]["status"] == "insufficient_data"


def test_locate_in_lifecycle_you_are_here_coordinate():
    # Build a 20-step reference template trajectory
    template_rows = [
        {"BRENT:z20": round(math.sin(i / 3.0) * 2.0, 4),
         "DXY:z20": round(math.cos(i / 3.0) * 1.5, 4)}
        for i in range(20)
    ]
    import math as _m  # noqa: F401
    # Slice indices 8..12 (ending at index 12 of 19 -> lifecycle_pct = 12/19 = 0.6316 -> peak_stress)
    live_window = [dict(r) for r in template_rows[8:13]]
    loc = brain_evolution.locate_in_lifecycle(
        live_window,
        {"archetype_A1": template_rows},
        sim_floor=0.70,
    )
    assert loc["declared"] is True
    assert loc["best_template"] == "archetype_A1"
    assert loc["matched_start_idx"] == 8
    assert loc["matched_end_idx"] == 12
    assert loc["lifecycle_pct"] == round(12 / 19, 4)
    assert loc["phase"] == "peak_stress"
    assert loc["remaining_sessions"] == 7
    assert loc["similarity"] == 1.0


def test_bayesian_confidence_time_decay_and_provenance_firewall():
    outcomes = [
        # Recent real wins
        {"date": "2026-10-25", "result": "win", "journal_ref": "real001"},
        {"date": "2026-10-26", "result": "win", "journal_ref": "real002"},
        {"date": "2026-10-27", "result": "win", "journal_ref": "real003"},
        # Old loss 360 days ago (decayed to 0.5^4 = 0.0625 weight)
        {"date": "2025-10-30", "result": "loss", "journal_ref": "real000"},
        # Simulated / shadow / hypothetical rows -> must be ignored by default!
        {"date": "2026-10-27", "result": "loss", "journal_ref": "sim:abc"},
        {"date": "2026-10-27", "result": "loss", "journal_ref": "shadow:xyz"},
        {"date": "2026-10-27", "result": "loss", "journal_ref": "real004", "hypothetical": True},
        # Future row after as_of -> must be ignored (future-blind!)
        {"date": "2026-11-15", "result": "loss", "journal_ref": "real999"},
    ]
    post = brain_evolution.compute_bayesian_confidence(
        outcomes, as_of="2026-10-28", half_life_days=90.0
    )
    assert post["raw_wins"] == 3
    assert post["raw_losses"] == 1
    assert post["posterior_mean"] > 0.60


def test_brier_calibration_ship_gate():
    # 1. Below 30 samples -> refused
    thin = brain_evolution.calibration_ship_gate([(0.8, 1)] * 10, min_samples=30)
    assert thin["ship_allowed"] is False
    assert thin["status"] == "insufficient_oos_samples"

    # 2. Uncalibrated / anti-predictive probabilities -> refused
    bad_preds = [(0.85, 0) for _ in range(20)] + [(0.15, 1) for _ in range(15)]
    bad = brain_evolution.calibration_ship_gate(bad_preds, min_samples=30)
    assert bad["ship_allowed"] is False
    assert bad["status"] == "calibration_rejected"

    # 3. Sharp, well-calibrated probabilities -> approved
    good_preds = [(0.85, 1) for _ in range(20)] + [(0.15, 0) for _ in range(15)]
    good = brain_evolution.calibration_ship_gate(good_preds, min_samples=30)
    assert good["ship_allowed"] is True
    assert good["status"] == "calibrated"
    assert good["brier_skill_score"] > 0.5

"""AltData edge-class proving court: pairing, placebo, graduation, promotion."""

import datetime as dt
import importlib.util
import json
import random
import sqlite3
from pathlib import Path

import pytest

from src.analysis import altdata_graph as ag
from src.knowledge_graph import graphrag_eval as ge

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "run_proving_court_script", ROOT / "scripts" / "run_proving_court.py")
court = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(court)

POOL = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
START = dt.date(2025, 1, 1)


def _day(i):
    return (START + dt.timedelta(days=i)).isoformat()


def _walk_bars(n_sessions=420, seed=7):
    rng = random.Random(seed)
    out = {}
    for t in POOL:
        px, bars = 100.0, []
        for i in range(n_sessions):
            o = px
            px = max(1.0, px * (1 + rng.gauss(0, 0.02)))
            bars.append({"session": _day(i), "open": o, "close": px})
        out[t] = bars
    return out


def _oracle_activations(bars, relation="WON_CONTRACT", every=5, count=80, h=5):
    """Each date picks the pool ticker with the best forward return: a real edge."""
    acts = []
    for k in range(count):
        d = _day(k * every)
        best = max(POOL, key=lambda t: ge.forward_entry(bars[t], d, h)[1])
        acts.append({"date": d, "ticker": best, "relation": relation, "sign": 1})
    return acts


def _random_activations(relation, count=80, every=5, seed=3):
    rng = random.Random(seed)
    return [{"date": _day(k * every), "ticker": rng.choice(POOL),
             "relation": relation, "sign": rng.choice((1, -1))}
            for k in range(count)]


def _graph_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ag.ensure_altdata_graph_schema(conn)
    return conn


def _edge(conn, src, rel, tgt, day="2025-01-02"):
    ag._upsert_graph_edge_and_nodes(
        conn, source_node=src, source_type="company", relation=rel,
        target_node=tgt, target_type="policy", weight=1.0, confidence_score=0.5,
        polarity=1, edge_class="demand", context_dict={}, valid_from=day)
    conn.commit()


def _sources(conn):
    return {(r["source_node"], r["relation"]): (r["source"], r["extractor"])
            for r in conn.execute("SELECT * FROM graph_edges")}


def test_forward_entry_enters_next_session_open_and_needs_a_full_window():
    bars = [{"session": _day(i), "open": 100 + i, "close": 101 + i} for i in range(10)]
    idx, ret = ge.forward_entry(bars, _day(2), horizon_sessions=5)
    assert idx == 3
    assert ret == pytest.approx((101 + 7 - 103) / 103 * 100)
    assert ge.forward_entry(bars, _day(6), horizon_sessions=5) is None
    bars[3]["open"] = None
    assert ge.forward_entry(bars, _day(2), horizon_sessions=5) is None


def test_overlapping_activations_on_one_ticker_count_once_per_window():
    bars = _walk_bars(60)
    acts = [{"date": _day(i), "ticker": "AAA.NS", "relation": "R", "sign": 1}
            for i in range(10)]
    cls = ge.pair_altdata_activations_with_outcomes(acts, bars, 5)["R"]
    assert cls["n"] == 2
    assert cls["skipped_overlap"] == 8


def test_real_edge_graduates_and_placebo_is_significant():
    bars = _walk_bars()
    v = ge.evaluate_altdata_edge_classes(_oracle_activations(bars), bars,
                                         n_shuffles=200)["WON_CONTRACT"]
    assert v["n"] >= 60
    assert v["status"] == "GRADUATES" and v["graduates"]
    assert v["wilson_lb"] > 0.5
    assert v["sharpe_annualised"] > 0
    assert v["placebo"]["p_value"] < 0.05
    assert v["placebo"]["placebo_mean_hit_rate"] < v["hit_rate"]


def test_random_edge_is_rejected():
    bars = _walk_bars()
    v = ge.evaluate_altdata_edge_classes(
        _random_activations("INPUT_COST_PRESSURE"), bars,
        n_shuffles=200)["INPUT_COST_PRESSURE"]
    assert v["n"] >= 60
    assert v["status"] == "REJECTED" and not v["graduates"]


def test_thin_class_accumulates_without_a_placebo():
    bars = _walk_bars()
    v = ge.evaluate_altdata_edge_classes(_oracle_activations(bars, count=30),
                                         bars)["WON_CONTRACT"]
    assert v["status"] == "ACCUMULATING"
    assert v["placebo"] is None


def test_small_placebo_pool_blocks_graduation():
    bars = {t: b for t, b in _walk_bars().items() if t in POOL[:3]}
    acts = [{"date": _day(k * 5), "ticker": "AAA", "relation": "R", "sign": 1}
            for k in range(70)]
    v = ge.evaluate_altdata_edge_classes(acts, bars, n_shuffles=50)["R"]
    assert not v["graduates"]
    assert v["placebo"]["reason"] == "placebo_pool_too_small"


def test_alpha_is_split_across_classes_that_reached_n():
    bars = _walk_bars()
    acts = _oracle_activations(bars) + _random_activations("VEHICLE_SALES_MOMENTUM")
    vs = ge.evaluate_altdata_edge_classes(acts, bars, n_shuffles=50)
    assert vs["WON_CONTRACT"]["alpha_bonferroni"] == pytest.approx(0.025)


def test_load_activations_needs_an_altdata_edge_and_a_nonzero_sign():
    conn = _graph_conn()
    _edge(conn, "AAA.NS", "WON_CONTRACT", "MINISTRY_OF_POWER")
    for d, t, sig, m in [("2025-01-02", "AAA.NS", "WON_CONTRACT", 0.4),
                         ("2025-01-03", "AAA.NS", "WON_CONTRACT", 0.0),
                         ("2025-01-02", "BBB.NS", "WON_CONTRACT", 0.4)]:
        ag._upsert_momentum_modifier(conn, d, t, sig, "X", None, 1.0, m, {}, d)
    acts = ge.load_altdata_activations(conn)
    assert acts == [{"date": "2025-01-02", "ticker": "AAA",
                     "relation": "WON_CONTRACT", "sign": 1}]


def test_promotion_flips_only_the_graduated_class_and_revoke_restores():
    conn = _graph_conn()
    _edge(conn, "AAA.NS", "WON_CONTRACT", "MINISTRY_OF_POWER")
    _edge(conn, "Sugar", "INPUT_COST_PRESSURE", "BBB.NS")
    with pytest.raises(ValueError):
        ge.promote_edge_class(conn, {"relation": "WON_CONTRACT", "graduates": False}, "t")
    n = ge.promote_edge_class(conn, {"relation": "WON_CONTRACT", "graduates": True}, "t0")
    assert n == 1
    s = _sources(conn)
    assert s[("AAA.NS", "WON_CONTRACT")] == ("outcome_derived", "altdata_graph")
    assert s[("Sugar", "INPUT_COST_PRESSURE")][0] == "altdata_telemetry"
    assert ge.promoted_relations(conn) == {"WON_CONTRACT"}
    assert ge.revoke_edge_class(conn, "WON_CONTRACT") == 1
    assert _sources(conn)[("AAA.NS", "WON_CONTRACT")][0] == "altdata_telemetry"
    assert ge.promoted_relations(conn) == set()


def test_injector_keeps_a_promoted_class_promoted():
    conn = _graph_conn()
    _edge(conn, "AAA.NS", "WON_CONTRACT", "MINISTRY_OF_POWER")
    ge.promote_edge_class(conn, {"relation": "WON_CONTRACT", "graduates": True}, "t0")
    _edge(conn, "AAA.NS", "WON_CONTRACT", "MINISTRY_OF_POWER", day="2025-02-01")
    _edge(conn, "CCC.NS", "WON_CONTRACT", "MINISTRY_OF_DEFENCE", day="2025-02-01")
    _edge(conn, "Onions", "INPUT_COST_PRESSURE", "DDD.NS", day="2025-02-01")
    s = _sources(conn)
    assert s[("AAA.NS", "WON_CONTRACT")][0] == "outcome_derived"
    assert s[("CCC.NS", "WON_CONTRACT")][0] == "outcome_derived"
    assert s[("Onions", "INPUT_COST_PRESSURE")][0] == "altdata_telemetry"


def _court_db(bars):
    conn = _graph_conn()
    for a in _oracle_activations(bars):
        t = a["ticker"] + ".NS"
        _edge(conn, t, "WON_CONTRACT", "MINISTRY_OF_POWER", day=a["date"])
        ag._upsert_momentum_modifier(conn, a["date"], t, "WON_CONTRACT",
                                     "MINISTRY_OF_POWER", None, 1.0, 0.5, {}, a["date"])
    conn.commit()
    return conn


def test_run_court_reports_without_promoting_by_default(tmp_path):
    bars = _walk_bars()
    conn = _court_db(bars)
    ledger = tmp_path / "court.jsonl"
    rep = court.run_court(conn, bars_loader=lambda syms: bars, n_shuffles=100,
                          ledger_path=ledger, now_iso="t0")
    assert rep["verdicts"]["WON_CONTRACT"]["status"] == "GRADUATES"
    assert rep["promoted_now"] == {}
    assert {v[0] for v in _sources(conn).values()} == {"altdata_telemetry"}
    assert json.loads(ledger.read_text().splitlines()[0])["run_at"] == "t0"


def test_run_court_promote_flag_promotes_once(tmp_path):
    bars = _walk_bars()
    conn = _court_db(bars)
    ledger = tmp_path / "court.jsonl"
    rep = court.run_court(conn, bars_loader=lambda syms: bars, promote=True,
                          n_shuffles=100, ledger_path=ledger, now_iso="t0")
    assert rep["promoted_now"]["WON_CONTRACT"] >= 1
    assert {v[0] for v in _sources(conn).values()} == {"outcome_derived"}
    rep2 = court.run_court(conn, bars_loader=lambda syms: bars, promote=True,
                           n_shuffles=100, ledger_path=ledger, now_iso="t1")
    assert rep2["promoted_now"] == {}
    assert rep2["already_promoted"] == ["WON_CONTRACT"]
    assert len(ledger.read_text().splitlines()) == 2

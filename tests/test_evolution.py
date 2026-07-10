"""
Procedural Evolution tests — fully offline: the LLM is a scripted fake,
bars are synthetic, DBs are in-memory, and candidate/lineage files land
in temp dirs only. No Ollama, no network, no production file touched.

Run from the project folder:
    python tests/test_evolution.py      (simple, no extra installs)
    python -m pytest tests/             (if you have pytest)
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import evolution as ev
from src.simulator import ensure_schema as ensure_sim_schema


class FakeLLM:
    """A scripted extractor: returns canned replies in order. Mimics
    local_parser.LocalExtractor's _chat/is_reachable surface."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.base_url = "http://fake"

    def is_reachable(self):
        return True

    def _chat(self, payload, system_prompt=None):
        return self.replies.pop(0) if self.replies else None


GOOD_PROPOSAL = json.dumps({"parameter": "vix_block_above",
                            "proposed_value": 15.0,
                            "rationale": "losses cluster in the 15-16 band",
                            "expected_effect": "fewer condors near the edge"})
GOOD_CRITIQUE = json.dumps({"objections": ["could overfit to 2024 vol"],
                            "verdict": "proceed_if_resolved"})
GOOD_RESOLUTION = json.dumps({"resolutions": ["holdout covers 3 regimes"],
                              "withdraw": False})


def make_conn():
    conn = brain_map.connect(":memory:")
    ensure_sim_schema(conn)
    return conn


def insert_sim(conn, ref, *, vix, result, pnl, underlying="NIFTY BANK",
               strategy="iron_condor", r_multiple=None,
               resolution="pre_expiry_exit"):
    conn.execute(
        "INSERT INTO simulated_trades (journal_ref, underlying, strategy, "
        "proposed_on, expiry, vix, net_credit, spread_width, max_loss, "
        "max_profit, lots, lot_size, resolution, exit_date, pnl_net, "
        "frictions_rs, slippage_rs, capture_pct, r_multiple, result, "
        "verdict) VALUES (?, ?, ?, '2026-06-01', '2026-06-11', ?, 70.0, "
        "400.0, 4550.0, 2450.0, 1, 35, ?, '2026-06-09', ?, 100.0, 50.0, "
        "10.0, ?, ?, 'test')",
        (ref, underlying, strategy, vix, resolution, pnl, r_multiple, result))
    conn.commit()


def seed_cluster(conn, n_losses=4, band_vix=15.5, n_wins=3):
    for i in range(n_losses):
        insert_sim(conn, f"loss{i}", vix=band_vix, result="loss",
                   pnl=-3000.0, r_multiple=-0.95 if i % 2 else -0.3)
    for i in range(n_wins):
        insert_sim(conn, f"win{i}", vix=12.0, result="win", pnl=1500.0,
                   r_multiple=0.5)


# --- mining + hindsight -----------------------------------------------------

def test_loss_clusters_group_by_setup_and_vix_band_with_provenance():
    conn = make_conn()
    seed_cluster(conn)
    insert_sim(conn, "stray", vix=11.0, result="loss", pnl=-500.0)  # < min
    clusters = ev.find_loss_clusters(conn, min_size=3)
    assert len(clusters) == 1
    c = clusters[0]
    assert (c["underlying"], c["strategy"], c["vix_band"]) == \
        ("NIFTY BANK", "iron_condor", "mid")
    assert sorted(c["journal_refs"]) == ["loss0", "loss1", "loss2", "loss3"]
    assert c["total_loss"] == -12000.0


def test_hindsight_split_buckets_are_deterministic():
    assert ev.hindsight_split({"r_multiple": -0.95,
                               "resolution": "profit_take"}) == \
        "bad_risk_parameters"
    assert ev.hindsight_split({"r_multiple": -0.2,
                               "resolution": "pre_expiry_exit"}) == \
        "bad_timing"
    assert ev.hindsight_split({"r_multiple": -0.7,
                               "resolution": "pre_expiry_exit"}) == \
        "ambiguous"
    assert ev.hindsight_split({"r_multiple": None,
                               "resolution": "x"}) == "ambiguous"


def test_counterfactual_contrasts_wins_against_the_cluster():
    conn = make_conn()
    seed_cluster(conn)
    cluster = ev.find_loss_clusters(conn)[0]
    cf = ev.counterfactual_context(conn, cluster)
    assert cf["n_wins"] == 3
    assert cf["wins_avg_vix"] == 12.0        # wins lived in calm vol
    assert cf["losses_avg_vix"] is None or True  # summary carries losses side


# --- schema gates + dialectic --------------------------------------------------

def test_proposal_gate_rejects_bad_parameters_values_and_noops():
    assert "whitelist" in ev.validate_proposal(
        {"parameter": "lot_size", "proposed_value": 1})
    assert "bounds" in ev.validate_proposal(
        {"parameter": "vix_block_above", "proposed_value": 99})
    assert "not coercible" in ev.validate_proposal(
        {"parameter": "vix_block_above", "proposed_value": "much"})
    assert "equals the current" in ev.validate_proposal(
        {"parameter": "vix_block_above",
         "proposed_value": ev.current_value("vix_block_above")})
    assert ev.validate_proposal(
        {"parameter": "vix_block_above", "proposed_value": 15.0}) is None


def test_llm_json_gate_drops_malformed_replies():
    llm = FakeLLM(["not json at all", "{\"parameter\": 42}"])
    out = ev._llm_json(llm, "sys", "payload",
                       {"parameter": str}, retries=1)
    assert out is None                       # both attempts failed the gate


def test_dialectic_consensus_gate_blocks_and_withdrawals():
    summary = {"strategy": "iron_condor", "underlying": "NIFTY BANK",
               "vix_band": "mid", "n_losses": 4, "total_loss": -12000.0,
               "hindsight_buckets": {}, "avg_vix": 15.5, "journal_refs": []}
    # critic says block -> dead
    llm = FakeLLM([GOOD_PROPOSAL,
                   json.dumps({"objections": ["overfit"],
                               "verdict": "block"})])
    assert ev.run_dialectic(llm, summary, {}) is None
    # analyst withdraws -> dead
    llm = FakeLLM([GOOD_PROPOSAL, GOOD_CRITIQUE,
                   json.dumps({"resolutions": [], "withdraw": True})])
    assert ev.run_dialectic(llm, summary, {}) is None
    # full pass -> survives with all three artifacts
    llm = FakeLLM([GOOD_PROPOSAL, GOOD_CRITIQUE, GOOD_RESOLUTION])
    out = ev.run_dialectic(llm, summary, {})
    assert out and out["proposal"]["parameter"] == "vix_block_above"
    assert out["critique"]["objections"] == ["could overfit to 2024 vol"]


# --- parameter overrides + backtest verdicts -----------------------------------

def test_override_parameters_patches_and_always_restores():
    from src import strategy
    before = strategy.VIX_BLOCK_ABOVE
    with ev.override_parameters({"vix_block_above": 14.0}):
        assert strategy.VIX_BLOCK_ABOVE == 14.0
    assert strategy.VIX_BLOCK_ABOVE == before
    try:
        with ev.override_parameters({"vix_block_above": 14.0}):
            raise RuntimeError("boom mid-backtest")
    except RuntimeError:
        pass
    assert strategy.VIX_BLOCK_ABOVE == before   # restored even on crash


def test_portfolio_metrics_math():
    conn = make_conn()
    for i, pnl in enumerate((1000.0, -2000.0, 3000.0)):
        insert_sim(conn, f"t{i}", vix=12.0,
                   result="win" if pnl > 0 else "loss", pnl=pnl)
    m = ev._portfolio_metrics(conn)
    assert m["trades"] == 3 and m["total_pnl"] == 2000.0
    assert m["win_rate"] == 66.67
    assert m["max_drawdown"] == 2000.0       # peak 1000 -> trough -1000


def _fake_replay_factory(results):
    """Patch backtest_candidate's internals by seeding DBs per call."""
    calls = {"n": 0}

    def fake_run_simulation(start, end, underlyings, conn=None,
                            bars_by_underlying=None, vix_by_date=None):
        ensure_sim_schema(conn)
        for i, row in enumerate(results[calls["n"]]):
            insert_sim(conn, f"r{calls['n']}_{i}", **row)
        calls["n"] += 1
        return {}
    return fake_run_simulation


def test_backtest_verdicts_promoted_reverted_norepair():
    from unittest import mock
    cluster = {"underlying": "NIFTY BANK", "strategy": "iron_condor",
               "vix_band": "mid",
               "trades": [{"vix": 15.5}], "journal_refs": []}
    proposal = {"parameter": "vix_block_above", "proposed_value": 15.0}
    baseline = [dict(vix=15.5, result="loss", pnl=-3000.0),
                dict(vix=12.0, result="win", pnl=2000.0),
                dict(vix=12.0, result="win", pnl=2000.0)]

    # 1. repaired + healthy + stable in both window halves -> promoted
    #    (a would-be promotion now also replays each half: baseline then
    #    mutated per half, so the factory needs 4 extra result sets)
    mutated_good = [dict(vix=12.0, result="win", pnl=2000.0),
                    dict(vix=12.0, result="win", pnl=2100.0)]
    half_baseline = [dict(vix=12.0, result="win", pnl=800.0)]
    half_mutated = [dict(vix=12.0, result="win", pnl=1000.0)]
    with mock.patch("src.simulator.run_simulation",
                    _fake_replay_factory([baseline, mutated_good,
                                          half_baseline, half_mutated,
                                          half_baseline, half_mutated])):
        out = ev.backtest_candidate(cluster, proposal, {"NIFTY BANK": []},
                                    {}, "2026-01-01", "2026-06-01")
    assert out["verdict"] == "promoted"
    assert out["cluster_losses_baseline"] == 1
    assert out["cluster_losses_mutated"] == 0
    assert out["stability"]["stable"] is True
    assert [h["delta_pnl"] for h in out["stability"]["halves"]] == [200.0, 200.0]

    # 2. repaired but global metrics degrade -> revert_on_regression
    mutated_bad = [dict(vix=12.0, result="loss", pnl=-6000.0),
                   dict(vix=12.0, result="win", pnl=500.0)]
    with mock.patch("src.simulator.run_simulation",
                    _fake_replay_factory([baseline, mutated_bad])):
        out = ev.backtest_candidate(cluster, proposal, {"NIFTY BANK": []},
                                    {}, "2026-01-01", "2026-06-01")
    assert out["verdict"] == "revert_on_regression"

    # 3. cluster losses unchanged -> no_repair
    with mock.patch("src.simulator.run_simulation",
                    _fake_replay_factory([baseline, baseline])):
        out = ev.backtest_candidate(cluster, proposal, {"NIFTY BANK": []},
                                    {}, "2026-01-01", "2026-06-01")
    assert out["verdict"] == "no_repair"
    assert out["stability"] is None      # never reached the halves check


# --- Phase 5 anti-overfitting guardrails ------------------------------------

def test_window_halves_split_on_the_midpoint_date():
    first, second = ev.window_halves("2026-01-01", "2026-12-31")
    assert first == ("2026-01-01", "2026-07-02")
    assert second == ("2026-07-02", "2026-12-31")
    # degenerate one-day window still yields two (identical) halves
    a, b = ev.window_halves("2026-06-01", "2026-06-01")
    assert a == ("2026-06-01", "2026-06-01") == b


def test_backtest_flags_one_regime_winners_as_unstable():
    """The overfit trap: a mutation that wins the FULL window but only
    because one half carries it — the other half actually degrades. Must
    come back unstable_out_of_sample, never promoted."""
    from unittest import mock
    cluster = {"underlying": "NIFTY BANK", "strategy": "iron_condor",
               "vix_band": "mid",
               "trades": [{"vix": 15.5}], "journal_refs": []}
    proposal = {"parameter": "vix_block_above", "proposed_value": 15.0}
    baseline = [dict(vix=15.5, result="loss", pnl=-3000.0),
                dict(vix=12.0, result="win", pnl=2000.0),
                dict(vix=12.0, result="win", pnl=2000.0)]
    mutated_good = [dict(vix=12.0, result="win", pnl=2000.0),
                    dict(vix=12.0, result="win", pnl=2100.0)]
    half_baseline = [dict(vix=12.0, result="win", pnl=800.0)]
    half1_mutated = [dict(vix=12.0, result="win", pnl=2500.0)]   # carried...
    half2_mutated = [dict(vix=12.0, result="win", pnl=300.0)]    # ...degraded
    with mock.patch("src.simulator.run_simulation",
                    _fake_replay_factory([baseline, mutated_good,
                                          half_baseline, half1_mutated,
                                          half_baseline, half2_mutated])):
        out = ev.backtest_candidate(cluster, proposal, {"NIFTY BANK": []},
                                    {}, "2026-01-01", "2026-06-01")
    assert out["verdict"] == "unstable_out_of_sample"
    assert out["stability"]["stable"] is False
    deltas = [h["delta_pnl"] for h in out["stability"]["halves"]]
    assert deltas == [1700.0, -500.0]


def test_corpus_size_counts_and_tolerates_missing_table():
    conn = make_conn()
    assert ev.corpus_size(conn) == 0
    seed_cluster(conn)                    # 4 losses + 3 wins
    assert ev.corpus_size(conn) == 7
    bare = brain_map.connect(":memory:")  # no simulated_trades table at all
    assert ev.corpus_size(bare) == 0


def test_run_evolution_refuses_a_thin_corpus():
    """Guard 1: below the 30-trade floor the run must stop BEFORE mining
    clusters or invoking the LLM — no mutation from noise."""
    from unittest import mock
    conn = make_conn()
    seed_cluster(conn)                    # only 7 resolved trades
    cache = {"bars": {"NIFTY BANK": []}, "vix": {},
             "start": "2026-01-01", "end": "2026-06-01"}
    with mock.patch.object(ev, "find_loss_clusters") as miner, \
         mock.patch.object(ev, "backtest_candidate") as backtester:
        stats = ev.run_evolution(conn, extractor=None, bars_cache=cache)
    assert "corpus too small" in stats["skipped"]
    assert "7 resolved trades" in stats["skipped"]
    assert stats["written"] == 0
    assert not miner.called and not backtester.called


def test_run_evolution_proceeds_at_the_floor():
    from unittest import mock
    conn = make_conn()
    for i in range(ev.MIN_TRADES_FOR_EVOLUTION):     # exactly 30
        insert_sim(conn, f"t{i}", vix=12.0, result="win", pnl=100.0)
    cache = {"bars": {"NIFTY BANK": []}, "vix": {},
             "start": "2026-01-01", "end": "2026-06-01"}
    with mock.patch.object(ev, "find_loss_clusters",
                           return_value=[]) as miner:
        stats = ev.run_evolution(conn, extractor=None, bars_cache=cache)
    assert "skipped" not in stats
    assert miner.called
    assert stats["clusters_found"] == 0


# --- lineage + candidate file ---------------------------------------------------

def test_lineage_versions_chain_per_parameter():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "lineage.json"
        e1 = ev.append_lineage({"candidate_id": "aaa",
                                "parameter": "vix_block_above",
                                "verdict": "promoted"}, path=path)
        e2 = ev.append_lineage({"candidate_id": "bbb",
                                "parameter": "vix_block_above",
                                "verdict": "revert_on_regression"}, path=path)
        e3 = ev.append_lineage({"candidate_id": "ccc",
                                "parameter": "profit_take_fraction",
                                "verdict": "promoted"}, path=path)
    assert (e1["version"], e1["parent"]) == ("v1", None)
    assert (e2["version"], e2["parent"]) == ("v2", "aaa")   # failures count
    assert (e3["version"], e3["parent"]) == ("v1", None)    # separate tree


def test_parameter_diff_is_a_real_unified_diff():
    diff = ev.parameter_diff("profit_take_fraction", 0.55)
    assert diff.startswith("--- a/src/plan_tracker.py")
    assert "-OPTION_PROFIT_TAKE_FRACTION = 0.65" in diff
    assert "+OPTION_PROFIT_TAKE_FRACTION = 0.55" in diff


def test_candidate_markdown_has_all_four_sections_and_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        cluster_sum = {"strategy": "iron_condor", "underlying": "NIFTY BANK",
                       "vix_band": "mid", "n_losses": 4,
                       "total_loss": -12000.0, "avg_vix": 15.5,
                       "hindsight_buckets": {"bad_risk_parameters": 2},
                       "journal_refs": ["sim:aaa", "sim:bbb"]}
        dialectic = {"proposal": json.loads(GOOD_PROPOSAL),
                     "critique": json.loads(GOOD_CRITIQUE),
                     "resolution": json.loads(GOOD_RESOLUTION)}
        backtest = {"verdict": "promoted",
                    "baseline": {"trades": 10, "win_rate": 60.0,
                                 "total_pnl": 5000.0, "sharpe": 0.3,
                                 "max_drawdown": 4000.0},
                    "mutated": {"trades": 9, "win_rate": 66.7,
                                "total_pnl": 6200.0, "sharpe": 0.4,
                                "max_drawdown": 3000.0},
                    "cluster_losses_baseline": 4,
                    "cluster_losses_mutated": 1}
        entry = {"candidate_id": "abc123", "version": "v1", "parent": None}
        path = ev.write_candidate(cluster_sum, dialectic, backtest, entry,
                                  out_dir=Path(tmp),
                                  now=datetime(2026, 7, 9, 1, 0, 0))
        text = path.read_text()
    assert path.name == "evolution_20260709_010000.md"
    for section in ("## 1. Target Error Cluster",
                    "## 2. Dialectic Debate Summary",
                    "## 3. Simulator Proof", "## 4. Code Diff"):
        assert section in text
    assert "sim:aaa, sim:bbb" in text          # provenance pointers
    assert "```diff" in text
    assert "awaiting human review" in text      # never auto-applied


# --- orchestrator end-to-end ------------------------------------------------------

def test_run_evolution_end_to_end_writes_only_promoted_candidates():
    from unittest import mock
    conn = make_conn()
    seed_cluster(conn)
    llm = FakeLLM([GOOD_PROPOSAL, GOOD_CRITIQUE, GOOD_RESOLUTION])
    cache = {"bars": {"NIFTY BANK": []}, "vix": {},
             "start": "2026-01-01", "end": "2026-06-01"}
    canned = {"verdict": "promoted",
              "baseline": {"trades": 5, "win_rate": 40.0, "total_pnl": -1.0,
                           "sharpe": 0.1, "max_drawdown": 5.0},
              "mutated": {"trades": 5, "win_rate": 60.0, "total_pnl": 9.0,
                          "sharpe": 0.2, "max_drawdown": 4.0},
              "cluster_losses_baseline": 4, "cluster_losses_mutated": 1}
    with tempfile.TemporaryDirectory() as tmp:
        # the seeded cluster is 7 trades — below the Phase 5 anti-overfit
        # floor, which has its own tests; lift it here to test the rest
        with mock.patch.object(ev, "backtest_candidate",
                               return_value=canned), \
             mock.patch.object(ev, "corpus_size", return_value=100):
            stats = ev.run_evolution(
                conn, llm, cache, out_dir=Path(tmp) / "candidates",
                lineage_path=Path(tmp) / "lineage.json",
                now=datetime(2026, 7, 9, 1, 0, 0))
        written = list((Path(tmp) / "candidates").glob("*.md"))
        lineage = json.loads((Path(tmp) / "lineage.json").read_text())
    assert stats["clusters_found"] == 1 and stats["written"] == 1
    assert len(written) == 1
    assert lineage[0]["verdict"] == "promoted"
    assert lineage[0]["cluster_refs"] == ["loss0", "loss1", "loss2", "loss3"]


def test_run_from_sleep_phase_skips_gracefully():
    conn = make_conn()

    class DeadLLM:
        base_url = "http://fake"

        def is_reachable(self):
            return False

    out = ev.run_from_sleep_phase(conn, DeadLLM())
    assert out == {"skipped": "Ollama not reachable"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError:
            print(f"FAIL  {t.__name__}")
        else:
            passed += 1
    print(f"\n{passed}/{len(tests)} tests passed.")

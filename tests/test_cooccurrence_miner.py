"""
Tests for the co-occurrence miner (Phase 5, P5-1). Fully offline — the pure
functions are exercised on synthetic transactions; the DB path uses an
in-memory brain_map. No network, no real artifacts.

Run either of these from the project folder:
    python tests/test_cooccurrence_miner.py
    python -m pytest tests/test_cooccurrence_miner.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import daily_context as dc
from src.discovery import cooccurrence_miner as cm
from src.validation import registry as rg
from src.validation import stat_gates as sg


def _txn(items, win, stratum):
    return {"items": frozenset(items), "win": win, "stratum": stratum}


# ----------------------------------------------------- context -> tags

def test_context_tags_are_null_honest():
    row = {"vix_band": "CALM", "fii_net": -100.0, "dii_net": 50.0,
           "news_net": None, "macro_nifty_short": 0.03,
           "macro_bank_short": None,
           "deals_buy_legs": 5, "deals_sell_legs": 2,
           "affinity_distribution": 3, "affinity_accumulation": 1}
    tags = cm.context_tags(row)
    assert "ctx:vix:CALM" in tags
    assert "ctx:fii:down" in tags and "ctx:dii:up" in tags
    assert "ctx:macro_nifty:up" in tags
    assert "ctx:deals:net_buy" in tags
    assert "ctx:affinity:distribution" in tags
    # Absent readings contribute NO tag — never a guessed neutral.
    assert not any(t.startswith("ctx:news") for t in tags)
    assert not any(t.startswith("ctx:macro_bank") for t in tags)
    assert cm.context_tags(None) == set()


# ----------------------------------------------------- apriori

def test_frequent_itemsets_respects_support_and_maxlen():
    txns = [_txn({"A", "B", "C"}, True, ("X", "CALM")) for _ in range(12)]
    txns += [_txn({"A"}, False, ("X", "CALM")) for _ in range(5)]
    freq = cm.frequent_itemsets(txns, min_support=12, max_len=3)
    assert frozenset(["A"]) in freq and freq[frozenset(["A"])] == 17
    assert frozenset(["A", "B"]) in freq          # co-occurs 12x
    assert frozenset(["A", "B", "C"]) in freq
    # A rare pairing never clears the floor.
    assert not any(len(k) == 2 and freq[k] < 12 for k in freq)


# ----------------------------------------------------- the mine

def test_mine_surfaces_a_real_edge_and_rejects_the_inverse():
    # One stratum, moderate base rate, but {A,B} massively overperforms it
    # while {D,E} underperforms it. Stratification uses the stratum's OWN
    # blended rate as the null, so the edge is measured WITHIN the cell.
    txns = [_txn({"A", "B"}, True, ("X", "CALM")) for _ in range(14)]
    txns += [_txn({"A", "B"}, False, ("X", "CALM")) for _ in range(1)]
    txns += [_txn({"D", "E"}, False, ("X", "CALM")) for _ in range(12)]
    txns += [_txn({"D", "E"}, True, ("X", "CALM")) for _ in range(3)]
    survivors = cm.mine(txns, min_support=12, fdr_q=0.15)
    tag_sets = [tuple(s["tags"]) for s in survivors]
    assert ("A", "B") in tag_sets                 # genuine edge survives
    assert ("D", "E") not in tag_sets             # significant but WRONG way
    ab = next(s for s in survivors if s["tags"] == ["A", "B"])
    assert ab["lift"] > 0 and ab["expected_rate"] < ab["win_rate"]


# ------------------------------------- one signal = one pattern (08-19)

def test_nested_itemsets_over_the_same_trades_collapse_to_one_maximal():
    """THE 2026-08-18 REGRESSION: Apriori enumerated {A,B}, {A,C}, {B,C}
    and {A,B,C} over ONE cluster of trades and the registry minted four
    "discoveries" with byte-identical stats. Now it mints one — the
    maximal set — and says what it absorbed."""
    txns = [_txn({"A", "B", "C"}, True, ("X", "CALM")) for _ in range(14)]
    txns += [_txn({"A", "B", "C"}, False, ("X", "CALM")) for _ in range(1)]
    txns += [_txn({"D", "E"}, False, ("X", "CALM")) for _ in range(12)]
    txns += [_txn({"D", "E"}, True, ("X", "CALM")) for _ in range(3)]

    survivors = cm.mine(txns, min_support=12, fdr_q=0.15)

    assert len(survivors) == 1
    winner = survivors[0]
    assert winner["tags"] == ["A", "B", "C"]          # maximal, not a subset
    assert winner["absorbed_n"] == 3                  # the three 2-tag sets
    assert sorted(winner["absorbed"]) == [["A", "B"], ["A", "C"], ["B", "C"]]
    assert "_covered" not in winner                   # internal key stripped


def test_two_genuinely_different_clusters_are_not_collapsed():
    """Same n is not the same finding. Collapsing keys on the identical
    SET of transactions, so two disjoint clusters that happen to share a
    support count both survive."""
    txns = [_txn({"A", "B"}, True, ("X", "CALM")) for _ in range(13)]
    txns += [_txn({"A", "B"}, False, ("X", "CALM")) for _ in range(1)]
    txns += [_txn({"P", "Q"}, True, ("X", "CALM")) for _ in range(13)]
    txns += [_txn({"P", "Q"}, False, ("X", "CALM")) for _ in range(1)]
    txns += [_txn({"D", "E"}, False, ("X", "CALM")) for _ in range(20)]

    tag_sets = [tuple(s["tags"]) for s in cm.mine(txns, min_support=12,
                                                  fdr_q=0.15)]
    assert ("A", "B") in tag_sets and ("P", "Q") in tag_sets


def test_collapsing_happens_after_fdr_never_before():
    """Every enumerated itemset must stay in the Benjamini-Hochberg
    denominator — correcting for only the survivors would be the exact
    multiple-testing sin this layer exists to prevent."""
    seen = {}
    real_bh = sg.benjamini_hochberg

    def spy(pvals, q):
        seen["n_tested"] = len(pvals)
        return real_bh(pvals, q=q)

    txns = [_txn({"A", "B", "C"}, True, ("X", "CALM")) for _ in range(14)]
    txns += [_txn({"A", "B", "C"}, False, ("X", "CALM")) for _ in range(1)]
    txns += [_txn({"D", "E"}, False, ("X", "CALM")) for _ in range(12)]
    txns += [_txn({"D", "E"}, True, ("X", "CALM")) for _ in range(3)]
    try:
        sg.benjamini_hochberg = spy
        survivors = cm.mine(txns, min_support=12, fdr_q=0.15)
    finally:
        sg.benjamini_hochberg = real_bh

    assert seen["n_tested"] >= 4          # {A,B} {A,C} {B,C} {A,B,C} + ...
    assert len(survivors) < seen["n_tested"]     # collapsed AFTER the test


def test_collapse_is_deterministic_so_pattern_ids_stay_idempotent():
    """Two runs over the same data must mint the same representative —
    the registry's idempotence contract depends on it."""
    txns = [_txn({"A", "B", "C"}, True, ("X", "CALM")) for _ in range(14)]
    txns += [_txn({"A", "B", "C"}, False, ("X", "CALM")) for _ in range(1)]
    txns += [_txn({"D", "E"}, False, ("X", "CALM")) for _ in range(12)]
    txns += [_txn({"D", "E"}, True, ("X", "CALM")) for _ in range(3)]
    first = [tuple(s["tags"]) for s in cm.mine(txns, min_support=12, fdr_q=0.15)]
    second = [tuple(s["tags"]) for s in cm.mine(list(reversed(txns)),
                                                min_support=12, fdr_q=0.15)]
    assert first == second


def test_month_seasonality_never_reaches_the_miner():
    """The banned tag cannot enter a transaction, so it cannot enter an
    itemset, so it cannot be registered — enforced at the one door."""
    tags = cm.context_tags({"date": "2026-07-15", "fii_net": -3.0})
    assert "ctx:season:month_jul" not in tags
    assert "ctx:fii:down" in tags


def test_stratification_defuses_a_pipeline_gate():
    # A cluster that is merely coextensive with a 100%-win stratum must NOT
    # read as an edge: its stratified null equals the stratum's own rate,
    # so observed == expected and nothing is significant.
    txns = [_txn({"G", "H"}, True, ("Y", "CALM")) for _ in range(20)]
    survivors = cm.mine(txns, min_support=12, fdr_q=0.15)
    assert survivors == []


def test_thin_data_yields_no_survivors_and_that_is_correct():
    txns = [_txn({"A", "B"}, True, ("X", "CALM")) for _ in range(8)]
    assert cm.mine(txns, min_support=12, fdr_q=0.15) == []


# ----------------------------------------------------- transactions (DB)

def test_build_transactions_splits_real_and_sim_and_joins_context():
    conn = brain_map.connect(":memory:")
    dc.record_frame(conn, {"date": "2026-01-05", "vix_band": "CALM",
                           "fii_net": -5.0, "deals_buy_legs": 4,
                           "deals_sell_legs": 1})
    # Real resolved outcome with an event tag.
    oid = brain_map.record_outcome(
        conn, journal_ref="2026-01-05|REL|BUY|100", date="2026-01-05",
        ticker="REL", r_multiple=1.4, result="win")
    eid = brain_map.record_event(conn, "2026-01-05", "REL", "signal",
                                 "golden_cross", source="journal")
    brain_map.link_event_outcome(conn, eid, oid)
    # A simulated outcome on the same day (sim: ref -> sim corpus only).
    brain_map.record_outcome(
        conn, journal_ref="sim:deadbeef", date="2026-01-05", ticker="REL",
        r_multiple=-1.0, result="loss")

    real = cm.build_transactions(conn, corpus="real")
    sim = cm.build_transactions(conn, corpus="sim")
    assert len(real) == 1 and len(sim) == 1
    rt = real[0]
    assert "golden_cross" in rt["items"]
    assert "ctx:vix:CALM" in rt["items"] and "ctx:deals:net_buy" in rt["items"]
    assert rt["win"] is True and rt["stratum"] == ("REL", "CALM")
    # The sim txn inherits the same context but is quarantined to its corpus.
    assert "ctx:vix:CALM" in sim[0]["items"] and sim[0]["win"] is False


# ----------------------------------------------------- registration

def test_register_survivors_is_idempotent_candidates():
    conn = brain_map.connect(":memory:")
    survivors = [{"tags": ["A", "B"], "support": 14, "n": 15, "wins": 14,
                  "win_rate": 14 / 15, "expected_rate": 0.57,
                  "p_value": 0.001, "lift": 0.36}]
    first = cm.register_survivors(conn, survivors, "real")
    assert first[0]["created"] is True and first[0]["status"] == "CANDIDATE"
    row = rg.get(conn, first[0]["pattern_id"])
    assert row["kind"] == "cooccurrence" and "[real]" in row["description"]
    # Re-mining the same cluster mints nothing new (frozen-definition rule).
    second = cm.register_survivors(conn, survivors, "real")
    assert second[0]["created"] is False
    assert second[0]["pattern_id"] == first[0]["pattern_id"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

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

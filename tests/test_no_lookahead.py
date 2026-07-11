"""
The timelock suite (Phase 2, P2-3): every discovery-facing surface must be
FUTURE-BLIND — adding data dated after its as_of must not change output.
This suite is the enforcement mechanism (decision-#30 import-guard style):
a new discovery feature without a timelock test here has no contract.

Run either of these from the project folder:
    python tests/test_no_lookahead.py
    python -m pytest tests/test_no_lookahead.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.knowledge_graph import entity_affinity as ea
from src.validation import timelock as tl


GROUPS = {"ticker_to_group": {"ADANIENT.NS": "ADANI", "TCS.NS": "TATA"},
          "groups": {"ADANI": ["ADANIENT.NS"], "TATA": ["TCS.NS"]},
          "client_aliases": {}}
AS_OF = "2026-07-10"


def _deal(day, ticker="ADANIENT.NS", client="MISTY SEAS FUND", side="sell",
          qty=1000):
    return {"ticker": ticker, "client": client, "side": side, "qty": qty,
            "price": 100.0, "value_rs": qty * 100.0, "deal_type": "bulk",
            "as_of": day}


def _base_history():
    return [_deal("2026-07-06"), _deal("2026-07-07"), _deal("2026-07-08")]


def _future_row(day):
    # A future deal that would FLIP the story if it leaked: massive buy.
    return _deal(day, side="buy", qty=500000)


def test_semantic_equal_tolerates_formatting_never_drift():
    assert tl.semantic_equal({"a": 1.0, "b": [1, 2]}, {"b": [1, 2], "a": 1})
    assert not tl.semantic_equal({"a": 1.0}, {"a": 1.1})
    assert not tl.semantic_equal([1, 2], [1, 2, 3])


def test_affinity_accumulation_is_future_blind():
    def compute(history):
        conn = brain_map.connect(":memory:")
        ea.accumulate_entity_affinity(conn, history, GROUPS,
                                      today=date.fromisoformat(AS_OF))
        rows = [dict(r) for r in conn.execute(
            "SELECT client, grp, buy_qty, sell_qty, deal_count "
            "FROM entity_affinity ORDER BY client, grp")]
        edges = [dict(r) for r in conn.execute(
            "SELECT source_node, target_node, confidence_score, valid_from "
            "FROM graph_edges ORDER BY source_node")]
        conn.close()
        return {"rows": rows, "edges": edges}

    tl.assert_future_blind(
        compute, _base_history(),
        tl.future_salt(_base_history(), AS_OF, _future_row),
        label="entity_affinity.accumulate_entity_affinity")


def test_affinity_readmodel_and_advisories_are_future_blind():
    def compute(history):
        conn = brain_map.connect(":memory:")
        ea.accumulate_entity_affinity(conn, history, GROUPS,
                                      today=date.fromisoformat(AS_OF))
        rm = ea.build_affinity_readmodel(conn, GROUPS, history,
                                         today=date.fromisoformat(AS_OF))
        adv = ea.evaluate_distribution_signals(
            rm, today=date.fromisoformat(AS_OF))
        conn.close()
        return {"rm": rm, "adv": adv}

    tl.assert_future_blind(
        compute, _base_history(),
        tl.future_salt(_base_history(), AS_OF, _future_row),
        label="entity_affinity.build_affinity_readmodel")


def test_the_harness_itself_catches_a_planted_leak():
    """Sanity: a deliberately leaky computation (no horizon filter) must
    FAIL the check — proving the suite can actually catch violations."""
    def leaky(history):
        return {"net": sum(d["qty"] if d["side"] == "buy" else -d["qty"]
                           for d in history)}
    try:
        tl.assert_future_blind(
            leaky, _base_history(),
            tl.future_salt(_base_history(), AS_OF, _future_row),
            label="planted leak")
    except AssertionError as exc:
        assert "TIMELOCK VIOLATION" in str(exc)
    else:
        raise AssertionError("planted leak was NOT caught")


def test_evidence_snapshot_is_pure_of_unpassed_history():
    """build_evidence_snapshot without load_missing consults ONLY its
    arguments — same inputs, same output, no ambient reads."""
    from src.confluence.evidence import build_evidence_snapshot
    a = build_evidence_snapshot("TCS.NS", today=date(2026, 7, 10), vix=14.0)
    b = build_evidence_snapshot("TCS.NS", today=date(2026, 7, 10), vix=14.0)
    assert tl.semantic_equal(a, b)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

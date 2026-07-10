"""
Loss-permanence invariant (survivorship-bias guard).

The knowledge graph must NEVER lose a loss trade — deleting or fading losses
while wins persist would inflate every win-rate the system reports (fake
data). This locks the guarantee end to end: a recorded loss survives a FULL
decay pass (graph-edge decay + semantic-node decay) with its outcome row
intact and STILL COUNTED against the win-rate, and the decaying layers are
proven to FLAG-not-DELETE (rows kept even once inactive/expired).

If a future change ever starts hard-deleting outcomes, decaying the outcome
ledger, or dropping decayed rows, these tests fail loudly.

Run either of these from the project folder:
    python tests/test_loss_permanence.py     (simple, no extra installs)
    python -m pytest tests/                    (if you have pytest)
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, graph_engine, decay_engine, sleep_phase


def _seed_win_and_loss(conn):
    """One resolved WIN (tag 'golden_cross') and one resolved LOSS
    (tag 'earnings_miss'), each linked to its outcome — the minimal shape
    query_similar_events reasons over."""
    # WIN
    ev_w = brain_map.record_event(conn, "2026-06-01", "TCS.NS", "signal", "golden_cross")
    out_w = brain_map.record_outcome(conn, "win001", "2026-06-05", "TCS.NS",
                                     archetype="golden_cross", r_multiple=2.0)
    brain_map.link_event_outcome(conn, ev_w, out_w)
    # LOSS
    ev_l = brain_map.record_event(conn, "2026-06-02", "WIPRO.NS", "signal", "earnings_miss")
    out_l = brain_map.record_outcome(conn, "loss001", "2026-06-06", "WIPRO.NS",
                                     archetype="earnings_miss", r_multiple=-1.0)
    brain_map.link_event_outcome(conn, ev_l, out_l)
    return out_l


def _full_decay_pass(conn):
    """Run every decay mechanism the system has, hard, against the DB."""
    # Graph-edge decay: backdate an edge so the sweep pushes it below the
    # 0.1 expiry threshold in one run.
    graph_engine.add_edge(conn, "earnings_miss", "resulted_in", "loss",
                          confidence_score=0.5)
    conn.execute("UPDATE graph_edges SET valid_from = ? WHERE relation = ?",
                 ("2020-01-01T00:00:00+00:00", "resulted_in"))
    conn.commit()
    graph_stats = decay_engine.apply_decay_sweep(conn)

    # Semantic-node decay: a low-score node with an ancient anchor gets
    # flagged inactive this run.
    sleep_phase.ensure_schema(conn)
    conn.execute(
        "INSERT INTO semantic_nodes (tag, summary, sentiment, confidence_score, "
        "created_at, last_reinforced, active) VALUES (?, ?, 0, ?, ?, ?, 1)",
        ("loss_lesson", "avoid buying into an earnings miss", 0.15,
         "2020-01-01", "2020-01-01"))
    conn.commit()
    sem_stats = sleep_phase.apply_decay(conn, today=date(2026, 8, 1))
    return graph_stats, sem_stats


def test_loss_outcome_survives_full_decay_and_stays_counted():
    conn = brain_map.connect(":memory:")
    _seed_win_and_loss(conn)

    # BEFORE: the loss is present and drags the win-rate down.
    before = brain_map.query_similar_events(conn, ["earnings_miss"])
    assert before["count"] == 1 and before["win_rate"] == 0.0

    graph_stats, sem_stats = _full_decay_pass(conn)
    # Sanity: decay actually did something (edge expired, node flagged).
    assert graph_stats["expired"] >= 1
    assert sem_stats["flagged_inactive"] >= 1

    # AFTER: the loss outcome is STILL there, STILL a loss, STILL counted.
    row = conn.execute(
        "SELECT result FROM outcomes WHERE journal_ref = 'loss001'").fetchone()
    assert row is not None and row["result"] == "loss"
    after = brain_map.query_similar_events(conn, ["earnings_miss"])
    assert after["count"] == 1 and after["win_rate"] == 0.0   # not vanished, not 100%

    # The blended win-rate across both tags reflects the loss (1 win, 1 loss).
    both = brain_map.query_similar_events(conn, ["golden_cross", "earnings_miss"])
    assert both["count"] == 2 and both["win_rate"] == 0.5


def test_decay_flags_rather_than_deletes():
    """The decaying layers must KEEP their rows — decay is reversible
    (invalid_at / active=0), never a DELETE. If a row vanished, a
    re-observation could never revive it and history would be lost."""
    conn = brain_map.connect(":memory:")
    _seed_win_and_loss(conn)
    _full_decay_pass(conn)

    # The expired graph edge row is still present (invalid_at set, not gone).
    edge = conn.execute(
        "SELECT invalid_at FROM graph_edges WHERE relation = 'resulted_in'").fetchone()
    assert edge is not None and edge["invalid_at"] is not None

    # The flagged semantic node row is still present (active=0, not gone).
    node = conn.execute(
        "SELECT active FROM semantic_nodes WHERE tag = 'loss_lesson'").fetchone()
    assert node is not None and node["active"] == 0

    # And the event rows behind the outcomes are untouched by any decay.
    n_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert n_events == 2


def test_loss_derived_edge_is_decay_exempt():
    """A causal edge written with decay_lambda=0 (the loss-permanence rate)
    must survive ANY amount of decay at full confidence — a lesson paid for
    with a loss never fades from the active graph. A normal edge with the
    same age must expire, proving the exemption is the lambda, not luck."""
    conn = brain_map.connect(":memory:")
    graph_engine.add_edge(conn, "chased_breakout_into_resistance", "caused",
                          "loss", confidence_score=1.0, decay_lambda=0.0)
    graph_engine.add_edge(conn, "clean_golden_cross", "preceded",
                          "win", confidence_score=1.0)   # default lambda
    # Age both edges by six years.
    conn.execute("UPDATE graph_edges SET valid_from = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    decay_engine.apply_decay_sweep(conn)

    loss_edge = conn.execute(
        "SELECT confidence_score, invalid_at FROM graph_edges "
        "WHERE source_node = 'chased_breakout_into_resistance'").fetchone()
    assert loss_edge["invalid_at"] is None            # still active
    assert loss_edge["confidence_score"] == 1.0       # undecayed
    win_edge = conn.execute(
        "SELECT invalid_at FROM graph_edges "
        "WHERE source_node = 'clean_golden_cross'").fetchone()
    assert win_edge["invalid_at"] is not None         # normal edge expired

    # And the exemption is STICKY: a later write without an explicit rate
    # (e.g. the same lesson re-observed via a win-bucket extraction) keeps
    # lambda 0 rather than silently re-arming decay.
    graph_engine.add_edge(conn, "chased_breakout_into_resistance", "caused",
                          "loss", confidence_score=1.0)   # no decay_lambda
    row = conn.execute(
        "SELECT decay_lambda FROM graph_edges "
        "WHERE source_node = 'chased_breakout_into_resistance'").fetchone()
    assert row["decay_lambda"] == 0.0


def test_causal_writer_routes_loss_triples_to_exempt_lambda():
    """write_causal_links must extract win and loss outcomes in separate
    buckets and write the loss bucket's triples with decay_lambda=0."""
    conn = brain_map.connect(":memory:")
    _seed_win_and_loss(conn)

    class FakeExtractor:
        """Returns one distinct triple per bucket, keyed off the summary
        text so the test can tell which bucket each write came from."""
        def is_reachable(self):
            return True
        def extract_causal_triples(self, text):
            if "earnings_miss" in text:
                return [{"subject": "earnings_miss", "predicate": "caused",
                         "object": "loss", "condition": None}]
            return [{"subject": "golden_cross", "predicate": "preceded",
                     "object": "win", "condition": None}]

    stats = sleep_phase.write_causal_links(conn, extractor=FakeExtractor(),
                                           window_days=3650,
                                           today=date(2026, 8, 1))
    assert stats["triples_written"] == 2
    loss = conn.execute("SELECT decay_lambda FROM graph_edges "
                        "WHERE source_node = 'earnings_miss'").fetchone()
    win = conn.execute("SELECT decay_lambda FROM graph_edges "
                       "WHERE source_node = 'golden_cross'").fetchone()
    assert loss["decay_lambda"] == 0.0            # loss lesson: exempt
    assert win["decay_lambda"] not in (None, 0.0)  # win edge: decays normally


def test_no_delete_statement_touches_the_outcome_ledger():
    """Belt-and-braces source guard: nothing in src/ may DELETE FROM
    outcomes / events / event_outcome_link. If someone adds one, this
    fails — the outcome ledger is append-only by contract."""
    src = Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for py in src.rglob("*.py"):
        text = py.read_text().lower()
        for table in ("delete from outcomes", "delete from events",
                      "delete from event_outcome_link", "drop table outcomes",
                      "drop table events"):
            if table in text:
                offenders.append(f"{py.name}: {table}")
    assert not offenders, f"outcome ledger must be append-only; found: {offenders}"


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

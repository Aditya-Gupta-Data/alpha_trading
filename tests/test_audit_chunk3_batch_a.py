"""Audit Chunk 3, Batch A: W2 blank cards, Z1 token polarity, S4 per-host opportunity cost."""
from src import notifier
from src.vol_bridge import _node_polarity


def test_w2_text_and_embed_payloads_become_event_cards():
    e = notifier._build_embed(notifier._normalise_payload({"event": "digest", "text": "hello"}))
    assert e["description"] == "hello" and "Digest" in e["title"]
    e = notifier._build_embed(notifier._normalise_payload({"embeds": [{"description": "perf"}]}))
    assert e["description"] == "perf" and e["title"].startswith("📌 Note")
    p = notifier._normalise_payload({"event": "alias_review", "text": "x"})
    assert p["ticker"] == "desk" and p["date"] and p["description"] == "x"
    assert notifier._normalise_payload({"event": "eod", "description": "d"}) == {"event": "eod", "description": "d"}


def test_w2_budget_gate_sees_the_named_event(tmp_path):
    v = notifier.budget_gate(notifier._normalise_payload({"event": "digest", "text": "t"}),
                             state_path=tmp_path / "b.json", queue_path=tmp_path / "q.jsonl", enabled=True)
    assert v == "send"
    v = notifier.budget_gate(notifier._normalise_payload({"event": "alias_review", "text": "needs a look"}),
                             state_path=tmp_path / "b.json", queue_path=tmp_path / "q.jsonl", enabled=True)
    assert v == "spool"
    row = (tmp_path / "q.jsonl").read_text()
    assert "alias_review" in row and "needs a look" in row


def test_z1_polarity_matches_whole_tokens_only():
    for n in ("wider_wing_spacing", "time_decay_against_our_long", "following_the_signal",
              "stronger_downside_momentum", "drawings"):
        assert _node_polarity(n) == 0, n
    assert _node_polarity("iron_condor_RESULTS_IN_win") == 1
    assert _node_polarity("three wins in a row") == 1
    assert _node_polarity("losses_mount") == -1
    assert _node_polarity("HIGH_TAIL_RISK") == -1
    assert _node_polarity("stronger_downside_momentum_causes_loss") == -1


def test_s4_one_host_blocking_many_days_is_one_outcome():
    import sqlite3
    from src import opportunity_cost as oc
    from src.validation import trial
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    trial.ensure_schema(conn)
    for i in range(6):
        trial.record_block(conn, gate="exposure_gate", fire_date=f"2026-07-1{i}", ticker="NIFTY",
                           direction="bullish", host_ref="H1")
    conn.execute("UPDATE shadow_trades SET resolved=1, result='win', r_multiple=1.2 WHERE host_ref='H1'")
    stats = oc.collect(conn=conn)
    assert stats["resolved"] == 1 and stats["resolved_rows"] == 6 and stats["wins"] == 1
    assert stats["verdict"] == "ACCUMULATING"
    assert "1** independent host" in " ".join(oc.render_lines(stats))

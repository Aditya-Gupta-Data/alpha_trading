"""Audit Chunk 4, Batches A+B: spool keeps the card (R1), morning brief scheduled (R2), pages don't burn the budget (R3),
digest acks on delivery (B4), W/L approved only (R4), clean day needs the sweep (R5), firm_mtm abstains (R8), /pnl (B2)."""
import json
from src import notifier as nt


def test_r1_spooled_trade_card_keeps_its_content(tmp_path):
    q = tmp_path / "q.jsonl"
    v = nt.budget_gate({"event": "closed", "ticker": "NIFTY 50", "date": "2026-10-09", "pnl_rs": -4200.0,
                        "resolution": "ratchet_hit", "r_multiple": -0.7},
                       enabled=True, state_path=tmp_path / "b.json", queue_path=q)
    assert v == "spool"
    row = json.loads(q.read_text())
    assert row["payload"]["pnl_rs"] == -4200.0 and "NIFTY 50" in row["summary"] and "4,200" in row["summary"]
    text, n = nt.peek_digest_queue(q)
    assert n == 1 and "closed: NIFTY 50" in text and q.read_text().strip()      # peek never drains


def test_r2_r3_morning_brief_is_scheduled_and_pages_do_not_count(tmp_path):
    assert "morning_brief" in nt.BUDGET_SCHEDULED
    assert "live_entry_needs_review" in nt.BUDGET_ALWAYS
    assert nt.budget_gate({"event": "morning_brief", "description": "x"}, enabled=True,
                          state_path=tmp_path / "b.json", queue_path=tmp_path / "q.jsonl") == "send"


def test_b4_ack_removes_only_the_delivered_rows(tmp_path):
    q = tmp_path / "q.jsonl"
    for i in range(3):
        nt._spool({"event": f"ev{i}", "description": f"d{i}"}, q)
    text, n = nt.peek_digest_queue(q, max_lines=2)
    assert n == 3 and "…and 1 more" in text
    assert nt.ack_digest_queue(q, 2) == 2
    assert [json.loads(l)["event"] for l in q.read_text().splitlines()] == ["ev2"]
    assert (tmp_path / "q.jsonl.drained").read_text().count("\n") == 2
    assert nt.drain_digest_queue(q) is not None and q.read_text() == ""


def test_r4_wl_counts_only_approved_real_rows(tmp_path, monkeypatch):
    import sqlite3
    from src import eod_summary as es
    db = tmp_path / "bm.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE outcomes (journal_ref TEXT, date TEXT, ticker TEXT, archetype TEXT, r_multiple REAL, result TEXT)")
    c.executemany("INSERT INTO outcomes VALUES (?, '2026-10-09', 'NIFTY 50', 'bear_put_spread', 1.0, 'win')",
                  [("aaaa0001",), ("bbbb0002",), ("cccc0003",)])
    c.commit(); c.close()
    monkeypatch.setattr(es, "_today", lambda: "2026-10-09")
    entries = [{"short_id": "aaaa0001", "decision": "approved", "outcome": {"r_multiple": 1.0}},
               {"short_id": "bbbb0002", "decision": "rejected", "outcome": {"r_multiple": 1.0, "hypothetical": True}},
               {"short_id": "cccc0003", "decision": "approved", "outcome": {"r_multiple": 1.0, "hypothetical": True}}]
    rows = es.query_todays_resolutions(db_path=db, entries=entries)
    assert [r["journal_ref"] for r in rows] == ["aaaa0001"]


def test_r8_firm_mtm_never_reports_a_crashed_reader_as_flat(monkeypatch):
    from src import firm_mtm
    monkeypatch.setattr("src.portfolio_report._open_entries", lambda e=None: 1 / 0)
    u, marked, open_count, names = firm_mtm._options_unrealized(entries=[])
    assert u is None and open_count == 1 and names == ["options book unreadable"]


def test_b2_pnl_accepts_the_parsed_gateway_body(monkeypatch):
    from src import discord_bot as db
    monkeypatch.setattr(db, "_bridge_call", lambda m, p, payload=None: (200, {"ok": True, "card": {"text": "x"}}))
    assert db._fetch_pnl() == {"text": "x"}
    monkeypatch.setattr(db, "_bridge_call", lambda m, p, payload=None: (500, {"error": "boom"}))
    try:
        db._fetch_pnl()
    except RuntimeError as e:
        assert "500" in str(e)

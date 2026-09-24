"""Decision #111 — the showcase dashboard's data layer: read-only, honest
on a locked/absent database, ratchet + recon + audit surfaced. No streamlit
needed here (the UI file is a manual tool)."""
import json
import sqlite3
from pathlib import Path

from src.dashboard import data as d


def _db(tmp_path):
    from src import brain_map, portfolio_manager as pm
    p = tmp_path / "bm.db"
    conn = brain_map.connect(str(p))
    pm.get_account(conn)
    pm.get_paper_account(conn, "PAPER_2L")
    pm.request_entry(conn, "ab12cd34", 50000.0)
    pm.paper_request_entry(conn, "PAPER_2L", "ab12cd34", 9000.0, lots=1, primary_lots=2)
    pm.log_event(conn, "treasury_rotation", "equity budget set to Rs.300,000")
    conn.close()
    return p


def test_treasury_reads_both_accounts_read_only(tmp_path):
    p = _db(tmp_path)
    T = d.treasury(p)
    assert T["PAPER_10L"]["equity"] == 1_000_000.0 and T["PAPER_10L"]["open_locks"] == 1
    assert T["PAPER_10L"]["available_cash"] == 950_000.0 and T["PAPER_10L"]["drawdown_pct"] == 0.0
    assert T["PAPER_2L"]["equity"] == 200_000.0 and T["PAPER_2L"]["locked_margin"] == 9000.0
    assert T["PAPER_2L"]["rejections"] == 0
    # read-only: a write through the dashboard's connection must fail
    conn = d.connect_ro(p)
    try:
        conn.execute("INSERT INTO account_events (ts, event_type, detail) VALUES ('x','y','z')")
        assert False, "read-only connection accepted a write"
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def test_missing_or_locked_database_is_an_error_row_not_a_crash(tmp_path):
    assert "error" in d.treasury(tmp_path / "nope.db")
    ev = d.audit_events(tmp_path / "nope.db")
    assert ev[0]["event_type"].startswith("database unavailable")


def test_open_trades_carry_strategy_labels_ratchet_and_equity_rows(tmp_path):
    j = tmp_path / "j.jsonl"
    j.write_text("\n".join(json.dumps(x) for x in [
        {"short_id": "aa11", "ticker": "NIFTY 50", "date": "2026-09-16", "decision": "approved", "outcome": None,
         "spread": {"strategy": "bear_put_spread", "direction": "bearish", "lots": 2, "max_loss": 5000.0, "expiry": "2026-10-27"},
         "ratchet": {"peak_capture_pct": 51.45, "locked_pct": 0.0, "armed": True},
         "accounts": {"PAPER_2L": {"status": "approved"}}, "sizing": {"reason": "sized"}},
        {"short_id": "bb22", "ticker": "TCS.NS", "date": "2026-08-12", "decision": "approved", "outcome": None,
         "spread": {"strategy": "iron_condor", "direction": "neutral", "lots": 1, "max_loss": 3566.25, "expiry": "2026-10-27"}},
        {"short_id": "cc33", "ticker": "X", "date": "2026-08-12", "decision": "approved",
         "outcome": {"resolution": "profit_take", "pnl_rs": 11136.68, "r_multiple": 1.4, "settled_at": "2026-09-23T21:57:00",
                     "execution": {"ticket_id": "tkt:1"}}, "spread": {"strategy": "iron_condor"}},
    ]) + "\n")
    eq = tmp_path / "eq.jsonl"
    eq.write_text("\n".join(json.dumps(x) for x in [
        {"event": "entry", "id": "e1", "ticker": "DABUR.NS", "as_of": "2026-09-18",
         "funding": {"funded": True, "qty": 186, "lock_ref": "eqd:e1"}, "kya_kara_action": {"entry_price": 385.4, "stop": 374.42}},
        {"event": "entry", "id": "e2", "ticker": "LTF.NS", "as_of": "2026-09-21",
         "funding": {"funded": True, "qty": 307, "lock_ref": "eqd:e2"}, "kya_kara_action": {"entry_price": 309.75, "stop": 295.42}},
        {"event": "exit", "id": "e2", "exit_price": 300.0},
    ]) + "\n")
    rows = d.open_trades(j, eq, snapshot_marks={"aa11": {"live_pnl_rs": 4200.0, "capture_pct": 42.0}})
    by = {r["id"]: r for r in rows}
    assert set(by) == {"aa11", "bb22", "eqd:e1"}
    assert by["aa11"]["strategy"] == "Bear Put" and by["aa11"]["accounts"] == "PAPER_10L, PAPER_2L"
    assert by["aa11"]["ratchet"] == "armed → lock 0.0%" and by["aa11"]["mtm_rs"] == 4200.0
    assert by["aa11"]["max_loss_rs"] == 10000.0
    assert by["bb22"]["strategy"] == "Iron Condor" and by["bb22"]["ratchet"] == "static 65% take"
    assert by["eqd:e1"]["strategy"] == "Equity Long" and by["eqd:e1"]["lots"] == 186
    assert by["eqd:e1"]["max_loss_rs"] == round((385.4 - 374.42) * 186, 2)
    outs = d.recent_outcomes(j)
    assert outs[0]["resolution"] == "profit_take" and outs[0]["ticket"] == "tkt:1"


def test_recon_latest_and_history_read_the_ledger(tmp_path):
    p = tmp_path / "recon.jsonl"
    assert d.latest_recon(p) is None
    p.write_text(json.dumps({"ts": "2026-09-23T11:43:41", "verdict": "parity", "broker_positions": 0,
                             "book_rows": 15, "mismatches": [], "paper_only": [{"account": "PAPER_10L"}]}) + "\n"
                 + json.dumps({"ts": "2026-09-24T11:00:00", "verdict": "mismatch", "broker_positions": 1,
                               "book_rows": 15, "mismatches": [{"kind": "broker_position_unknown_to_book"}]}) + "\n")
    assert d.latest_recon(p)["verdict"] == "mismatch"
    h = d.recon_history(p)
    assert [x["verdict"] for x in h] == ["parity", "mismatch"] and h[1]["mismatches"] == 1


def test_audit_events_merge_both_ledgers_newest_first(tmp_path):
    p = _db(tmp_path)
    from src import brain_map, portfolio_manager as pm
    c = brain_map.connect(str(p))
    pm.paper_request_entry(c, "PAPER_2L", "zz99zz99", 500_000.0)     # a shadow refusal row
    c.close()
    ev = d.audit_events(p)
    kinds = {e["event_type"] for e in ev}
    assert {"treasury_rotation", "entry_approved"} & kinds or len(ev) >= 2
    assert ev == sorted(ev, key=lambda e: str(e["ts"]), reverse=True)
    assert any(e["account"] == "PAPER_2L" for e in ev)


def test_the_dashboard_writes_nothing_and_imports_no_execution_path():
    import re
    src = Path(d.__file__).read_text() + Path(d.__file__).with_name("app.py").read_text()
    for pat in (r"rewrite_all", r"INSERT", r"UPDATE ", r"DELETE", r"\.execute\(\s*[\"']INSERT",
                r"paper_venue", r"strategy_router", r"oms\.", r"dhan_client", r"fire_broadcast"):
        assert not re.search(pat, src), pat
    assert "mode=ro" in Path(d.__file__).read_text()

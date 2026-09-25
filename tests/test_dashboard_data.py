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


def test_cagr_is_annualised_from_the_run_epoch_and_none_under_a_day(tmp_path):
    assert d.cagr(1_000_000, 1_085_280.33, 64) == round(((1.08528033) ** (365 / 64) - 1) * 100, 2)
    assert d.cagr(1_000_000, 1_085_280.33, 0.5) is None and d.cagr(0, 1, 10) is None
    from src import brain_map, portfolio_manager as pm
    p = tmp_path / "bm.db"
    conn = brain_map.connect(str(p)); pm.get_account(conn); pm.get_paper_account(conn, "PAPER_2L")
    conn.execute("UPDATE account_state SET realized_pnl = 100000 WHERE id = 1")
    conn.execute("INSERT INTO account_events (ts, event_type, detail) VALUES (?, 'clean_sheet', 'epoch')",
                 ((d.datetime.now(d.IST) - d.timedelta(days=73)).replace(tzinfo=None).isoformat(timespec="seconds"),))
    conn.commit(); conn.close()
    T = d.treasury(p)
    a = T["PAPER_10L"]
    assert 72.9 <= a["days_elapsed"] <= 73.1 and a["abs_return_pct"] == 10.0
    assert a["cagr_pct"] == d.cagr(1_000_000, 1_100_000, a["days_elapsed"]) and a["cagr_pct"] > 10.0
    assert T["PAPER_2L"]["cagr_pct"] is None                      # born seconds ago: under a day


def test_full_curve_with_capital_markers_and_cagr_from_the_10l_base(tmp_path):
    """#117: the curve keeps the whole history in time order and the pool
    moves come back as labelled markers; #116: return / CAGR from the base."""
    p = _db(tmp_path)
    conn = sqlite3.connect(p)
    rows = [("2026-07-15T10:00:00", 1_047_617.31), ("2026-07-23T11:15:10", 205_538.96),
            ("2026-08-07T16:41:19", 1_039_423.99), ("2026-08-11T11:25:49", 1_033_825.75),
            ("2026-09-25T09:21:18", 1_084_859.92)]
    conn.executemany("INSERT INTO equity_curve (ts, equity, peak_equity, drawdown_pct) VALUES (?, ?, ?, 0)",
                     [(t, e, e) for t, e in reversed(rows)])            # inserted out of order
    conn.executemany("INSERT INTO account_events (ts, event_type, detail) VALUES (?, ?, ?)", [
        ("2026-08-07T16:41:19", "capital_injection",
         "Rs.800,000.00 (200,000.00 -> 1,000,000.00 base; equity 239,423.99 -> 1,039,423.99)"),
        ("2026-07-21T14:32:30", "clean_sheet", "decision #84: pool reset 10L->2L for the autonomous run")])
    conn.execute("UPDATE account_state SET realized_pnl = 84859.92 WHERE id = 1")
    conn.commit(); conn.close()
    T = d.treasury(p)
    assert [c["ts"] for c in T["equity_curve"]] == [r[0] for r in rows]     # full history, time-ordered
    assert [(e["ts"], e["label"]) for e in T["capital_events"]] == [
        ("2026-07-21T14:32:30", "Pool reset ₹10L → ₹2L"),
        ("2026-08-07T16:41:19", "₹8L capital injection")]
    assert [e["short"] for e in T["capital_events"]] == ["Reset → ₹2L", "+₹8L"]    # the chart's tags
    assert T["base_epoch"] == "2026-08-07"
    a = T["PAPER_10L"]
    assert a["base_equity"] == 1_039_423.99 and a["base_ts"] == "2026-08-07T16:41:19"
    assert a["abs_return_pct"] == round((1_084_859.92 / 1_039_423.99 - 1) * 100, 2)      # 4.37, not 8.49
    assert a["cagr_pct"] == d.cagr(1_039_423.99, 1_084_859.92, a["days_elapsed"])


def test_capital_event_labels_never_guess_an_amount(tmp_path):
    p = _db(tmp_path)
    conn = sqlite3.connect(p)
    conn.executemany("INSERT INTO account_events (ts, event_type, detail) VALUES (?, ?, ?)", [
        ("2026-09-01T10:00:00", "capital_injection", "Rs.-250,000.00 (withdrawal)"),
        ("2026-09-02T10:00:00", "capital_injection", "no amount here"),
        ("2026-09-03T10:00:00", "clean_sheet", "fresh start")])
    conn.commit(); conn.close()
    assert [e["label"] for e in d.treasury(p)["capital_events"]] == [
        "₹2.50L capital withdrawal", "capital injection", "Pool reset (clean sheet)"]


def test_unrealized_pnl_and_true_net_equity_come_from_the_engine_snapshot(tmp_path):
    p = _db(tmp_path)                                    # 10L + 2L both hold ab12cd34 (2L: 1 of 2 lots)
    conn = d.connect_ro(p)
    rows = [{"id": "ab12cd34", "mtm_rs": 3000.0}, {"id": "eqd:x", "mtm_rs": None}]
    snap = {"as_of": "2026-09-25T11:29:38+05:30",
            "marks": [{"short_id": "ab12cd34", "live_pnl_rs": 3000.0}]}
    u = d.unrealized_by_account(conn, snap, rows)
    conn.close()
    assert u["PAPER_10L"] == {"unrealized_pnl": 3000.0, "marked_positions": 1, "open_positions": 2}
    assert u["PAPER_2L"] == {"unrealized_pnl": 1500.0, "marked_positions": 1, "open_positions": 1}  # x 1/2 lots
    a = d._with_mtm({"equity": 1_000_000.0}, u["PAPER_10L"], snap["as_of"])
    assert a["net_equity"] == 1_003_000.0 and a["marks_as_of"] == snap["as_of"]


def test_nothing_priced_is_none_never_a_guessed_zero(tmp_path):
    p = _db(tmp_path)
    conn = d.connect_ro(p)
    u = d.unrealized_by_account(conn, {"marks": []}, [{"id": "ab12cd34", "mtm_rs": None}])
    conn.close()
    assert u["PAPER_10L"]["unrealized_pnl"] is None and u["PAPER_2L"]["unrealized_pnl"] is None
    a = d._with_mtm({"equity": 200_000.0}, u["PAPER_2L"], None)
    assert a["net_equity"] is None and a["open_positions"] == 1

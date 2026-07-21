"""
Equity desk v2 — VM-native, unified DB (decision #83) — hermetic tests.

The desk as a VIEW over the one firm account's `eqd:`-tagged locks:
budget/capital math, funding through the single pm.request_entry door,
the per-desk ruin halt, delivery-friction settlement, scrip-id quote
resolution, the LIVE darling cycle (in-zone live entries, live exits,
settlements), and the report-card renderer. All offline: :memory: firm
accounts, tmp ledgers/artifacts, injected quotes. Run:
    python -m pytest tests/test_equity_desk.py
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.equity_desk as desk
import src.firm_treasury as ft
from src import knowledge_graph_logger as kg
from src import portfolio_manager as pm
import src.equity_shadow_proposer as sp

IST = timezone(timedelta(hours=5, minutes=30))

# Pin the config knobs read at import time.
desk.EQUITY_DESK_ENABLED = True
desk.EQUITY_DESK_RISK_PER_TRADE_PCT = 1.0
desk.EQUITY_DESK_MAX_NOTIONAL_PCT = 15.0
desk.MAX_RISK_PER_TRADE_RS = 10000.0
ft.EQUITY_DESK_CAPITAL_RS = 300000.0


def _firm_conn():
    """A :memory: firm account (10L) with the treasury seeded at 3L."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pm.get_account(conn)
    ft.get_budget(conn)
    return conn


def _tier_row(sym="TCS", close=2269.0, stop=2085.0, val=35,
              tier="strong_buy", in_zone=True, pinned=None):
    return {"symbol": sym, "valuation": val, "forensic": 64, "close": close,
            "buy_zone": [2189.72, 2293.28], "stop": stop,
            "extension": "normal", "tier": tier,
            "family": {"strong_buy": "buy", "strong_sell": "sell"}.get(
                tier, "hold"),
            "in_zone": in_zone, "pinned": pinned,
            "rule": f"in zone + valuation {val} <= 45"}


def _write_artifacts(tmp, rows_by_tier, level_rows, as_of=None):
    tiers = tmp / "darling_tiers.json"
    levels = tmp / "darlings_levels.json"
    tiers.write_text(json.dumps(
        {"as_of": as_of or datetime.now(IST).isoformat(timespec="seconds"),
         "tiers": rows_by_tier}))
    levels.write_text(json.dumps({"levels": level_rows}))
    return tiers, levels


def _level_row(sym="TCS", trims=(2460.0, 2510.0)):
    return {"symbol": sym, "status": "ok", "trim_levels": list(trims),
            "anchored_vwap": 2240.0}


def _allow_all(proposal):
    return {"allowed": True, "blocked_by": None, "reason": None}


def _entry(sym="TCS", close=2269.0, stop=2085.0):
    return sp.evaluate_darling_entry(_tier_row(sym, close=close, stop=stop),
                                     _level_row(sym), "2026-07-21")


def test_desk_state_is_a_view_over_tagged_locks():
    conn = _firm_conn()
    s = desk.desk_state(conn)
    assert s["budget"] == s["capital"] == 300000.0 and s["deployed"] == 0
    pm.request_entry(conn, "eqd:a1", 40000.0)
    pm.request_entry(conn, "opt1", 100000.0)          # options lock: not ours
    s = desk.desk_state(conn)
    assert s["deployed"] == 40000.0 and s["available"] == 260000.0
    pm.release_margin(conn, "eqd:a1", -5000.0)
    s = desk.desk_state(conn)
    assert s["realized"] == -5000.0 and s["capital"] == 295000.0
    assert not s["ruin_halted"]
    conn.close()


def test_sizing_risk_budget_notional_cap_and_refusals():
    s = desk.size_entry(2269.0, 2085.0, 300000.0)
    assert s["qty"] == 16 and s["notional"] == round(16 * 2269.0, 2)
    s = desk.size_entry(100.0, 98.0, 300000.0)        # notional cap binds
    assert s["qty"] == 450 and s["notional"] == 45000.0
    assert desk.size_entry(100.0, 100.0, 300000.0)["qty"] == 0
    assert desk.size_entry(500000.0, 499000.0, 300000.0)["qty"] == 0


def test_hard_rupee_risk_cap_binds_above_percentage_sizing():
    """Decision #84: whatever the % sizing allows, no single trade may
    risk more than MAX_RISK_PER_TRADE_RS rupees entry-to-stop."""
    # 50% risk_pct of 3L = 1.5L budget — the Rs.10k cap must bind: risk
    # per share 100 -> 100 shares, never 1500.
    s = desk.size_entry(1000.0, 900.0, 300000.0, risk_pct=50.0)
    assert s["qty"] * 100.0 <= 10000.0 + 1e-9
    assert s["qty"] == 100 or s["notional"] <= 45000.0   # notional cap may
    # bind first (15% of 3L = 45k -> 45 shares); either way risk <= 10k.
    risk = s["qty"] * 100.0
    assert risk <= 10000.0


def test_fund_entry_locks_firm_cash_and_respects_kill_switch():
    conn = _firm_conn()
    f = desk.fund_entry(_entry(), conn=conn)
    assert f["funded"] and f["qty"] == 16
    assert pm.available_cash(conn) == 1_000_000.0 - f["notional"]
    desk.EQUITY_DESK_ENABLED = False
    try:
        assert desk.fund_entry(_entry("INFY"), conn=conn) == {
            "funded": False, "reason": "equity desk disabled"}
    finally:
        desk.EQUITY_DESK_ENABLED = True
    conn.close()


def test_fund_entry_budget_gate_and_desk_ruin_halt():
    conn = _firm_conn()
    ft.set_budget(conn, 30000.0, "test squeeze")      # tiny desk budget
    pm.request_entry(conn, "eqd:pre", 29000.0)        # nearly fully deployed
    f = desk.fund_entry(_entry(), conn=conn)          # 1 sh = 2269 > 1000
    assert not f["funded"] and "equity budget exhausted" in f["reason"]
    # Firm cash beyond the desk's own lock is untouched by the gate.
    assert pm.available_cash(conn) == 1_000_000.0 - 29000.0
    # Desk ruin: realized <= -10% of budget blocks desk entries only.
    ft.set_budget(conn, 300000.0, "restore")
    pm.request_entry(conn, "eqd:x", 1000.0)
    pm.release_margin(conn, "eqd:x", -30000.0)        # exactly -10%
    f = desk.fund_entry(_entry("INFY"), conn=conn)
    assert not f["funded"] and "ruin halt" in f["reason"]
    events = [r[0] for r in conn.execute(
        "SELECT event_type FROM account_events").fetchall()]
    assert "equity_desk_ruin_halt" in events
    conn.close()


def test_settle_exit_nets_delivery_frictions_into_firm_account():
    conn = _firm_conn()
    entry = _entry()
    entry["funding"] = desk.fund_entry(entry, conn=conn)
    qty = entry["funding"]["qty"]
    s = desk.settle_exit(entry, {"ticker": "TCS.NS", "reason": "target",
                                 "exit_price": 2460.0}, conn=conn)
    gross = (2460.0 - 2269.0) * qty
    expected = round(gross - desk.delivery_frictions("BUY", 2269.0, qty)
                     - desk.delivery_frictions("SELL", 2460.0, qty), 2)
    assert s["pnl_net"] == expected and s["pnl_net"] < gross
    assert pm.equity(conn) == 1_000_000.0 + expected  # firm equity moved
    assert desk.desk_state(conn)["realized"] == expected
    assert desk.settle_exit(_entry("INFY"), {"exit_price": 1.0},
                            conn=conn) is None        # unfunded never settles
    conn.close()


def test_security_id_resolution_is_fresh_or_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        ids = Path(tmp) / "darling_ids.json"
        assert desk.security_id_for("TCS", ids_path=ids) is None  # absent
        now = datetime.now(IST).replace(tzinfo=None).isoformat(
            timespec="seconds")
        ids.write_text(json.dumps({"built_at": now, "ids": {
            "TCS": {"id": "11536", "master_symbol": "TCS"}}}))
        assert desk.security_id_for("TCS.NS", ids_path=ids) == "11536"
        assert desk.security_id_for("UNKNOWN", ids_path=ids) is None
        old = (datetime.now(IST) - timedelta(days=20)).replace(
            tzinfo=None).isoformat(timespec="seconds")
        ids.write_text(json.dumps({"built_at": old, "ids": {
            "TCS": {"id": "11536"}}}))
        assert desk.security_id_for("TCS", ids_path=ids) is None  # stale
        # live_quote: id -> injected quote fn; no id -> None, no call.
        ids.write_text(json.dumps({"built_at": now, "ids": {
            "TCS": {"id": "11536"}}}))
        assert desk.live_quote("TCS.NS", ids_path=ids,
                               quote_by_id_fn=lambda sid: 3000.0) == 3000.0
        assert desk.live_quote("NEW.NS", ids_path=ids,
                               quote_by_id_fn=lambda sid: 3000.0) is None


def test_tiers_freshness_gate():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiers, _ = _write_artifacts(tmp, {}, [])
        assert desk._tiers_fresh(tiers) is True
        old = (datetime.now(IST) - timedelta(days=6)).isoformat()
        tiers.write_text(json.dumps({"as_of": old, "tiers": {}}))
        assert desk._tiers_fresh(tiers) is False
        assert desk._tiers_fresh(tmp / "missing.json") is False


def test_live_cycle_enters_in_zone_at_live_price_and_settles_later():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = _firm_conn()
        journal = tmp / "shadow.jsonl"
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("TCS")]}, [_level_row("TCS")])
        cards = []
        # Day 1: live quote INSIDE the strict zone -> funded live entry.
        res = desk.run_darling_live_cycle(
            tiers_path=tiers, levels_path=levels, path=journal, conn=conn,
            quote_fn=lambda t: 2250.0, check_fn=_allow_all, universe={},
            vix_fn=lambda: None, broadcast_fn=cards.append)
        [e] = res["entries"]
        assert e["mode"] == "PAPER_CAPITAL"
        assert e["kya_kara_action"]["entry_price"] == 2250.0   # the quote,
        assert e["kya_kara_action"]["fill_basis"] == "live"    # not close
        assert res["tiers_fresh"] and len(cards) == 1
        # Same day again: dedup holds, no re-entry, no second card.
        res = desk.run_darling_live_cycle(
            tiers_path=tiers, levels_path=levels, path=journal, conn=conn,
            quote_fn=lambda t: 2250.0, check_fn=_allow_all, universe={},
            vix_fn=lambda: None, broadcast_fn=cards.append)
        assert res["entries"] == [] and len(cards) == 1
        # Later: quote breaks the stop -> live exit + settlement into firm.
        res = desk.run_darling_live_cycle(
            tiers_path=tiers, levels_path=levels, path=journal, conn=conn,
            quote_fn=lambda t: 2080.0, check_fn=_allow_all, universe={},
            vix_fn=lambda: None, broadcast_fn=cards.append,
            now=datetime.now(IST) + timedelta(days=2))
        [x] = res["exits"]
        assert x["reason"] == "stop_loss" and x["mode"] == "PAPER_CAPITAL"
        [s] = res["settlements"]
        assert s["pnl_net"] < 0
        assert desk.desk_state(conn)["deployed"] == 0.0
        assert pm.equity(conn) == 1_000_000.0 + s["pnl_net"]
        conn.close()


def test_live_cycle_skips_out_of_zone_and_stale_tiers():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = _firm_conn()
        journal = tmp / "shadow.jsonl"
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("TCS")]}, [_level_row("TCS")])
        # Quote ABOVE the strict zone ceiling -> watched, never chased.
        res = desk.run_darling_live_cycle(
            tiers_path=tiers, levels_path=levels, path=journal, conn=conn,
            quote_fn=lambda t: 2400.0, check_fn=_allow_all, universe={},
            vix_fn=lambda: None, broadcast_fn=lambda c: None)
        assert res["entries"] == []
        # Stale tier table -> no NEW entries at all (exits would still run).
        old = (datetime.now(IST) - timedelta(days=6)).isoformat()
        tiers.write_text(json.dumps(
            {"as_of": old,
             "tiers": {"strong_buy": [_tier_row("TCS")]}}))
        res = desk.run_darling_live_cycle(
            tiers_path=tiers, levels_path=levels, path=journal, conn=conn,
            quote_fn=lambda t: 2250.0, check_fn=_allow_all, universe={},
            vix_fn=lambda: None, broadcast_fn=lambda c: None)
        assert res["entries"] == [] and res["tiers_fresh"] is False
        conn.close()


def test_funding_rejection_keeps_the_telemetry_row_live_path():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conn = _firm_conn()
        ft.set_budget(conn, 10000.0, "squeeze")       # nothing can fund
        journal = tmp / "shadow.jsonl"
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("TCS")]}, [_level_row("TCS")])
        res = desk.run_darling_live_cycle(
            tiers_path=tiers, levels_path=levels, path=journal, conn=conn,
            quote_fn=lambda t: 2250.0, check_fn=_allow_all, universe={},
            vix_fn=lambda: None, broadcast_fn=lambda c: None)
        [e] = res["entries"]                          # telemetry survives
        assert e["mode"] == "PAPER_TELEMETRY" and e["capital_allocated"] == 0
        assert not e["funding"]["funded"]
        conn.close()


def test_sweep_reconciles_orphan_locks():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _firm_conn()
        journal = Path(tmp) / "shadow.jsonl"
        entry = _entry()
        entry["funding"] = desk.fund_entry(entry, conn=conn)
        kg.log_event(entry, path=journal)
        kg.log_event({"event": "exit", "id": entry["id"],
                      "ticker": "TCS.NS", "reason": "target",
                      "exit_price": 2460.0}, path=journal)
        swept = desk.sweep_orphan_locks(ledger_path=journal, conn=conn)
        assert len(swept) == 1 and desk.desk_state(conn)["deployed"] == 0.0
        assert desk.sweep_orphan_locks(ledger_path=journal, conn=conn) == []
        conn.close()


def test_render_book_lines_live_view():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _firm_conn()
        journal = Path(tmp) / "shadow.jsonl"
        entry = _entry()
        entry["funding"] = desk.fund_entry(entry, conn=conn)
        entry["mode"] = "PAPER_CAPITAL"
        kg.log_event(entry, path=journal)
        kg.log_event(_entry("INFY"), path=journal)    # telemetry: not money
        out = desk.render_book_lines(conn=conn, path=journal,
                                     quote_fn=lambda t: 2300.0)
        assert "EQUITY DESK (live): 1 open" in out
        assert "budget Rs.300,000" in out and "TCS" in out
        assert f"{(2300.0 - 2269.0) * 16:+,.0f}" in out
        assert "INFY" not in out
        # Absent quote -> em-dash, counted, never guessed.
        out = desk.render_book_lines(conn=conn, path=journal,
                                     quote_fn=lambda t: None)
        assert "(1 unmarked)" in out and "—" in out
        conn.close()


def test_report_card_carries_the_equity_section():
    from src.portfolio_report import build_report_payload
    payload = build_report_payload([], 0, 0, None, datetime(2026, 7, 21),
                                   equity_section="EQUITY DESK (live): 1 open")
    assert "EQUITY DESK (live): 1 open" in payload["description"]
    bare = build_report_payload([], 0, 0, None, datetime(2026, 7, 21))
    assert "EQUITY DESK" not in bare["description"]


def test_eod_card_gains_the_desk_field():
    from src import eod_summary
    real_read = eod_summary._read_journal
    real_q = eod_summary.query_todays_resolutions
    real_render = desk.render_book_lines
    try:
        eod_summary._read_journal = lambda path=None: []
        eod_summary.query_todays_resolutions = lambda db_path=None: []
        desk.render_book_lines = lambda **kw: "EQUITY DESK (live): 2 open"
        card = eod_summary.build_eod_card()
        [f] = [f for f in card["fields"] if f["name"] == "💼 Equity Desk"]
        assert f["value"] == "EQUITY DESK (live): 2 open"
    finally:
        eod_summary._read_journal = real_read
        eod_summary.query_todays_resolutions = real_q
        desk.render_book_lines = real_render


def test_proposer_import_contract_still_holds():
    imports = [ln for ln in Path(sp.__file__).read_text().splitlines()
               if ln.strip().startswith(("import ", "from "))]
    for forbidden in ("equity_desk", "portfolio_manager", "options_proposer",
                      "notifier", "firm_treasury"):
        hits = [ln for ln in imports if forbidden in ln]
        assert not hits, f"proposer must not import {forbidden}: {hits}"


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_equity_desk.py")

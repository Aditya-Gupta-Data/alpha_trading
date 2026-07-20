"""
Equity desk (owner ruling 2026-07-20) — hermetic tests.

The darling shadow book's paper-capital layer: Dept 3's conn-generic
portfolio_manager reused against the desk's own sqlite file, risk-based
whole-share sizing, notional locks, delivery-friction-net settlement,
and the injection seams that keep equity_shadow_proposer import-clean.
Fully offline: desk DBs, kg journals, tier tables all in tmp paths. Run:
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
import src.equity_shadow_proposer as sp
from src import knowledge_graph_logger as kg
from src import portfolio_manager as pm

IST = timezone(timedelta(hours=5, minutes=30))

# Pin the config knobs the module read at import time so a future
# config.json edit can never silently change what these tests assert.
desk.EQUITY_DESK_ENABLED = True
desk.EQUITY_DESK_CAPITAL_RS = 300000.0
desk.EQUITY_DESK_RISK_PER_TRADE_PCT = 1.0
desk.EQUITY_DESK_MAX_NOTIONAL_PCT = 15.0


def _tier_row(sym="TCS", close=2269.0, stop=2085.0, val=35, tier="strong_buy",
              in_zone=True, pinned=None):
    return {"symbol": sym, "valuation": val, "forensic": 64, "close": close,
            "buy_zone": [2189.72, 2293.28], "stop": stop,
            "extension": "normal", "tier": tier,
            "family": {"strong_buy": "buy", "strong_sell": "sell"}.get(
                tier, "hold"),
            "in_zone": in_zone, "pinned": pinned,
            "rule": f"in zone + valuation {val} <= 45"}


def _level_row(sym="TCS", trims=(2460.0, 2510.0)):
    return {"symbol": sym, "status": "ok", "trim_levels": list(trims),
            "anchored_vwap": 2240.0}


def _write_artifacts(tmp, rows_by_tier, level_rows):
    tiers = tmp / "darling_tiers.json"
    levels = tmp / "darlings_levels.json"
    tiers.write_text(json.dumps({"tiers": rows_by_tier}))
    levels.write_text(json.dumps({"levels": level_rows}))
    return tiers, levels


def _allow_all(proposal):
    return {"allowed": True, "blocked_by": None, "reason": None}


def _entry(sym="TCS", close=2269.0, stop=2085.0):
    return sp.evaluate_darling_entry(_tier_row(sym, close=close, stop=stop),
                                     _level_row(sym), "2026-07-20")


def test_desk_account_bootstraps_at_slice_and_persists():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "desk.db"
        conn = desk.connect(db)
        assert pm.equity(conn) == 300000.0          # the slice, never 10L
        pm.request_entry(conn, "x", 1000.0)
        pm.release_margin(conn, "x", -5000.0)
        conn.close()
        conn = desk.connect(db)                     # reopen: no reset
        assert pm.equity(conn) == 295000.0
        conn.close()


def test_sizing_risk_budget_notional_cap_and_refusals():
    # Risk budget binds: 1% of 3L = 3000; (2269-2085)=184/share -> 16 sh.
    s = desk.size_entry(2269.0, 2085.0, 300000.0)
    assert s["qty"] == 16 and s["notional"] == round(16 * 2269.0, 2)
    # Notional cap binds: tight stop would buy 1500 sh; 15% cap -> 450.
    s = desk.size_entry(100.0, 98.0, 300000.0)
    assert s["qty"] == 450 and s["notional"] == 45000.0
    # Refusals: non-positive risk; a share pricier than the whole cap.
    assert desk.size_entry(100.0, 100.0, 300000.0)["qty"] == 0
    assert desk.size_entry(500000.0, 499000.0, 300000.0)["qty"] == 0


def test_fund_entry_locks_notional_and_respects_kill_switch():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "desk.db"
        f = desk.fund_entry(_entry(), db_path=db)
        assert f["funded"] and f["qty"] == 16
        conn = desk.connect(db)
        assert pm.locked_margin(conn) == f["notional"]
        conn.close()
        desk.EQUITY_DESK_ENABLED = False
        try:
            assert desk.fund_entry(_entry("INFY"), db_path=db) == {
                "funded": False, "reason": "equity desk disabled"}
        finally:
            desk.EQUITY_DESK_ENABLED = True


def test_fund_entry_exhaustion_and_risk_of_ruin_halt():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "desk.db"
        conn = desk.connect(db)
        pm.request_entry(conn, "hog", 299000.0)     # drain liquid cash
        conn.close()
        f = desk.fund_entry(_entry(), db_path=db)
        assert not f["funded"] and "margin exhaustion" in f["reason"]
        # A 13% drawdown trips the shared 10% risk-of-ruin halt.
        db2 = Path(tmp) / "desk2.db"
        conn = desk.connect(db2)
        pm.request_entry(conn, "x", 1000.0)
        pm.release_margin(conn, "x", -40000.0)
        conn.close()
        f = desk.fund_entry(_entry(), db_path=db2)
        assert not f["funded"] and "risk-of-ruin" in f["reason"]


def test_settle_exit_nets_delivery_frictions_and_releases():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "desk.db"
        entry = _entry()
        entry["funding"] = desk.fund_entry(entry, db_path=db)
        qty = entry["funding"]["qty"]
        s = desk.settle_exit(entry, {"ticker": "TCS.NS", "reason": "target",
                                     "exit_price": 2460.0}, db_path=db)
        gross = (2460.0 - 2269.0) * qty
        expected = round(gross - desk.delivery_frictions("BUY", 2269.0, qty)
                         - desk.delivery_frictions("SELL", 2460.0, qty), 2)
        assert s["pnl_net"] == expected and s["pnl_net"] < gross
        conn = desk.connect(db)
        assert pm.locked_margin(conn) == 0.0
        assert pm.equity(conn) == 300000.0 + expected
        conn.close()
        # Unfunded entries never settle.
        assert desk.settle_exit(_entry("INFY"),
                                {"exit_price": 100.0}, db_path=db) is None


def test_cycle_funds_stamps_and_settles_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, journal = tmp / "desk.db", tmp / "shadow.jsonl"
        capital_fn = lambda e: desk.fund_entry(e, db_path=db)   # noqa: E731
        settle_fn = lambda h, x: desk.settle_exit(h, x, db_path=db)  # noqa: E731

        # Day 1: TCS is strong_buy -> funded PAPER_CAPITAL entry.
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("TCS")]}, [_level_row("TCS")])
        res = sp.run_darling_cycle(
            tiers_path=tiers, levels_path=levels, path=journal,
            quote_fn=lambda t: 2269.0, universe={}, check_fn=_allow_all,
            as_of="2026-07-20", capital_fn=capital_fn, settle_fn=settle_fn)
        [e] = res["entries"]
        assert e["mode"] == "PAPER_CAPITAL"
        assert e["capital_allocated"] == e["funding"]["notional"] > 0
        assert e["kya_kara_action"]["qty"] == 16
        assert res["settlements"] == []

        # Day 2: TCS graded strong_sell -> forced exit settles the desk.
        tiers, levels = _write_artifacts(
            tmp, {"strong_sell": [_tier_row("TCS", tier="strong_sell",
                                            val=88)]}, [])
        res = sp.run_darling_cycle(
            tiers_path=tiers, levels_path=levels, path=journal,
            quote_fn=lambda t: 2100.0, universe={}, check_fn=_allow_all,
            as_of="2026-07-21", capital_fn=capital_fn, settle_fn=settle_fn,
            now=datetime(2026, 7, 21, 18, 0, tzinfo=IST))
        [x] = res["exits"]
        assert x["mode"] == "PAPER_CAPITAL"         # entry stamp rides along
        [s] = res["settlements"]
        assert s["reason"] == "strong_sell_tier" and s["pnl_net"] < 0
        conn = desk.connect(db)
        assert pm.locked_margin(conn) == 0.0
        assert pm.equity(conn) == 300000.0 + s["pnl_net"]
        conn.close()


def test_cycle_without_injection_stays_pure_telemetry():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("TCS")]}, [_level_row("TCS")])
        res = sp.run_darling_cycle(
            tiers_path=tiers, levels_path=levels, path=tmp / "s.jsonl",
            quote_fn=lambda t: 2269.0, universe={}, check_fn=_allow_all,
            as_of="2026-07-20")
        [e] = res["entries"]
        assert e["mode"] == "PAPER_TELEMETRY" and e["capital_allocated"] == 0
        assert "funding" not in e and res["settlements"] == []


def test_funding_rejection_keeps_the_telemetry_row():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tiers, levels = _write_artifacts(
            tmp, {"strong_buy": [_tier_row("TCS")]}, [_level_row("TCS")])
        logged = sp.propose_darling_entries(
            tiers_path=tiers, levels_path=levels, path=tmp / "s.jsonl",
            as_of="2026-07-20", check_fn=_allow_all, universe={},
            nifty_trend_fn=lambda: None,
            capital_fn=lambda e: {"funded": False,
                                  "reason": "margin exhaustion: test"})
        [e] = logged
        assert e["mode"] == "PAPER_TELEMETRY" and e["capital_allocated"] == 0
        assert e["funding"] == {"funded": False, "qty": None,
                                "notional": None, "lock_ref": None,
                                "reason": "margin exhaustion: test"}
        # A capital_fn that CRASHES also degrades to telemetry, never loss.
        logged = sp.propose_darling_entries(
            tiers_path=tiers, levels_path=levels, path=tmp / "s2.jsonl",
            as_of="2026-07-20", check_fn=_allow_all, universe={},
            nifty_trend_fn=lambda: None,
            capital_fn=lambda e: 1 / 0)
        assert logged[0]["mode"] == "PAPER_TELEMETRY"
        assert "desk unavailable" in logged[0]["funding"]["reason"]


def test_sweep_reconciles_orphan_locks():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, journal = tmp / "desk.db", tmp / "shadow.jsonl"
        entry = _entry()
        entry["funding"] = desk.fund_entry(entry, db_path=db)
        kg.log_event(entry, path=journal)
        # Exit logged but its settlement crashed -> lock left behind.
        kg.log_event({"event": "exit", "id": entry["id"],
                      "ticker": "TCS.NS", "reason": "target",
                      "exit_price": 2460.0}, path=journal)
        swept = desk.sweep_orphan_locks(ledger_path=journal, db_path=db)
        assert len(swept) == 1 and swept[0]["ticker"] == "TCS.NS"
        conn = desk.connect(db)
        assert pm.locked_margin(conn) == 0.0
        conn.close()
        # Idempotent: nothing left to sweep.
        assert desk.sweep_orphan_locks(ledger_path=journal, db_path=db) == []


def test_broadcast_activity_one_card_quiet_days_silent():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "desk.db"
        desk.connect(db).close()
        sent = []
        entry = dict(_entry(), funding={"funded": True, "qty": 16,
                                        "notional": 36304.0})
        shadow = {"entries": [entry],
                  "settlements": [{"ticker": "INFY.NS", "reason": "target",
                                   "pnl_net": 1200.5}]}
        assert desk.broadcast_activity(shadow, broadcast_fn=sent.append,
                                       db_path=db)
        [card] = sent
        assert card["event"] == "equity_desk"
        assert "BUY TCS.NS: 16 sh" in card["description"]
        assert "+Rs.1,200.50 net" in card["description"]
        # No money moved -> no card, ever.
        assert not desk.broadcast_activity({"entries": [], "settlements": []},
                                           broadcast_fn=sent.append)
        assert len(sent) == 1


def test_reserve_firm_slice_is_idempotent_on_the_options_account():
    conn = sqlite3.connect(":memory:")
    v1 = desk.reserve_firm_slice(conn=conn)
    v2 = desk.reserve_firm_slice(conn=conn)
    assert v1["approved"] and v2["approved"]
    assert "already locked" in v2["reason"]
    assert pm.locked_margin(conn) == desk.EQUITY_DESK_CAPITAL_RS  # never 2x
    assert pm.get_account(conn)["starting_capital"] == 1_000_000.0
    conn.close()


def test_proposer_import_contract_still_holds():
    """The capital era must not leak Dept 3 into the proposer: funding
    arrives ONLY via the injected seams at the composition root."""
    imports = [ln for ln in Path(sp.__file__).read_text().splitlines()
               if ln.strip().startswith(("import ", "from "))]
    for forbidden in ("equity_desk", "portfolio_manager", "options_proposer",
                      "notifier"):
        hits = [ln for ln in imports if forbidden in ln]
        assert not hits, f"proposer must not import {forbidden}: {hits}"


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_equity_desk.py")

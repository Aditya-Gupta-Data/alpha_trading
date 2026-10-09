"""Audit Chunk 6: E1 own stop, E2 intraday extreme, E4 entry-day bar, E5 darling gate, E7 cancel, K2 tool, K4 labels."""
import sqlite3
from src import equity_trail as et


def _bars(n=20, base=100.0):
    return [(f"2026-09-{d:02d}", base - 2, base + 2, base) for d in range(1, n + 1)]


def test_e4_entry_day_bar_contributes_its_close_not_its_high():
    bars = _bars(19) + [("2026-09-20", 99.0, 112.0, 101.0)]
    t = et.compute_trail("2026-09-20", 90.0, bars, 3.0, 14, live_price=101.0)
    assert t["extreme"] == 101.0


def test_e2_the_intraday_extreme_is_remembered_across_cycles(monkeypatch):
    et._EXTREMES.clear()
    bars = _bars(19) + [("2026-09-20", 99.0, 103.0, 101.0), ("2026-09-21", 100.0, 104.0, 102.0)]
    monkeypatch.setattr(et, "_trail_for_position_raw",
                        lambda entry, live_price=None, **kw: dict(et.compute_trail("2026-09-20", 90.0, bars, 3.0, 14, live_price=live_price),
                                                                  atr_mult=3.0, atr_n=14, reason=""))
    entry = {"id": "e1", "as_of": "2026-09-20T09:30:00", "ticker": "X.NS", "kya_kara_action": {"stop": 90.0}}
    spike = et.trail_for_position(entry, 125.0)
    dip = et.trail_for_position(entry, 110.0)
    assert spike["armed"] and dip["extreme"] == 125.0 and dip["trail"] == spike["trail"]
    assert et.trail_hit(dip["trail"], 110.0)


def test_e1_strong_sell_on_a_moved_pricer_stop_respects_the_positions_own_stop(tmp_path, monkeypatch):
    from src import equity_shadow_proposer as esp, knowledge_graph_logger as kg
    ledger = tmp_path / "l.jsonl"
    kg.log_event({"event": "entry", "id": "d1", "ticker": "DIXON.NS", "as_of": "2026-09-24",
                  "kyu_trigger": {"setup": "darling_buy"}, "kya_kara_action": {"entry_price": 13200.0, "stop": 10149.0}}, path=ledger)
    import json
    tp = tmp_path / "tiers.json"
    tp.write_text(json.dumps({"tiers": {"strong_sell": [{"symbol": "DIXON", "rule": "close below the hard stop"}]}}))
    out = esp.force_exit_strong_sell(tiers_path=tp, path=ledger, quote_fn=lambda t: 13228.0)
    assert out == [] and "DIXON.NS" in kg.open_positions(path=ledger)
    tp.write_text(json.dumps({"tiers": {"strong_sell": [{"symbol": "DIXON", "rule": "valuation 9 >= 8"}]}}))
    out = esp.force_exit_strong_sell(tiers_path=tp, path=ledger, quote_fn=lambda t: 13228.0)
    assert len(out) == 1


def test_e5_a_block_telemetry_row_does_not_block_a_darling_entry(tmp_path):
    from src import equity_shadow_proposer as esp, knowledge_graph_logger as kg
    ledger = tmp_path / "l.jsonl"
    kg.log_event({"event": "entry", "id": "b1", "ticker": "TCS.NS", "as_of": "2026-10-05",
                  "kyu_trigger": {"setup": "block_vwap_pullback"}, "kya_kara_action": {"entry_price": 1.0, "stop": 0.5}}, path=ledger)
    events = kg.read_events(ledger)
    open_now = {t: e for t, e in kg.open_positions(events=events).items() if esp._is_darling(e)}
    assert "TCS.NS" not in open_now and "TCS.NS" in kg.open_positions(events=events)


def test_k2_repair_tool_plans_and_applies_on_a_temp_db(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rk2", "scripts/repair_k2_shadow_trades.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    db = tmp_path / "bm.db"; c = sqlite3.connect(db)
    c.execute("CREATE TABLE shadow_trades (journal_ref TEXT PRIMARY KEY, pattern_id TEXT, fire_date TEXT, ticker TEXT, direction TEXT, resolved INTEGER, result TEXT, r_multiple REAL, resolution_date TEXT, created_at TEXT, host_ref TEXT, mode TEXT)")
    c.execute("CREATE TABLE outcomes (journal_ref TEXT, date TEXT, ticker TEXT, archetype TEXT, r_multiple REAL, result TEXT)")
    c.execute("CREATE TABLE account_events (ts TEXT, event_type TEXT, detail TEXT)")
    c.executemany("INSERT INTO shadow_trades VALUES (?, 'p', '2026-09-01', 'N', 'bullish', 1, ?, ?, '2026-09-10', 'x', ?, 'BLOCKED_BY_RISK')",
                  [("s1", "loss", -1.15, "h1"), ("s2", "loss", -1.15, "h2"), ("s3", "win", 1.0, "h3")])
    c.executemany("INSERT INTO outcomes VALUES (?, '2026-09-12', 'N', 'bear_put_spread', ?, ?)", [("h1", 0.8, "win"), ("h3", 1.0, "win")])
    c.commit()
    rows = m.plan(c)
    assert {r["ref"]: r["action"] for r in rows} == {"s1": "re-resolve", "s2": "unresolve"}
    assert m.apply(c, rows) == 2
    assert tuple(c.execute("SELECT result, r_multiple FROM shadow_trades WHERE journal_ref='s1'").fetchone()) == ("win", 0.8)
    assert tuple(c.execute("SELECT resolved FROM shadow_trades WHERE journal_ref='s2'").fetchone()) == (0,)
    assert m.plan(c) == []

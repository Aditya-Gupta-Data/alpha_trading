"""scripts/weekly_audit.py (2026-10-23): the read-only weekly architecture audit. A fabricated host in tmp_path;
nothing is read from or written to the real data/ or logs/."""
import importlib.util
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("weekly_audit", ROOT / "scripts" / "weekly_audit.py")
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)

SINCE, UNTIL = date(2026, 10, 19), date(2026, 10, 23)


def _host(tmp):
    (tmp / "data").mkdir()
    (tmp / "logs").mkdir()
    (tmp / "config.json").write_text(json.dumps({"event_engine": {"execute_enabled": True, "emit_enabled": True,
                                                                  "max_loss_per_trade_rs": 5000}}))
    rows = [
        {"session_date": "2026-10-20", "underlying": "INFY.NS", "fate": "REJECTED_OTHER",
         "reason": "earnings proximity gate: results in 3 day(s) (<= 5) — iron_condor refused (#151)"},
        {"session_date": "2026-10-21", "underlying": "INFY.NS", "fate": "REJECTED_OTHER",
         "reason": "earnings proximity gate: results in 2 day(s) (<= 5) — bear_put_spread refused (#151)"},
        {"session_date": "2026-10-22", "underlying": "NIFTY 50", "fate": "EXECUTED", "reason": "ok"},
        {"session_date": "2026-10-01", "underlying": "TCS.NS", "fate": "REJECTED_OTHER",
         "reason": "earnings proximity gate: results in 1 day(s) (<= 5) — iron_condor refused (#151)"},   # outside window
    ]
    (tmp / "data" / "proposal_ledger.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    db = sqlite3.connect(tmp / "data" / "brain_map.db")
    db.executescript("""
CREATE TABLE greek_attribution (journal_ref TEXT PRIMARY KEY, as_of TEXT NOT NULL, ticker TEXT, strategy TEXT,
  coverage REAL, components TEXT, taylor_total REAL, actual_window_pnl REAL, residual REAL, trade_pnl REAL,
  outside_window REAL, method TEXT, reason TEXT);
CREATE TABLE paper_accounts (account_id TEXT PRIMARY KEY, starting_capital REAL, realized_pnl REAL, peak_equity REAL, created_at TEXT);
""")
    db.execute("INSERT INTO greek_attribution VALUES ('r1','2026-10-22T15:40:00','NIFTY 50','bull_call_spread',1.0,?,"
               "120.5,118.0,-2.5,125.0,7.0,'taylor',NULL)",
               (json.dumps({"delta": 90, "gamma": 12, "theta": -8, "vega": 26.5, "volga": 0, "vanna": 0, "charm": 0}),))
    db.execute("INSERT INTO paper_accounts VALUES ('PAPER_EVENTS',1000000,500,1000500,'2026-10-21')")
    db.commit()
    db.close()
    from src.event_engine import store
    conn = sqlite3.connect(tmp / "data" / "brain_map.db")
    store.ensure_schema(conn)
    conn.execute("INSERT INTO event_cards_seen VALUES ('e1','2026-10-22T09:35:00+05:30','executed','2 suggestion(s)')")
    for ref, path, state, pnl, mark in (("a", "fixed", "closed", 1200.0, None), ("b", "llm", "open", None, 3.0)):
        conn.execute("INSERT INTO event_positions (journal_ref,event_id,underlying,direction,path,option_type,strike,expiry,"
                     "lots,lot_size,entry_premium,entry_total_rs,opened_at,horizon_days,state,pnl_net,last_mark) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ref, "e1", "NIFTY 50", "bullish", path, "CE", 25000, "2026-11-26", 10, 65, 2.0, 1300.0,
                      "2026-10-22T09:36:00+05:30", 30, state, pnl, mark))
    conn.execute("INSERT INTO event_actions (ts,event_id,ticker,path,action,reason) VALUES "
                 "('2026-10-22T09:36:00','e1','NIFTY',NULL,'abstained','one lot costs Rs.6,500 > the Rs.5,000 cap')")
    conn.commit()
    conn.close()
    (tmp / "data" / "event_card__e1.json").write_text("{}")


def test_report_counts_the_week_and_names_what_it_found(tmp_path):
    _host(tmp_path)
    r = wa.build_report(tmp_path, SINCE, UNTIL)
    assert r.startswith("WEEKLY_AUDIT_REPORT")
    assert "refused by the earnings proximity gate: 2" in r and "INFY: 2 refusal(s)" in r
    assert "iron_condor 1" in r and "bear_put_spread 1" in r and "TCS" not in r          # the 10-01 row is outside the window
    assert "rows with delta + gamma + theta + vega all present: 1 of 1" in r and "SAMPLE:" in r
    assert "Path A fixed 3% OTM: 1 opened, 1 closed, realised Rs.+1,200" in r
    assert "Path B LLM strike: 1 opened, 0 closed" in r and "open mark-to-bid Rs.+650" in r      # (3-2)*65*10
    assert "PLANNER ONLY" in r and "execute_enabled=True" in r and "cards shipped to this host (data/event_card__*.json): 1" in r
    assert "one lot costs Rs.6,500" in r


def test_an_empty_host_says_no_data_instead_of_inventing_numbers(tmp_path):
    (tmp_path / "data").mkdir()
    r = wa.build_report(tmp_path, SINCE, UNTIL)
    assert "NO DATA: data/proposal_ledger.jsonl is not on this host" in r
    assert "NO DATA: data/brain_map.db is not on this host" in r
    assert "SECTION FAILED" not in r


def test_missing_tables_are_named_not_hidden(tmp_path):
    (tmp_path / "data").mkdir()
    sqlite3.connect(tmp_path / "data" / "brain_map.db").close()
    r = wa.build_report(tmp_path, SINCE, UNTIL)
    assert "table `greek_attribution` DOES NOT EXIST" in r
    assert "event_positions DO NOT EXIST" in r


def test_the_audit_never_writes(tmp_path):
    _host(tmp_path)
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file())
    wa.build_report(tmp_path, SINCE, UNTIL)
    after = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file())
    assert before == after
    assert "mode=ro" in (ROOT / "scripts" / "weekly_audit.py").read_text()

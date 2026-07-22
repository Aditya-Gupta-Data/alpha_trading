"""
Firm MTM + return line (#84 Directive 6) — hermetic tests. Read-only
reporting: MTM composition, the day-1 CAGR edge (absolute return until
day 30, true CAGR after), partial-mark honesty. Run:
    python -m pytest tests/test_firm_mtm.py
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.firm_mtm as fm
from src import knowledge_graph_logger as kg
from src import portfolio_manager as pm

IST = timezone(timedelta(hours=5, minutes=30))


def _acct(days_ago=0, realized=0.0):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pm.get_account(conn)
    conn.execute("UPDATE account_state SET starting_capital=200000, "
                 "realized_pnl=?, peak_equity=200000 WHERE id=1",
                 (realized,))
    ts = (datetime.now(IST) - timedelta(days=days_ago)).replace(
        tzinfo=None).isoformat(timespec="seconds")
    conn.execute("INSERT INTO account_events (ts, event_type, detail) "
                 "VALUES (?, 'clean_sheet', 'test epoch')", (ts,))
    conn.commit()
    return conn


def _ledger(tmp, unreal_quote=2300.0):
    path = Path(tmp) / "shadow.jsonl"
    kg.log_event({"event": "entry", "id": "e1", "ticker": "TCS.NS",
                  "funding": {"funded": True, "qty": 10},
                  "kya_kara_action": {"entry_price": 2269.0}}, path=path)
    kg.log_event({"event": "entry", "id": "e2", "ticker": "INFY.NS",
                  "kyu_trigger": {}}, path=path)     # telemetry: excluded
    return path, (lambda t: unreal_quote)


def test_mtm_composes_cash_options_and_equity():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _acct(days_ago=2, realized=1500.0)
        ledger, quote = _ledger(tmp)
        m = fm.compute(conn=conn, entries=[],
                       marks=[{"live_pnl_rs": 800.0}],
                       ledger_path=ledger, quote_fn=quote)
        assert m["equity_realized"] == 201500.0
        assert m["options_unrealized"] == 800.0
        assert m["equity_unrealized"] == (2300.0 - 2269.0) * 10
        assert m["mtm"] == 201500.0 + 800.0 + 310.0
        assert m["days"] == 2 and m["cagr"] is None    # day-1 edge: no CAGR
        assert abs(m["abs_return"] - (m["mtm"] - 200000) / 200000) < 1e-9
        conn.close()


def test_cagr_unlocks_only_past_the_day_floor():
    with tempfile.TemporaryDirectory() as tmp:
        ledger, quote = _ledger(tmp, unreal_quote=None)
        young = fm.compute(conn=_acct(days_ago=fm.CAGR_MIN_DAYS - 1,
                                      realized=4000.0),
                           entries=[], marks=[], ledger_path=ledger,
                           quote_fn=quote)
        assert young["cagr"] is None
        old = fm.compute(conn=_acct(days_ago=45, realized=4000.0),
                         entries=[], marks=[], ledger_path=ledger,
                         quote_fn=quote)
        expected = (old["mtm"] / 200000.0) ** (365.0 / 45) - 1
        assert abs(old["cagr"] - expected) < 1e-9


def test_render_line_states_the_edge_and_partial_marks():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _acct(days_ago=1, realized=0.0)
        ledger, _ = _ledger(tmp)
        line = fm.render_line(conn=conn, entries=[], marks=[],
                              ledger_path=ledger, quote_fn=lambda t: None)
        assert "Firm MTM Rs.200,000" in line
        assert "CAGR unlocks at day 30" in line and "(day 1" in line
        assert "1 position(s) unmarked" in line       # honest partial
        conn.close()
    # Past the floor the line carries BOTH numbers.
    with tempfile.TemporaryDirectory() as tmp:
        ledger, quote = _ledger(tmp)
        line = fm.render_line(conn=_acct(days_ago=60, realized=9000.0),
                              entries=[], marks=[], ledger_path=ledger,
                              quote_fn=quote)
        assert "CAGR" in line and "Absolute" in line and "day 60" in line


def test_digests_carry_the_mtm_line():
    from src import ceo_brief, eod_summary
    real = fm.render_line
    real_read = eod_summary._read_journal
    real_q = eod_summary.query_todays_resolutions
    try:
        import src.firm_mtm
        src.firm_mtm.render_line = lambda **kw: "💹 Firm MTM Rs.200,123"
        eod_summary._read_journal = lambda path=None: []
        eod_summary.query_todays_resolutions = lambda db_path=None: []
        card = eod_summary.build_eod_card()
        assert card["fields"][0]["name"] == "💹 Firm MTM & Return"
        assert "200,123" in card["fields"][0]["value"]
        with tempfile.TemporaryDirectory() as tmp:
            brief = ceo_brief.build_brief_card(
                logs_dir=Path(tmp), state_path=Path(tmp) / "s.json",
                deploy_log_path=Path(tmp) / "d.jsonl",
                repo_root=Path(tmp))
        [risk] = [f for f in brief["fields"]
                  if f["name"] == "💰 Risk & Capital"]
        assert "200,123" in risk["value"]
    finally:
        src.firm_mtm.render_line = real
        eod_summary._read_journal = real_read
        eod_summary.query_todays_resolutions = real_q


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_firm_mtm.py")

"""
Tests for src/portfolio_report.py — the 2-hourly read-only report card.

Offline — injected journal entries, spots, clocks and notifiers; the
exposure read runs against a seeded temp brain_map.db and is verified to
go through a READ-ONLY connection.

Run:
    python tests/test_portfolio_report.py
    pytest tests/test_portfolio_report.py -v
"""

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import portfolio_report as pr
from src.market_loop import IST
from src.notifier import _build_embed

MARKET_OPEN_NOW = datetime(2026, 7, 10, 11, 0, tzinfo=IST)    # Friday 11:00
MARKET_CLOSED_NOW = datetime(2026, 7, 11, 11, 0, tzinfo=IST)  # Saturday


def _spread_entry(short_id="sprd0001", ticker="NIFTY 50", outcome=None):
    return {
        "short_id": short_id, "date": "2026-07-08", "action": "SPREAD",
        "ticker": ticker, "shares": 75, "price": 72.3, "decision": "approved",
        "spread": {"strategy": "bear_put_spread",
                   "legs": [{"side": "BUY", "option_type": "PE",
                             "strike": 24150.0, "premium": 181.75},
                            {"side": "SELL", "option_type": "PE",
                             "strike": 23950.0, "premium": 109.45}],
                   "lot_size": 75, "lots": 1, "expiry": "2026-07-21",
                   "net_debit": 72.3, "net_credit": None,
                   "max_loss": 5422.5, "max_profit": 9577.5},
        "outcome": outcome,
    }


def _equity_entry(short_id="eqty0001", price=242.5, shares=106):
    return {
        "short_id": short_id, "date": "2026-07-03", "action": "BUY",
        "ticker": "ONGC.NS", "shares": shares, "price": price,
        "decision": "approved",
        "plan": {"variant": "breakout", "stop_loss": 235.0, "target": 260.0},
        "outcome": None,
    }


# ------------------------------------------------------------- open book

def test_open_entries_apply_the_tracker_predicates():
    entries = [
        _spread_entry(), _equity_entry(),
        _spread_entry("done0001", outcome={"resolution": "closed"}),
        dict(_spread_entry("rej00001"), decision="rejected"),
        dict(_spread_entry("pend0001"), decision="pending_approval"),
    ]
    spreads, equities = pr._open_entries(entries)
    assert [e["short_id"] for e in spreads] == ["sprd0001"]
    assert [e["short_id"] for e in equities] == ["eqty0001"]


# ---------------------------------------------------------------- marking

def test_mark_positions_equity_math_and_spread_structure():
    spreads, equities = pr._open_entries([_spread_entry(), _equity_entry()])
    spots = {"NIFTY 50": 23900.0, "ONGC.NS": 250.0}
    marked = pr.mark_positions(spreads, equities, spots.get)
    by_id = {m["short_id"]: m for m in marked}
    # equity: (250 - 242.5) * 106 = 795.00, exact
    assert by_id["eqty0001"]["live_pnl_rs"] == 795.0
    assert "entry Rs.242.5" in by_id["eqty0001"]["detail"]
    # spread: marked through the tracker's model — pin the structure and
    # the sign (spot fell below both strikes: a bear put is in profit)
    assert by_id["sprd0001"]["live_pnl_rs"] > 0
    assert "% of max profit" in by_id["sprd0001"]["detail"]
    assert "d to expiry" in by_id["sprd0001"]["detail"]


def test_mark_positions_skips_quoteless_tickers_instead_of_guessing():
    spreads, equities = pr._open_entries([_spread_entry(), _equity_entry()])
    marked = pr.mark_positions(spreads, equities,
                               {"ONGC.NS": 250.0}.get)   # no NIFTY quote
    assert [m["short_id"] for m in marked] == ["eqty0001"]


def test_mark_positions_survives_a_raising_spot_fn():
    def exploding(ticker):
        raise RuntimeError("DH-906")
    spreads, equities = pr._open_entries([_equity_entry()])
    assert pr.mark_positions(spreads, equities, exploding) == []


# --------------------------------------------------------------- exposure

def test_read_exposure_via_read_only_connection():
    from src import portfolio_manager as pm
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "brain_map.db"
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        pm.request_entry(conn, "sprd0001", 20422.5)
        conn.close()
        exposure = pr.read_exposure(db)
    assert exposure == {"locked_margin_rs": 20422.5,
                        "equity_rs": 1_000_000.0,
                        "realized_pnl_rs": 0.0,
                        "exposure_pct": 2.04}


def test_read_exposure_absent_db_or_tables_is_none():
    with tempfile.TemporaryDirectory() as tmp:
        assert pr.read_exposure(Path(tmp) / "nope.db") is None
        empty = Path(tmp) / "empty.db"
        sqlite3.connect(empty).close()
        assert pr.read_exposure(empty) is None


# ----------------------------------------------------------------- payload

def _mark(short_id, ticker, pnl):
    return {"short_id": short_id, "ticker": ticker, "strategy": "s",
            "live_pnl_rs": pnl, "detail": "detail"}


def test_payload_names_winner_loser_net_and_exposure():
    marked = [_mark("a", "NIFTY 50", 4200.0), _mark("b", "ONGC.NS", -795.0),
              _mark("c", "NIFTY BANK", 100.0)]
    exposure = {"locked_margin_rs": 40000.0, "equity_rs": 1_000_000.0,
                "exposure_pct": 4.0}
    payload = pr.build_report_payload(marked, 3, 0, exposure, MARKET_OPEN_NOW)
    assert payload["event"] == "portfolio_report"
    names = [f["name"] for f in payload["fields"]]
    values = {f["name"]: f["value"] for f in payload["fields"]}
    assert values["Open Positions"] == "3"
    assert values["Net Live P&L (marked)"] == "Rs.+3,505.00"
    assert "Top Winner — NIFTY 50" in names
    assert "Rs.+4,200.00" in values["Top Winner — NIFTY 50"]
    assert "Top Loser — ONGC.NS" in names
    assert "Rs.-795.00" in values["Top Loser — ONGC.NS"]
    assert "4.0%" in values["Exposure"]
    assert "Unmarked" not in names


def test_payload_single_position_has_no_loser_field_and_counts_unmarked():
    payload = pr.build_report_payload([_mark("a", "NIFTY 50", 500.0)],
                                      3, 2, None, MARKET_OPEN_NOW)
    names = [f["name"] for f in payload["fields"]]
    assert "Top Winner — NIFTY 50" in names
    assert not any(n.startswith("Top Loser") for n in names)
    assert not any(n == "Exposure" for n in names)
    unmarked = next(f for f in payload["fields"] if f["name"] == "Unmarked")
    assert "2 position(s)" in unmarked["value"]


def test_payload_renders_as_the_purple_report_embed():
    payload = pr.build_report_payload([_mark("a", "NIFTY 50", 500.0)],
                                      1, 0, None, MARKET_OPEN_NOW)
    embed = _build_embed(payload)
    assert embed["title"].startswith("🗂️ Portfolio Report Card — ")
    assert "2026-07-10 11:00 IST" in embed["title"]
    assert embed["color"] == 0x9B59B6
    assert embed["fields"] == payload["fields"]    # passthrough, untouched


# --------------------------------------------------------------- run cycle

def test_run_posts_during_market_hours_with_injected_seams():
    sent = []
    result = pr.run(entries=[_spread_entry(), _equity_entry()],
                    spot_fn={"NIFTY 50": 23900.0, "ONGC.NS": 250.0}.get,
                    db_path=Path("/nonexistent/brain_map.db"),
                    now_fn=lambda: MARKET_OPEN_NOW,
                    notify_fn=sent.append)
    assert result["posted"] is True
    assert len(sent) == 1
    assert sent[0]["event"] == "portfolio_report"
    values = {f["name"]: f["value"] for f in sent[0]["fields"]}
    assert values["Open Positions"] == "2"


def test_run_stays_silent_when_the_market_is_closed():
    sent = []
    result = pr.run(entries=[_spread_entry()], spot_fn=lambda t: None,
                    now_fn=lambda: MARKET_CLOSED_NOW, notify_fn=sent.append)
    assert result["posted"] is False
    assert result["reason"] == "market closed"
    assert sent == []


def test_run_force_posts_even_when_closed():
    sent = []
    result = pr.run(entries=[], spot_fn=lambda t: None,
                    db_path=Path("/nonexistent/brain_map.db"),
                    now_fn=lambda: MARKET_CLOSED_NOW,
                    notify_fn=sent.append, force=True)
    assert result["posted"] is True
    values = {f["name"]: f["value"] for f in sent[0]["fields"]}
    assert values["Open Positions"] == "0"


def test_cli_force_flag_wires_through():
    with mock.patch.object(pr, "run") as run_fn:
        pr.main(["--force"])
        pr.main([])
    assert run_fn.call_args_list[0][1]["force"] is True
    assert run_fn.call_args_list[1][1]["force"] is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

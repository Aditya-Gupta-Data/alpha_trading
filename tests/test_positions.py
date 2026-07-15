"""
Tests for the active-trade visibility stack: src/positions.py (the
read-only source of truth), the ASCII table renderer, the gateway's
GET /api/discord/positions, and the Discord bot's /positions embed.

Offline — the journal is an injected list or a mock (data/journal.jsonl
is never touched, per HANDOVER's "never reset live data" rule).

Run:
    python tests/test_positions.py
    pytest tests/test_positions.py -v
"""

import os
import sys
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.positions import active_positions, format_table

TODAY = date(2026, 7, 10)


def _spread_entry(short_id="sprd0001", decision="approved", outcome=None,
                  opened="2026-07-08"):
    return {
        "short_id": short_id, "date": opened, "action": "SPREAD",
        "ticker": "NIFTY 50", "shares": 75, "price": 72.3,
        "signal": "bearish trend read", "decision": decision, "why": "Test",
        "spread": {"strategy": "bear_put_spread", "lots": 1, "lot_size": 75,
                   "expiry": "2026-07-21", "net_debit": 72.3,
                   "net_credit": None, "max_loss": 5422.5,
                   "max_profit": 9577.5,
                   # real spread blocks always carry legs — active_positions
                   # now shares the tracker's _spread_trackable predicate,
                   # which (correctly) refuses a legless spread.
                   "legs": [{"side": "BUY", "option_type": "PE",
                             "strike": 24050.0, "premium": 207.75},
                            {"side": "SELL", "option_type": "PE",
                             "strike": 23850.0, "premium": 135.45}]},
        "outcome": outcome,
    }


def _equity_entry(short_id="eqty0001", decision="approved", outcome=None,
                  opened="2026-07-03"):
    return {
        "short_id": short_id, "date": opened, "action": "BUY",
        "ticker": "ONGC.NS", "shares": 106, "price": 242.5,
        "signal": "golden cross", "decision": decision, "why": "trend",
        "plan": {"variant": "breakout", "stop_loss": 235.0, "target": 260.0,
                 "risk_reward": 2.3, "max_loss_rs": 795.0},
        "outcome": outcome,
    }


# ------------------------------------------------------- active_positions

def test_only_open_approved_entries_are_active():
    entries = [
        _equity_entry(),                                        # open equity
        _spread_entry(),                                        # open spread
        _spread_entry("done0001", outcome={"resolution": "closed"}),  # closed
        _spread_entry("rej00001", decision="rejected"),         # rejected
        _spread_entry("pend0001", decision="pending_approval"), # not entered
        {"short_id": "exit0001", "date": "2026-07-01", "action": "SELL",
         "ticker": "TCS.NS", "decision": "approved", "outcome": None},
    ]
    open_pos = active_positions(entries, today=TODAY)
    assert [p["trade_id"] for p in open_pos] == ["sprd0001", "eqty0001"]


def test_spread_position_fields():
    p = active_positions([_spread_entry()], today=TODAY)[0]
    assert p["kind"] == "spread"
    assert p["ticker"] == "NIFTY 50"
    assert p["strategy"] == "bear_put_spread"
    assert p["entry_price"] == 72.3            # the net debit per share
    assert p["max_loss_rs"] == 5422.5          # per-lot figures × 1 lot
    assert p["max_profit_rs"] == 9577.5
    assert p["expiry"] == "2026-07-21"
    assert p["days_in_trade"] == 2             # 07-08 -> 07-10


def test_spread_rupee_bounds_scale_with_lots():
    e = _spread_entry()
    e["spread"]["lots"] = 3
    p = active_positions([e], today=TODAY)[0]
    assert p["max_loss_rs"] == 5422.5 * 3
    assert p["max_profit_rs"] == 9577.5 * 3


def test_equity_position_fields():
    p = active_positions([_equity_entry()], today=TODAY)[0]
    assert p["kind"] == "equity"
    assert p["entry_price"] == 242.5
    assert p["target"] == 260.0
    assert p["stop_loss"] == 235.0
    assert p["expiry"] is None
    assert p["days_in_trade"] == 7             # 07-03 -> 07-10


def test_undated_entry_survives_with_unknown_time_in_trade():
    e = _spread_entry()
    e["date"] = "not-a-date"
    p = active_positions([e], today=TODAY)[0]
    assert p["days_in_trade"] is None


def test_default_source_is_the_journal_file():
    with mock.patch("src.positions.journal.read_all",
                    return_value=[_spread_entry()]) as reader:
        assert len(active_positions(today=TODAY)) == 1
        reader.assert_called_once()


# ----------------------------------------------------------- ASCII table

def test_format_table_renders_both_kinds():
    table = format_table(active_positions(
        [_equity_entry(), _spread_entry()], today=TODAY))
    assert "NIFTY 50" in table and "ONGC.NS" in table
    assert "max +9,577.50" in table and "max -5,422.50" in table
    assert "260.00" in table and "235.00" in table        # equity target/stop
    assert "2d" in table and "7d" in table
    assert "2 open position(s)" in table
    # every rendered line of the table body is equally wide (a real table)
    lines = [l for l in table.splitlines() if l.startswith(("|", "+"))]
    assert len({len(l) for l in lines}) == 1


def test_format_table_empty_state():
    assert format_table([]) == "No open paper positions."


# ------------------------------------------------------ gateway endpoint

KEY = "gateway-secret"


def test_gateway_positions_endpoint_is_read_only_and_gated():
    from fastapi.testclient import TestClient
    from src.api_server import app
    client = TestClient(app)
    with mock.patch.dict(os.environ, {"API_KEY": KEY}, clear=False), \
         mock.patch("src.positions.journal.read_all",
                    return_value=[_spread_entry(), _equity_entry()]):
        denied = client.get("/api/discord/positions")
        allowed = client.get("/api/discord/positions",
                             headers={"X-API-Key": KEY})
    assert denied.status_code == 401           # fail-closed, like every route
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["ok"] is True
    ids = [p["trade_id"] for p in body["positions"]]
    assert ids == ["eqty0001", "sprd0001"] or ids == ["sprd0001", "eqty0001"]


# ------------------------------------------------------ Discord embed

def test_positions_embed_uses_one_codeblock_table():
    from src.discord_bot import _positions_embed
    items = active_positions([_equity_entry(), _spread_entry()], today=TODAY)
    embed = _positions_embed(items).to_dict()
    assert embed["title"] == "📂 Open Paper Positions (2)"
    assert not embed.get("fields")            # no more field-per-position
    desc = embed["description"]
    assert "```" in desc                      # the code-block table
    assert "NIFTY" in desc and "ONGC" in desc


# --------------------------------------------- format_discord_table (U3)

def test_discord_table_abbreviates_and_aligns():
    from src.positions import format_discord_table
    items = active_positions([_equity_entry(), _spread_entry()], today=TODAY)
    table = format_discord_table(items)
    assert table.startswith("```") and table.rstrip().endswith("open — paper only.")
    for col in ("UNDER", "STRAT", "ENTRY", "EXPIRY", "MAX P/L", "DAYS"):
        assert col in table
    assert "NIFTY" in table and "BPS" in table          # ticker + strat abbrev
    assert "+9.6k/-5.4k" in table                        # compact max profit/loss
    assert "07-21" in table                              # MM-DD expiry
    assert "2d" in table and "7d" in table
    # equity row: EQ strat, target/stop cell, no expiry
    assert "EQ" in table and "T260/S235" in table
    # every table row (between the fences) is equally wide — a real table
    body = [l for l in table.splitlines()
            if l and not l.startswith("```") and "open — paper" not in l]
    assert len({len(l) for l in body}) == 1


def test_discord_table_empty_state():
    from src.positions import format_discord_table
    assert format_discord_table([]) == "No open paper positions."


def test_discord_table_caps_rows_with_footer():
    from src.positions import format_discord_table
    items = active_positions(
        [_spread_entry(short_id=f"id{i:06d}") for i in range(30)],
        today=TODAY)
    table = format_discord_table(items)
    assert table.count("\n") <= 30          # not 30 data rows
    assert "25 of 30 shown" in table


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

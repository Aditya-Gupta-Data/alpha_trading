"""#187: past the 10% line a paper book keeps trading; the line is recorded once a day. Hermetic ':memory:'."""
import sqlite3
from datetime import datetime

import pytest

from src import equity_history
from src import portfolio_manager as pm

NOW = datetime(2026, 11, 23, 10, 59, 0)
SENT = []


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    pm.ensure_schema(c)
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    pm.get_paper_account(c, "PAPER_2L")
    pm.get_paper_account(c, pm.ACCOUNT_PAPER_SHADOW_LEARNER)
    equity_history.ensure_schema(c)
    monkeypatch.setattr(pm, "_now_ist_naive", lambda: NOW)
    monkeypatch.setattr(pm, "_now_iso", lambda: NOW.isoformat(timespec="seconds"))
    SENT.clear()
    monkeypatch.setattr(pm, "_ruin_halt_card", lambda conn: SENT.append(1))
    yield c
    c.close()


def _read(c, acct, minute, net):
    c.execute("INSERT INTO net_equity_history (account_id, ts, equity, unrealized_pnl, net_equity, "
              "starting_capital, marks_as_of, marked_of) VALUES (?, ?, 0, 0, ?, 0, NULL, '5/5')",
              (acct, NOW.replace(minute=minute).isoformat(timespec="seconds"), net))
    c.commit()


def _rows(c, table, event, acct=None):
    if acct:
        return c.execute(f"SELECT detail FROM {table} WHERE event_type = ? AND account_id = ?",
                         (event, acct)).fetchall()
    return c.execute(f"SELECT detail FROM {table} WHERE event_type = ?", (event,)).fetchall()


def test_record_only_is_the_production_default():
    assert pm.RUIN_HALT_BLOCKS_ENTRIES is False


def test_primary_past_the_line_trades_on_and_records_once_a_day(conn):
    _read(conn, "PAPER_10L", 40, 870_000)
    _read(conn, "PAPER_10L", 55, 860_000)
    for _ in range(3):
        assert pm.trading_halted(conn) is False
    assert pm.halt_latched(conn) is False
    assert pm.entry_halt(conn) is None
    rows = _rows(conn, "account_events", pm.RUIN_LINE_EVENT)
    assert len(rows) == 1 and rows[0][0].startswith("true-equity drawdown 13.00%")
    assert pm.halt_banner_lines(conn) == [] and SENT == []


def test_paper_account_past_the_line_trades_on_and_records_once_a_day(conn):
    _read(conn, "PAPER_2L", 40, 170_000)
    _read(conn, "PAPER_2L", 55, 172_000)
    assert pm.paper_trading_halted(conn, "PAPER_2L") is False
    assert pm.paper_trading_halted(conn, "PAPER_2L") is False
    assert pm.paper_halt_latched(conn, "PAPER_2L") is False
    assert pm.entry_halt(conn, "PAPER_2L") is None
    assert len(_rows(conn, "paper_account_events", pm.RUIN_LINE_EVENT, "PAPER_2L")) == 1
    assert _rows(conn, "account_events", pm.RUIN_LINE_EVENT) == []     # never the primary's trail


def test_a_recon_or_kill_switch_latch_still_blocks(conn):
    assert pm.latch_halt(conn, why="recon divergence: ghost row") is True
    assert pm.trading_halted(conn) is True
    assert pm.entry_halt(conn)["event"] == "risk_of_ruin_halt"
    assert pm.paper_latch_halt(conn, "PAPER_2L", why="/halt all by owner") is True
    assert pm.paper_trading_halted(conn, "PAPER_2L") is True


def test_an_old_drawdown_latch_is_cleared_for_good(conn):
    pm.paper_log_event(conn, "PAPER_2L", pm.HALT_LATCH_EVENT, None, "realized drawdown 12.02% >= 10%")
    _read(conn, "PAPER_2L", 40, 170_000)
    _read(conn, "PAPER_2L", 55, 170_000)
    out = pm.paper_clear_halt(conn, "PAPER_2L", why="#187 record-only", who="owner")
    assert out["cleared"] is True and out["halted"] is False
    assert pm.paper_halt_latched(conn, "PAPER_2L") is False


def test_a_failed_record_never_blocks(conn, monkeypatch):
    _read(conn, "PAPER_10L", 40, 870_000)
    _read(conn, "PAPER_10L", 55, 860_000)
    monkeypatch.setattr(pm, "log_event", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("ro")))
    assert pm.trading_halted(conn) is False

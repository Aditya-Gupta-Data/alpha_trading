"""Ruin halt on true equity (realized + open P&L) — 2026-11-06. Hermetic ':memory:'."""
import sqlite3
from datetime import datetime

import pytest

from src import equity_history
from src import portfolio_manager as pm

NOW = datetime(2026, 11, 6, 11, 50, 0)


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    pm.ensure_schema(c)
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    pm.get_paper_account(c, "PAPER_2L_LIVE")
    pm.get_paper_account(c, pm.ACCOUNT_PAPER_SHADOW_LEARNER)
    equity_history.ensure_schema(c)
    monkeypatch.setattr(pm, "_now_ist_naive", lambda: NOW)
    monkeypatch.setattr(pm, "_ruin_halt_card", lambda conn: None)
    yield c
    c.close()


def _read(c, acct, minute, net, marked="15/15"):
    c.execute("INSERT INTO net_equity_history (account_id, ts, equity, unrealized_pnl, net_equity, "
              "starting_capital, marks_as_of, marked_of) VALUES (?, ?, 0, 0, ?, 0, NULL, ?)",
              (acct, NOW.replace(minute=minute).isoformat(timespec="seconds"), net, marked))
    c.commit()


def test_open_loss_past_ten_percent_latches_the_primary(conn):
    _read(conn, "PAPER_10L", 30, 890_000)
    _read(conn, "PAPER_10L", 45, 880_000)
    assert pm.drawdown_pct(conn) == 0.0
    assert pm.true_equity_drawdown_pct(conn) == 11.0           # the shallower of the two reads
    assert pm.trading_halted(conn) is True
    assert pm.halt_latched(conn) is True
    detail = conn.execute("SELECT detail FROM account_events WHERE event_type = ?",
                          (pm.HALT_LATCH_EVENT,)).fetchone()[0]
    assert detail.startswith("true-equity drawdown 11.00%")
    halt = pm.entry_halt(conn)
    assert halt["event"] == "risk_of_ruin_halt" and "11.00%" in halt["reason"]


def test_one_bad_reading_does_not_latch(conn):
    _read(conn, "PAPER_10L", 30, 990_000)
    _read(conn, "PAPER_10L", 45, 850_000)
    assert pm.true_equity_drawdown_pct(conn) == 1.0
    assert pm.trading_halted(conn) is False


@pytest.mark.parametrize("rows", [
    [(45, 850_000, "15/15")],                                  # only one reading
    [(30, 850_000, "15/15"), (45, 850_000, "14/15")],          # partial marks
    [(0, 850_000, "15/15"), (2, 850_000, "15/15")],            # stale (> 45 min old)
])
def test_abstains_without_fresh_fully_priced_readings(conn, rows):
    for minute, net, marked in rows:
        _read(conn, "PAPER_10L", minute, net, marked)
    assert pm.true_equity_drawdown_pct(conn) is None
    assert pm.trading_halted(conn) is False


def test_no_table_falls_back_to_realized(monkeypatch):
    c = sqlite3.connect(":memory:")
    pm.ensure_schema(c)
    pm.get_account(c)
    assert pm.true_equity_drawdown_pct(c) is None
    assert pm.trading_halted(c) is False
    c.close()


def test_paper_account_latches_on_true_equity(conn):
    _read(conn, "PAPER_2L_LIVE", 30, 178_000, "5/5")
    _read(conn, "PAPER_2L_LIVE", 45, 179_000, "5/5")
    assert pm.paper_trading_halted(conn, "PAPER_2L_LIVE") is True
    assert pm.paper_halt_latched(conn, "PAPER_2L_LIVE") is True
    assert pm.entry_halt(conn, "PAPER_2L_LIVE")["event"] == "risk_of_ruin_halt"


def test_shadow_learner_is_exempt(conn):
    acct = pm.ACCOUNT_PAPER_SHADOW_LEARNER
    _read(conn, acct, 30, 10_000, "30/30")
    _read(conn, acct, 45, 10_000, "30/30")
    assert pm.true_equity_drawdown_pct(conn, acct) is None
    assert pm.paper_trading_halted(conn, acct) is False


def test_realized_rule_still_latches_alone(conn):
    conn.execute("UPDATE account_state SET realized_pnl = -110000")
    conn.commit()
    assert pm.true_equity_drawdown_pct(conn) is None
    assert pm.trading_halted(conn) is True
    detail = conn.execute("SELECT detail FROM account_events WHERE event_type = ?",
                          (pm.HALT_LATCH_EVENT,)).fetchone()[0]
    assert detail.startswith("realized drawdown 11.00%")


def test_clear_re_arms_while_true_equity_is_still_breached(conn):
    _read(conn, "PAPER_10L", 30, 880_000)
    _read(conn, "PAPER_10L", 45, 880_000)
    assert pm.trading_halted(conn) is True
    out = pm.clear_halt(conn, why="test")
    assert out["cleared"] is True and out["halted"] is True and out["drawdown_pct"] == 12.0

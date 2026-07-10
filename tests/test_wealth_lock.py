"""
Tests for src/wealth_lock.py — the Gold-ETF paper profit sweep (paper
scope of the Wealth-Locking Flywheel, roadmap §5). Entirely offline:
sqlite ':memory:' ledgers, mocked prices, Discord dispatch patched.

Run from the project folder:
    python tests/test_wealth_lock.py         (simple, no extra installs)
    python -m pytest tests/                  (if you have pytest)
"""

import sqlite3
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import portfolio_manager as pm
from src import wealth_lock as wl
from src.notifier import _build_embed


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------ record_sweep

def test_profitable_settlement_sweeps_exactly_half():
    conn = _conn()
    row = wl.record_sweep(conn, "abcd1234", 9577.50)
    assert row is not None
    assert row["sweep_rs"] == 4788.75
    assert row["instrument"] == "GOLDBEES"
    assert row["status"] == "logged"
    stored = conn.execute("SELECT * FROM wealth_lock_ledger").fetchall()
    assert len(stored) == 1
    assert stored[0]["journal_ref"] == "abcd1234"
    assert stored[0]["sweep_rs"] == 4788.75


def test_losses_and_breakeven_never_sweep():
    conn = _conn()
    assert wl.record_sweep(conn, "loss0001", -5422.50) is None
    assert wl.record_sweep(conn, "flat0001", 0.0) is None
    wl.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM wealth_lock_ledger").fetchone()[0] == 0


def test_a_win_is_never_swept_twice():
    """The tracker's sweeps may re-touch a resolved entry — the ledger
    must stay idempotent per journal_ref."""
    conn = _conn()
    assert wl.record_sweep(conn, "abcd1234", 1000.0) is not None
    assert wl.record_sweep(conn, "abcd1234", 1000.0) is None
    assert conn.execute("SELECT COUNT(*) FROM wealth_lock_ledger").fetchone()[0] == 1


def test_price_fn_computes_mock_units():
    conn = _conn()
    row = wl.record_sweep(conn, "abcd1234", 1000.0,
                          price_fn=lambda instrument: 65.50)
    assert row["mock_price"] == 65.50
    assert row["mock_units"] == round(500.0 / 65.50, 4)


def test_broken_price_fn_still_records_the_amount():
    """GOLDBEES has no verified SECURITY_ID_MAP entry yet — a failing or
    None price lookup stores the rupee amount with units unknown."""
    conn = _conn()

    def exploding(instrument):
        raise RuntimeError("no security id")

    row = wl.record_sweep(conn, "abcd1234", 1000.0, price_fn=exploding)
    assert row["sweep_rs"] == 500.0
    assert row["mock_price"] is None and row["mock_units"] is None

    row2 = wl.record_sweep(conn, "efgh5678", 1000.0,
                           price_fn=lambda instrument: None)
    assert row2["mock_units"] is None


# --------------------------------------------------------- alert payload

def test_sweep_alert_payload_carries_the_required_call_to_action():
    row = {"ts": "2026-07-10T15:35:00", "journal_ref": "abcd1234",
           "trade_pnl": 9577.50, "sweep_rs": 4788.75,
           "instrument": "GOLDBEES", "mock_price": None,
           "mock_units": None, "status": "logged"}
    payload = wl.build_sweep_alert(row)
    assert payload["event"] == "wealth_sweep"
    assert payload["ticker"] == "GOLDBEES"
    assert payload["date"] == "2026-07-10"
    assert payload["sweep_rs"] == 4788.75
    assert payload["short_id"] == "abcd1234"
    assert payload["description"] == \
        "🔒 PAPER SWEEP REQUIRED: Buy Rs.4,788.75 of GOLDBEES"


def test_sweep_alert_mentions_units_when_a_price_was_known():
    row = {"ts": "2026-07-10T15:35:00", "journal_ref": "abcd1234",
           "trade_pnl": 1000.0, "sweep_rs": 500.0, "instrument": "GOLDBEES",
           "mock_price": 65.50, "mock_units": 7.6336, "status": "logged"}
    payload = wl.build_sweep_alert(row)
    assert "≈7.63 units @ Rs.65.50" in payload["description"]


def test_sweep_embed_renders_gold_card_with_fields():
    row = {"ts": "2026-07-10T15:35:00", "journal_ref": "abcd1234",
           "trade_pnl": 9577.50, "sweep_rs": 4788.75,
           "instrument": "GOLDBEES", "mock_price": None,
           "mock_units": None, "status": "logged"}
    embed = _build_embed(wl.build_sweep_alert(row))
    assert embed["title"] == "🔒 Paper Wealth Sweep — GOLDBEES"
    assert embed["color"] == 0xF1C40F
    names = [f["name"] for f in embed["fields"]]
    assert "Sweep Amount" in names and "From Winning P&L" in names
    amount = next(f["value"] for f in embed["fields"]
                  if f["name"] == "Sweep Amount")
    assert amount == "Rs.4,788.75"


# ------------------------------------------------- sweep_on_settlement seam

def test_hook_sweeps_and_dispatches_on_profit():
    conn = _conn()
    with mock.patch("src.notifier.fire_broadcast") as fire:
        result = wl.sweep_on_settlement("abcd1234", 9577.50, conn=conn)
    assert result["swept"] is True
    assert result["sweep_rs"] == 4788.75
    assert fire.called
    sent = fire.call_args[0][0]
    assert sent["event"] == "wealth_sweep"
    assert "PAPER SWEEP REQUIRED" in sent["description"]


def test_hook_declines_losses_without_dispatching():
    conn = _conn()
    with mock.patch("src.notifier.fire_broadcast") as fire:
        result = wl.sweep_on_settlement("loss0001", -100.0, conn=conn)
    assert result["swept"] is False
    assert not fire.called


def test_hook_never_raises_when_the_ledger_is_broken():
    with mock.patch.object(wl, "record_sweep",
                           side_effect=RuntimeError("db locked")):
        result = wl.sweep_on_settlement("abcd1234", 1000.0, conn=_conn())
    assert result["swept"] is False
    assert "db locked" in result["reason"]


# --------------------------------------- integration: the settlement path

def test_release_entry_triggers_the_sweep_on_a_winning_trade():
    """End-to-end through portfolio_manager: lock margin, settle at a
    profit, and the wealth sweep both records and reports — without ever
    touching the account's cash (advisory ledger only)."""
    conn = _conn()
    pm.request_entry(conn, "win00001", 20000.0)
    equity_before = pm.equity(conn)
    with mock.patch("src.notifier.fire_broadcast") as fire:
        result = pm.release_entry("win00001", 9577.50, conn=conn)
    assert result["released"] is True
    assert result["wealth_sweep"]["swept"] is True
    assert result["wealth_sweep"]["sweep_rs"] == 4788.75
    assert fire.called
    # the sweep is advisory: account equity reflects the FULL win
    assert pm.equity(conn) == equity_before + 9577.50
    ledger = conn.execute("SELECT * FROM wealth_lock_ledger").fetchall()
    assert len(ledger) == 1 and ledger[0]["journal_ref"] == "win00001"


def test_release_entry_stays_truthful_when_the_sweep_itself_raises():
    """The margin release is COMMITTED before the sweep runs; a sweep that
    blows up (not just declines) must not flip the answer to
    released=False — the caller would keep accounting a lock the DB has
    already let go."""
    conn = _conn()
    pm.request_entry(conn, "win00002", 20000.0)
    with mock.patch.object(wl, "sweep_on_settlement",
                           side_effect=RuntimeError("ledger exploded")):
        result = pm.release_entry("win00002", 9577.50, conn=conn)
    assert result["released"] is True
    assert result["wealth_sweep"] is None
    assert "ledger exploded" in result["wealth_sweep_error"]


def test_release_entry_losing_trade_settles_with_no_sweep():
    conn = _conn()
    pm.request_entry(conn, "loss0001", 20000.0)
    with mock.patch("src.notifier.fire_broadcast") as fire:
        result = pm.release_entry("loss0001", -5422.50, conn=conn)
    assert result["released"] is True
    assert "wealth_sweep" not in result
    assert not fire.called
    wl.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM wealth_lock_ledger").fetchone()[0] == 0


def test_ledger_summary_totals():
    conn = _conn()
    wl.record_sweep(conn, "win00001", 1000.0)
    wl.record_sweep(conn, "win00002", 2000.0)
    summary = wl.ledger_summary(conn)
    assert summary["sweeps"] == 2
    assert summary["total_swept_rs"] == 1500.0
    assert summary["total_winning_pnl_rs"] == 3000.0
    assert summary["instrument"] == "GOLDBEES"


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

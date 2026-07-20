"""
Tests for src/wealth_lock.py — the Gold-ETF paper profit sweep (paper
scope of the Wealth-Locking Flywheel, roadmap §5). Entirely offline:
sqlite ':memory:' ledgers, mocked prices, Discord dispatch patched.

Run from the project folder:
    python tests/test_wealth_lock.py         (simple, no extra installs)
    python -m pytest tests/                  (if you have pytest)
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import portfolio_manager as pm
from src import wealth_lock as wl
from src.notifier import _build_embed

_TMP = tempfile.TemporaryDirectory()


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _scrip_report(verdict="ok", age_days=1, status="verified",
                  ticker="GOLDBEES.NS", name="report.json") -> Path:
    """A scrip-clerk report fixture. Hermetic on purpose: the sizing gate
    must never be decided by whatever happens to sit in the repo's data/
    folder (which is gitignored — on a fresh clone it isn't there at
    all)."""
    as_of = (datetime.now() - timedelta(days=age_days)).isoformat(
        timespec="seconds")
    path = Path(_TMP.name) / name
    path.write_text(json.dumps({
        "as_of": as_of, "status": status,
        "rows": [{"ticker": ticker, "id": "14428", "verdict": verdict,
                  "detail": "id now trades as SOMETHINGELSE"}]}))
    return path


VERIFIED = None          # lazily built per test run


def _verified() -> Path:
    global VERIFIED
    if VERIFIED is None:
        VERIFIED = _scrip_report(name="verified.json")
    return VERIFIED


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
                          price_fn=lambda instrument: 65.50,
                          scrip_report_path=_verified())
    assert row["mock_price"] == 65.50
    assert row["mock_units"] == round(500.0 / 65.50, 4)


def test_broken_price_fn_still_records_the_amount():
    """A failing or None price lookup stores the rupee amount with units
    unknown — the sweep is never lost to a quote outage."""
    conn = _conn()

    def exploding(instrument):
        raise RuntimeError("quote endpoint down")

    row = wl.record_sweep(conn, "abcd1234", 1000.0, price_fn=exploding,
                          scrip_report_path=_verified())
    assert row["sweep_rs"] == 500.0
    assert row["mock_price"] is None and row["mock_units"] is None

    row2 = wl.record_sweep(conn, "efgh5678", 1000.0,
                           price_fn=lambda instrument: None,
                           scrip_report_path=_verified())
    assert row2["mock_units"] is None


# ------------------------------------- the flywheel: sizing + the id gate

def test_sweep_sizes_whole_units_and_reports_residual():
    """Graduated from next_gen_engine/wealth_flywheel: whole units only,
    the un-investable remainder reported honestly, never rounded in."""
    conn = _conn()
    row = wl.record_sweep(conn, "win00001", 10_000.0,
                          price_fn=lambda i: 62.0,
                          scrip_report_path=_verified())
    assert row["sweep_rs"] == 5000.0
    assert row["order_qty"] == 80                  # floor(5000/62)
    assert row["cash_residual_rs"] == 40.0         # 5000 - 4960
    assert row["sizing_blocked_reason"] is None


def test_earmark_below_one_unit_accumulates_instead_of_rounding():
    conn = _conn()
    row = wl.record_sweep(conn, "tiny0001", 100.0, price_fn=lambda i: 62.0,
                          scrip_report_path=_verified())
    assert row["order_qty"] is None                # earmark 50 < one unit
    assert row["cash_residual_rs"] == 50.0         # carries forward whole


def test_sizing_is_blocked_when_the_id_fails_its_master_check():
    """The owner's Null-Honesty condition: a rotted id must stop the
    flywheel — but the sweep is still RECORDED, with the reason."""
    conn = _conn()
    bad = _scrip_report(verdict="symbol_mismatch", name="mismatch.json")
    row = wl.record_sweep(conn, "rot00001", 10_000.0,
                          price_fn=lambda i: 62.0, scrip_report_path=bad)
    assert row["sweep_rs"] == 5000.0               # never lost
    assert row["order_qty"] is None and row["mock_price"] is None
    assert "symbol_mismatch" in row["sizing_blocked_reason"]


def test_unverifiable_report_states_block_sizing_too():
    """Missing, stale, or 'unavailable' can never read as a pass."""
    conn = _conn()
    cases = {
        "missing": Path(_TMP.name) / "nope.json",
        "stale": _scrip_report(age_days=30, name="stale.json"),
        "unavailable": _scrip_report(status="unavailable",
                                     name="outage.json"),
        "absent_row": _scrip_report(ticker="TCS.NS", name="absent.json"),
    }
    for i, (label, path) in enumerate(cases.items()):
        row = wl.record_sweep(conn, f"case{i:04d}", 10_000.0,
                              price_fn=lambda i: 62.0,
                              scrip_report_path=path)
        assert row["order_qty"] is None, label
        assert row["sizing_blocked_reason"], label
        assert row["sweep_rs"] == 5000.0, label    # the earmark survives


def test_verified_report_reads_as_verified():
    g = wl.goldbees_verified(report_path=_verified())
    assert g["verified"] is True and "14428" in g["reason"]


def test_default_price_fn_never_touches_the_network_under_pytest():
    """A unit test must never reach Dhan — the muzzle is the same
    PYTEST_CURRENT_TEST signal the notifier uses."""
    assert wl.default_price_fn() is None


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


def test_sweep_alert_shows_the_sized_order_and_says_when_sizing_was_blocked():
    sized = wl.build_sweep_alert(
        {"ts": "2026-07-20T15:35:00", "journal_ref": "win00001",
         "trade_pnl": 10_000.0, "sweep_rs": 5000.0, "instrument": "GOLDBEES",
         "mock_price": 62.0, "mock_units": 80.6452, "order_qty": 80,
         "cash_residual_rs": 40.0, "status": "logged"})
    assert "PAPER ORDER: 80 unit(s) @ Rs.62.00" in sized["description"]
    assert "Rs.40.00 residual" in sized["description"]
    assert sized["order_qty"] == 80

    blocked = wl.build_sweep_alert(
        {"ts": "2026-07-20T15:35:00", "journal_ref": "rot00001",
         "trade_pnl": 10_000.0, "sweep_rs": 5000.0, "instrument": "GOLDBEES",
         "mock_price": None, "mock_units": None, "order_qty": None,
         "cash_residual_rs": 5000.0, "status": "logged",
         "sizing_blocked_reason": "scrip check FAILED: symbol_mismatch"})
    # An un-sized sweep is stated, never left for the owner to infer.
    assert "⚠ sizing skipped" in blocked["description"]
    assert "symbol_mismatch" in blocked["description"]


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

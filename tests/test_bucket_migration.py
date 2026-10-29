"""Hermetic tests for M1/M4 Multi-Bucket Ledger Migration Script (`scripts/migrate_m1_buckets.py`).

Proves:
1. All existing options capital and open positions are mapped to Bucket B1 (Options Spreads).
2. Buckets B2 (Equity Swing), B3 (Darlings), B4 (Wealth Lock), and B5 (Cross-Asset) are initialized with 0 active positions.
3. Strict PARITY holds: sum(5 buckets' ledger-summed equity) == legacy flat account's booked equity.
4. Dry-run mode is strictly read-only by default, and `--apply` is transactional and idempotent.
"""
import sqlite3
from pathlib import Path

import pytest

from scripts import migrate_m1_buckets as m1
from src import brain_map, portfolio_manager as pm


def _build_legacy_flat_ledger(starting_capital: float = 1_000_000.0, realized_pnl: float = 85_000.0):
    """Build a hermetic in-memory legacy flat SQLite ledger."""
    conn = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(conn)
    pm.get_account(conn)  # Ensure row 1 exists

    # Legacy flat account setup
    conn.execute("UPDATE account_state SET starting_capital = ?, realized_pnl = ?, peak_equity = ? WHERE id = 1",
                 (starting_capital, realized_pnl, starting_capital + max(0.0, realized_pnl)))

    # Existing options positions in flat ledger: 2 open, 1 settled
    conn.execute(
        "INSERT INTO margin_locks (journal_ref, margin_rs, locked_at, released_at, pnl_net) VALUES "
        "('opt_spread_1', 60000.0, '2026-10-20T10:00:00', NULL, NULL), "
        "('opt_spread_2', 45000.0, '2026-10-21T11:00:00', NULL, NULL), "
        "('opt_spread_3', 50000.0, '2026-10-15T09:30:00', '2026-10-18T15:20:00', ?)", (realized_pnl,)
    )

    # Associated outcomes in flat ledger
    conn.execute("INSERT INTO outcomes (journal_ref, date, ticker, result) VALUES "
                 "('opt_spread_1', '2026-10-20', 'NIFTY', 'loss'), "
                 "('opt_spread_3', '2026-10-18', 'BANKNIFTY', 'win')")
    pm.ensure_buckets_schema(conn)
    conn.commit()
    return conn


def test_5_bucket_migration_maps_all_options_to_b1_and_initializes_b2_to_b5():
    starting_cap = 1_000_000.0
    realized = 85_000.0
    total_equity = starting_cap + realized  # 10,85,000.0

    conn = _build_legacy_flat_ledger(starting_cap, realized)
    p = m1.plan(conn)

    assert p["ok"] is True

    # 1. Verify all 5 buckets exist in plan
    by_id = {b["portfolio_id"]: b for b in p["buckets"]}
    expected_buckets = {
        "PAPER_10L/IDX_SPREADS",  # B1: Options Spreads
        "PAPER_10L/EQ_SWING",     # B2: Equity Swing
        "PAPER_10L/DARLINGS",     # B3: Darlings
        "PAPER_10L/WEALTH_LOCK",  # B4: Wealth Lock
        "PAPER_10L/CROSS_ASSET",  # B5: Cross-Asset
    }
    assert expected_buckets.issubset(set(by_id.keys()))

    # 2. Verify Bucket B1 received all options capital
    b1 = by_id["PAPER_10L/IDX_SPREADS"]
    assert b1["strategy_family"] == "index_options"
    assert b1["starting_capital"] == starting_cap
    assert b1["realized_pnl"] == realized
    assert round(b1["starting_capital"] + b1["realized_pnl"], 2) == total_equity

    # 3. Verify Buckets B2, B3, B4, B5 are initialized with 0 active positions
    for pid in ("PAPER_10L/EQ_SWING", "PAPER_10L/DARLINGS", "PAPER_10L/WEALTH_LOCK", "PAPER_10L/CROSS_ASSET"):
        b = by_id[pid]
        assert b["starting_capital"] == 0.0
        assert b["realized_pnl"] == 0.0
        assert p["active_positions"][pid] == 0

    # Bucket B1 has exactly the 2 open options positions
    assert p["active_positions"]["PAPER_10L/IDX_SPREADS"] == 2

    # 4. Strict PARITY proof: Sum of all 5 buckets' capital + deployed margin == legacy flat account equity
    deployed_margin = 60000.0 + 45000.0  # 105,000.0
    sum_5_buckets_equity = sum(
        by_id[pid]["starting_capital"] + by_id[pid]["realized_pnl"]
        for pid in expected_buckets
    )
    available_cash = sum_5_buckets_equity - deployed_margin

    assert round(sum_5_buckets_equity, 2) == total_equity
    assert round(available_cash + deployed_margin, 2) == total_equity

    # Verify parity checks reported in plan
    parity_by_name = {c["check"]: c for c in p["parity"]}
    assert parity_by_name["PAPER_10L equity"]["ok"] is True
    assert parity_by_name["PAPER_10L equity"]["buckets"] == total_equity
    assert parity_by_name["PAPER_10L equity"]["account"] == total_equity

    conn.close()


def test_5_bucket_migration_dry_run_safety_and_apply():
    conn = _build_legacy_flat_ledger(1_000_000.0, 50_000.0)
    p = m1.plan(conn)

    # Dry-run check: Plan does NOT mutate tables
    assert conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM portfolio_margin_locks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM margin_locks WHERE portfolio_id IS NOT NULL").fetchone()[0] == 0

    # Mutate with apply
    applied = m1.apply(conn, p)
    assert applied["buckets"] == 5  # Exactly 5 buckets for PAPER_10L
    assert applied["locks"] == 3    # 3 margin locks copied

    # Verify database state after apply
    portfolios = conn.execute("SELECT portfolio_id, strategy_family, starting_capital, realized_pnl FROM portfolios").fetchall()
    assert len(portfolios) == 5

    # Verify B1 holds all open locks, B2-B5 have 0 active locks
    b1_locks = conn.execute(
        "SELECT COUNT(*) FROM portfolio_margin_locks WHERE portfolio_id = 'PAPER_10L/IDX_SPREADS' AND released_at IS NULL"
    ).fetchone()[0]
    assert b1_locks == 2

    for other_pid in ("PAPER_10L/EQ_SWING", "PAPER_10L/DARLINGS", "PAPER_10L/WEALTH_LOCK", "PAPER_10L/CROSS_ASSET"):
        other_active_locks = conn.execute(
            "SELECT COUNT(*) FROM portfolio_margin_locks WHERE portfolio_id = ? AND released_at IS NULL",
            (other_pid,)
        ).fetchone()[0]
        assert other_active_locks == 0

    # Verify account_state_v view reflects 5 buckets and identical equity
    view_row = conn.execute("SELECT buckets, bucket_equity FROM account_state_v").fetchone()
    assert view_row["buckets"] == 5
    assert view_row["bucket_equity"] == 1_050_000.0

    # Idempotency check: Re-applying adds 0 rows
    reapplied = m1.apply(conn, m1.plan(conn))
    assert reapplied["buckets"] == 0
    assert reapplied["locks"] == 0
    conn.close()


def test_5_bucket_migration_parity_refusal_on_corruption():
    conn = _build_legacy_flat_ledger(1_000_000.0, 50_000.0)
    p = m1.plan(conn)
    corrupted_plan = dict(p, ok=False)

    with pytest.raises(RuntimeError, match="PARITY failed"):
        m1.apply(conn, corrupted_plan)

    # Proves transaction rolled back / nothing written
    assert conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 0
    conn.close()


def test_5_bucket_migration_cli_dry_run_and_apply(tmp_path: Path, capsys):
    db_path = tmp_path / "brain_map.db"
    conn = brain_map.connect(db_path)
    pm.ensure_accounts_schema(conn)
    pm.get_account(conn)
    conn.execute("UPDATE account_state SET starting_capital = 1000000.0, realized_pnl = 40000.0 WHERE id = 1")
    conn.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at, released_at, pnl_net) "
                 "VALUES ('opt_cli', 30000.0, 'now', 'later', 40000.0)")
    conn.commit()
    conn.close()

    # CLI dry-run
    ret_dry = m1.main(["--db", str(db_path)])
    assert ret_dry == 0
    out_dry = capsys.readouterr().out
    assert "dry-run: nothing written" in out_dry
    assert "PARITY ok" in out_dry

    conn2 = sqlite3.connect(db_path)
    assert conn2.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 0

    # CLI --apply
    ret_apply = m1.main(["--db", str(db_path), "--apply"])
    assert ret_apply == 0
    out_apply = capsys.readouterr().out
    assert "applied:" in out_apply

    assert conn2.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 5
    conn2.close()

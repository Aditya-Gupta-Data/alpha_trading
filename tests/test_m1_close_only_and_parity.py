"""Decision #170: overweight legacy buckets go close-only (exits settle, entries wait), and the M1 migration's
PARITY is the ledger-summed P&L per strategy against the booked account equity, exact to the paisa."""
import importlib.util
import sqlite3

import pytest

from src import brain_map, buckets, config, portfolio_manager as pm

spec = importlib.util.spec_from_file_location("m1", "scripts/migrate_m1_buckets.py")
m1 = importlib.util.module_from_spec(spec); spec.loader.exec_module(m1)

IDX, DARL, L2 = "PAPER_10L/IDX_SPREADS", "PAPER_10L/DARLINGS", "PAPER_2L/IDX_SPREADS"


def _seed(c, pid, account, family, cap):
    c.execute("INSERT INTO portfolios (portfolio_id, account_id, strategy_family, starting_capital, realized_pnl, "
              "peak_equity, risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, created_at) "
              "VALUES (?, ?, ?, ?, 0, ?, 2.0, 50.0, 3.0, 'now')", (pid, account, family, cap, cap))


def _lock(c, pid, ref, margin):
    c.execute("INSERT INTO portfolio_margin_locks (portfolio_id, journal_ref, margin_rs, lots, locked_at) "
              "VALUES (?, ?, ?, 1, 'now')", (pid, ref, margin))


@pytest.fixture
def overweight(monkeypatch):
    """PAPER_10L Rs.10L across two buckets; IDX has Rs.8.6L (86%) deployed against a 25% (Rs.2.5L) ceiling."""
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", True)
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    _seed(c, IDX, "PAPER_10L", "index_options", 950_000.0)
    _seed(c, DARL, "PAPER_10L", "darlings", 50_000.0)
    _seed(c, L2, "PAPER_2L", "index_options", 200_000.0)
    for i in range(4):
        _lock(c, IDX, f"leg{i}", 215_000.0)
    c.commit()
    yield c
    c.close()


def test_overweight_bucket_is_close_only(overweight):
    st = buckets.ceiling_state(overweight, IDX)
    assert st["ceiling_rs"] == 250_000.0 and st["deployed_rs"] == 860_000.0
    assert st["deployed_pct"] == 86.0 and st["close_only"] is True
    assert buckets.can_allocate_new_trade(overweight, IDX) is False
    assert buckets.can_allocate_new_trade(overweight, IDX, 1.0) is False


def test_close_only_refuses_entries_but_never_forces_an_exit(overweight):
    v = buckets.gate(overweight, "PAPER_10L", "newopt", 10_000.0)
    assert v["approved"] is False and v["close_only"] is True and v["reason"].startswith("close-only:")
    assert overweight.execute("SELECT COUNT(*) FROM portfolio_margin_locks WHERE released_at IS NULL").fetchone()[0] == 4
    assert overweight.execute("SELECT COUNT(*) FROM portfolio_events WHERE event_type = 'ceiling_refused'"
                              ).fetchone()[0] == 1
    held = buckets.gate(overweight, "PAPER_10L", "leg0", 215_000.0)            # an existing position stays held
    assert held["approved"] is True and held["reason"] == buckets.HELD_LOCK_REASON


def test_exits_settle_while_close_only_and_entries_reopen_once_drained(overweight):
    r = buckets.release(overweight, "PAPER_10L", "leg0", 12_345.67)
    assert r["released"] and r["pnl_net"] == 12_345.67
    assert buckets.bucket(overweight, IDX)["realized_pnl"] == 12_345.67
    assert buckets.ceiling_state(overweight, IDX)["close_only"] is True           # 6.45L still > 2.5L
    for ref in ("leg1", "leg2"):
        assert buckets.release(overweight, "PAPER_10L", ref, 0.0)["released"]
    st = buckets.ceiling_state(overweight, IDX)
    assert st["deployed_rs"] == 215_000.0 and st["close_only"] is False
    assert st["ceiling_rs"] == round((1_000_000.0 + 12_345.67) * 0.25, 2)
    v = buckets.gate(overweight, "PAPER_10L", "newopt", 10_000.0)
    assert v["approved"] is True and v["reason"] == "bucket margin locked"


def test_ceiling_boundary_is_inclusive(overweight):
    for ref in ("leg0", "leg1", "leg2"):
        buckets.release(overweight, "PAPER_10L", ref, 0.0)
    assert buckets.can_allocate_new_trade(overweight, IDX, 35_000.0) is True     # 2.15L + 0.35L == 2.5L
    assert buckets.can_allocate_new_trade(overweight, IDX, 35_000.01) is False
    v = buckets.gate(overweight, "PAPER_10L", "big", 40_000.0)
    assert v["approved"] is False and v["close_only"] is False and v["reason"].startswith("ceiling:")


def test_shadow_and_unknown_buckets_have_no_ceiling(overweight):
    _lock(overweight, L2, "s1", 190_000.0)                                       # 95% of its own account
    assert buckets.ceiling_state(overweight, L2) is None
    assert buckets.can_allocate_new_trade(overweight, L2, 5_000.0) is True
    assert buckets.can_allocate_new_trade(overweight, "PAPER_10L/NOPE", 1e9) is True


# ---- migration PARITY -------------------------------------------------------------------------------------

def _ledger(path=":memory:", booked=50_000.0, budget=300_000.0):
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    pm.get_account(c)
    pm.get_paper_account(c, pm.ACCOUNT_PAPER_2L)
    c.execute("CREATE TABLE IF NOT EXISTS treasury_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
              "equity_budget_rs REAL NOT NULL, updated_at TEXT NOT NULL)")
    c.execute("INSERT INTO treasury_state VALUES (1, ?, 'now')", (budget,))
    c.execute("UPDATE account_state SET realized_pnl = ? WHERE id = 1", (booked,))
    c.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at, released_at, pnl_net) VALUES "
              "('eqd:a', 90000, 'now', 'later', -2000.25), ('eqd:b', 10000, 'now', NULL, NULL), "
              "('opt1', 20000, 'now', 'later', 30000.10), ('opt2', 15000, 'now', 'later', 22000.15), "
              "('opt3', 25000, 'now', NULL, NULL)")
    c.execute("UPDATE paper_accounts SET realized_pnl = 1500 WHERE account_id = 'PAPER_2L'")
    c.execute("INSERT INTO paper_margin_locks (account_id, journal_ref, margin_rs, lots, primary_lots, locked_at, "
              "released_at, pnl_net) VALUES ('PAPER_2L', 'opt1', 17000, 1, 1, 'now', 'later', 1500)")
    c.commit()
    return c


def _check(p, name):
    return {x["check"]: x for x in p["parity"]}[name]


def test_bucket_pnl_is_the_ledger_sum_by_strategy():
    p = m1.plan(_ledger())
    by = {b["portfolio_id"]: b for b in p["buckets"]}
    assert by[DARL]["starting_capital"] == 300_000.0 and by[DARL]["realized_pnl"] == -2000.25
    assert by[IDX]["starting_capital"] == 700_000.0 and by[IDX]["realized_pnl"] == 52_000.25
    assert by[L2]["realized_pnl"] == 1500.0
    assert p["ok"] is True and all(x["ok"] for x in p["parity"])
    assert _check(p, "PAPER_10L equity")["buckets"] == 1_050_000.0


def test_parity_fails_on_a_one_paisa_gap_and_apply_refuses():
    c = _ledger(booked=50_000.01)
    p = m1.plan(c)
    eq = _check(p, "PAPER_10L equity")
    assert eq["ok"] is False and eq["buckets"] == 1_050_000.0 and eq["account"] == 1_050_000.01
    assert p["ok"] is False
    with pytest.raises(RuntimeError, match="PARITY failed"):
        m1.apply(c, p)
    assert c.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 0


def test_parity_fails_when_a_shadow_account_disagrees_with_its_ledger():
    c = _ledger()
    c.execute("UPDATE paper_accounts SET realized_pnl = 1600 WHERE account_id = 'PAPER_2L'")
    p = m1.plan(c)
    assert _check(p, "PAPER_2L equity")["ok"] is False and p["ok"] is False


def test_parity_fails_on_a_released_lock_without_pnl():
    c = _ledger()
    c.execute("UPDATE margin_locks SET released_at = 'later' WHERE journal_ref = 'opt3'")
    p = m1.plan(c)
    assert _check(p, "released locks without P&L")["buckets"] == 1 and p["ok"] is False


def test_parity_fails_when_the_darlings_budget_exceeds_capital():
    p = m1.plan(_ledger(budget=1_200_000.0))
    assert _check(p, "PAPER_10L capital split (IDX start >= 0)")["ok"] is False and p["ok"] is False


def test_cli_dry_run_is_read_only_and_aborts_on_mismatch(tmp_path, capsys):
    db = tmp_path / "bm.db"
    _ledger(str(db), booked=49_999.0).close()
    before = sqlite3.connect(db).execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert m1.main(["--db", str(db)]) == 2
    out = capsys.readouterr().out
    assert "PARITY FAIL PAPER_10L equity" in out and "ABORT" in out
    after = sqlite3.connect(db).execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert after == before
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 0


def test_cli_apply_writes_only_when_parity_holds(tmp_path, capsys):
    db = tmp_path / "bm.db"
    _ledger(str(db)).close()
    assert m1.main(["--db", str(db)]) == 0
    assert "dry-run: nothing written" in capsys.readouterr().out
    assert m1.main(["--db", str(db), "--apply"]) == 0
    c = sqlite3.connect(db)
    assert c.execute("SELECT realized_pnl FROM portfolios WHERE portfolio_id = ?", (IDX,)).fetchone()[0] == 52_000.25
    assert c.execute("SELECT COUNT(*) FROM account_events WHERE event_type = 'm1_bucket_migration'").fetchone()[0] == 1


def _with_reset(c, pnl_before=26_982.14):
    """A #84 reset: older settled locks keep their P&L rows, the booked P&L restarted at zero."""
    c.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at, released_at, pnl_net) VALUES "
              "('old1', 50000, '2026-07-01T10:00:00', '2026-07-10T15:00:00', ?), "
              "('eqd:old', 40000, '2026-07-02T10:00:00', '2026-07-15T15:00:00', -1000.0)", (pnl_before,))
    c.execute("INSERT INTO account_events (ts, event_type, detail) VALUES "
              "('2026-07-21T14:32:30', 'clean_sheet', 'decision #84: pool reset')")
    c.execute("UPDATE margin_locks SET released_at = '2026-08-01T15:00:00' WHERE released_at = 'later'")
    c.commit()
    return c


def test_reset_excludes_pre_clean_sheet_pnl_and_parity_holds():
    p = m1.plan(_with_reset(_ledger()))
    by = {b["portfolio_id"]: b for b in p["buckets"]}
    assert by[IDX]["realized_pnl"] == 52_000.25 and by[DARL]["realized_pnl"] == -2000.25
    assert p["pre_reset"] == {"reset_at": "2026-07-21T14:32:30", "locks": 2, "pnl_excluded": 25_982.14}
    assert _check(p, "PAPER_10L equity")["ok"] is True and p["ok"] is True


def test_without_the_reset_event_the_old_pnl_breaks_parity():
    c = _with_reset(_ledger())
    c.execute("DELETE FROM account_events WHERE event_type = 'clean_sheet'")
    p = m1.plan(c)
    eq = _check(p, "PAPER_10L equity")
    assert eq["ok"] is False and round(eq["buckets"] - eq["account"], 2) == 25_982.14
    assert p["pre_reset"]["locks"] == 0


def test_latest_reset_wins_and_a_post_reset_paisa_gap_still_fails():
    c = _with_reset(_ledger())
    c.execute("INSERT INTO account_events (ts, event_type, detail) VALUES ('2026-07-30T09:00:00', 'clean_sheet', 'x')")
    p = m1.plan(c)
    assert p["pre_reset"]["reset_at"] == "2026-07-30T09:00:00" and p["ok"] is True
    c.execute("UPDATE account_state SET realized_pnl = realized_pnl + 0.01 WHERE id = 1")
    assert m1.plan(c)["ok"] is False

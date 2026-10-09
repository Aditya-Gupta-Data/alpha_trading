"""M1 multi-bucket ledger: additive schema + views, the migration plan/PARITY/apply on a temp DB, dry-run safety."""
import importlib.util, sqlite3
from src import portfolio_manager as pm

spec = importlib.util.spec_from_file_location("m1", "scripts/migrate_m1_buckets.py")
m1 = importlib.util.module_from_spec(spec); spec.loader.exec_module(m1)


def _db():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    pm.get_account(c)
    for a in (pm.ACCOUNT_PAPER_2L, pm.ACCOUNT_PAPER_SHADOW_LEARNER):
        pm.get_paper_account(c, a)
    c.execute("CREATE TABLE IF NOT EXISTS treasury_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
              "equity_budget_rs REAL NOT NULL, updated_at TEXT NOT NULL)")
    c.execute("INSERT INTO treasury_state VALUES (1, 300000.0, 'now')")
    c.execute("UPDATE account_state SET realized_pnl = 50000 WHERE id = 1")
    c.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at, released_at, pnl_net) VALUES "
              "('eqd:aaaa', 90000, 'now', 'later', -2000), ('opt00001', 20000, 'now', NULL, NULL), "
              "('opt00002', 15000, 'now', 'later', 52000)")
    c.execute("INSERT INTO paper_margin_locks (account_id, journal_ref, margin_rs, lots, primary_lots, locked_at) "
              "VALUES ('PAPER_2L', 'opt00001', 17000, 1, 1, 'now')")
    c.commit()
    return c


def test_schema_is_additive_and_views_exist():
    c = _db()
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"portfolios", "capital_allocations", "portfolio_margin_locks", "portfolio_equity_curve",
            "portfolio_events", "account_state_v", "paper_accounts_v"} <= tables
    for t in ("margin_locks", "paper_margin_locks"):
        assert "portfolio_id" in {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
    c.execute("CREATE TABLE outcomes (journal_ref TEXT, date TEXT, ticker TEXT, result TEXT)")
    pm.ensure_buckets_schema(c)                                    # a ledger created later gets the column too
    assert "portfolio_id" in {r[1] for r in c.execute("PRAGMA table_info(outcomes)")}
    pm.ensure_accounts_schema(c)                                   # idempotent
    from src.config import MULTI_BUCKET_LEDGER
    assert MULTI_BUCKET_LEDGER is False                            # flat ledger until the VM dry-run


def test_plan_seeds_by_the_rulings_and_parity_holds():
    c = _db()
    p = m1.plan(c)
    by = {b["portfolio_id"]: b for b in p["buckets"]}
    assert set(by) == {"PAPER_10L/IDX_SPREADS", "PAPER_10L/DARLINGS", "PAPER_2L/IDX_SPREADS",
                       "PAPER_SHADOW_LEARNER/IDX_SPREADS"}
    d, i = by["PAPER_10L/DARLINGS"], by["PAPER_10L/IDX_SPREADS"]
    assert d["starting_capital"] == 300000.0 and d["realized_pnl"] == -2000.0      # treasury budget + eqd: P&L
    assert i["realized_pnl"] == 52000.0                                              # the rest of the realized
    assert round(d["starting_capital"] + d["realized_pnl"] + i["starting_capital"] + i["realized_pnl"], 2) == 1050000.0
    assert by["PAPER_SHADOW_LEARNER/IDX_SPREADS"]["max_drawdown_pct"] is None        # no latch on the learner
    assert p["ok"] and all(x["ok"] for x in p["parity"])


def test_apply_is_transactional_idempotent_and_refuses_on_parity(tmp_path):
    c = _db()
    p = m1.plan(c)
    n = m1.apply(c, p)
    assert n["buckets"] == 4 and n["locks"] == 4
    assert c.execute("SELECT portfolio_id FROM margin_locks WHERE journal_ref = 'eqd:aaaa'").fetchone()[0] == "PAPER_10L/DARLINGS"
    assert c.execute("SELECT portfolio_id FROM paper_margin_locks").fetchone()[0] == "PAPER_2L/IDX_SPREADS"
    assert c.execute("SELECT bucket_equity FROM account_state_v").fetchone()[0] == 1050000.0
    assert c.execute("SELECT COUNT(*) FROM account_events WHERE event_type = 'm1_bucket_migration'").fetchone()[0] == 1
    again = m1.apply(c, m1.plan(c))
    assert again["buckets"] == 0 and again["locks"] == 0                              # idempotent
    bad = dict(p, ok=False)
    try:
        m1.apply(c, bad); assert False
    except RuntimeError as e:
        assert "PARITY" in str(e)


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    db = tmp_path / "bm.db"
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    pm.get_account(c); pm.get_paper_account(c, pm.ACCOUNT_PAPER_2L); c.commit(); c.close()
    assert m1.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "dry-run: nothing written" in out and "PARITY ok" in out
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0] == 0

"""M1 bucket routing seams (decision #142): gate/release per bucket behind the switch, bucket isolation,
flat audit trail kept, portfolio_id stamped on tickets / positions / outcomes / Court rows."""
import sqlite3

import pytest

from src import brain_map, buckets, config, oms, portfolio_manager as pm, strategy_router as sr
from src.execution import live_pricer
from src.validation import trial

IDX, DARL, L2 = "PAPER_10L/IDX_SPREADS", "PAPER_10L/DARLINGS", "PAPER_2L/IDX_SPREADS"


def _seed(c, pid, account, family, cap, mdd=10.0):
    c.execute("INSERT OR IGNORE INTO portfolios (portfolio_id, account_id, strategy_family, starting_capital, "
              "realized_pnl, peak_equity, risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, created_at) "
              "VALUES (?, ?, ?, ?, 0, ?, 2.0, ?, 3.0, 'now')", (pid, account, family, cap, cap, mdd))


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", True)
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    pm.get_paper_account(c, pm.ACCOUNT_PAPER_2L)
    _seed(c, IDX, "PAPER_10L", "index_options", 700000.0)
    _seed(c, DARL, "PAPER_10L", "darlings", 300000.0)
    _seed(c, L2, "PAPER_2L", "index_options", 200000.0)
    c.commit()
    yield c
    c.close()


def _open_bucket_lock(c, pid, ref):
    return c.execute("SELECT margin_rs FROM portfolio_margin_locks WHERE portfolio_id = ? AND journal_ref = ? "
                     "AND released_at IS NULL", (pid, ref)).fetchone()


def test_switch_off_or_no_bucket_row_returns_none(monkeypatch):
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    assert buckets.gate(c, "PAPER_10L", "opt00001", 1000.0, 1, False) is None          # switch off
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", True)
    assert buckets.gate(c, "PAPER_10L", "opt00001", 1000.0, 1, False) is None          # no bucket row
    verdict = pm.request_entry(c, "opt00001", 1000.0)                                   # flat ledger unchanged
    assert verdict["approved"] and "portfolio_id" not in verdict
    assert c.execute("SELECT COUNT(*) FROM portfolio_margin_locks").fetchone()[0] == 0


def test_gate_routes_to_the_bucket_and_keeps_the_flat_lock(conn):
    v = pm.request_entry(conn, "opt00001", 50000.0)
    assert v["approved"] and v["portfolio_id"] == IDX
    assert _open_bucket_lock(conn, IDX, "opt00001")[0] == 50000.0
    assert conn.execute("SELECT portfolio_id FROM margin_locks WHERE journal_ref = 'opt00001'").fetchone()[0] == IDX
    assert buckets.available_cash(conn, buckets.bucket(conn, IDX)) == 650000.0
    again = pm.request_entry(conn, "opt00001", 50000.0)                                  # idempotent re-request
    assert again["approved"] and again["reason"] == pm.HELD_LOCK_REASON and again["portfolio_id"] == IDX
    assert conn.execute("SELECT COUNT(*) FROM portfolio_margin_locks").fetchone()[0] == 1


def test_bucket_cash_is_judged_alone_not_the_account(conn):
    # the account has Rs.10L liquid, but the IDX bucket only Rs.7L: a 7.5L ask is refused by the bucket
    v = pm.request_entry(conn, "opt00002", 750000.0)
    assert not v["approved"] and "bucket PAPER_10L/IDX_SPREADS" in v["reason"] and v["portfolio_id"] == IDX
    assert conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0] == 0
    ev = conn.execute("SELECT event_type FROM portfolio_events WHERE portfolio_id = ?", (IDX,)).fetchall()
    assert [e[0] for e in ev] == ["margin_exhaustion"]
    # the DARLINGS bucket's own cash funds the desk's eqd: lock through the same door
    d = pm.request_entry(conn, "eqd:7", 250000.0, portfolio_id=DARL)
    assert d["approved"] and d["portfolio_id"] == DARL and _open_bucket_lock(conn, DARL, "eqd:7")


def test_dry_run_locks_nothing_in_either_ledger(conn):
    v = pm.request_entry(conn, "eqd:9", 10000.0, dry_run=True)                          # eqd: → DARLINGS by prefix
    assert v["approved"] and v["portfolio_id"] == DARL
    assert conn.execute("SELECT COUNT(*) FROM portfolio_margin_locks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM margin_locks").fetchone()[0] == 0


def test_release_settles_the_bucket_and_the_flat_ledger_together(conn):
    pm.request_entry(conn, "opt00003", 40000.0)
    out = pm.release_margin(conn, "opt00003", 12000.0)
    assert out["released"] and out["bucket"]["portfolio_id"] == IDX and out["bucket"]["equity"] == 712000.0
    assert _open_bucket_lock(conn, IDX, "opt00003") is None
    assert buckets.bucket(conn, IDX)["peak_equity"] == 712000.0
    assert buckets.bucket(conn, DARL)["realized_pnl"] == 0.0                              # the other bucket untouched
    assert pm.equity(conn) == 1012000.0                                                   # flat audit trail moved too
    curve = conn.execute("SELECT equity, drawdown_pct FROM portfolio_equity_curve WHERE portfolio_id = ?",
                         (IDX,)).fetchall()
    assert [tuple(r) for r in curve] == [(712000.0, 0.0)]


def test_darlings_halt_does_not_touch_idx_spreads(conn):
    pm.request_entry(conn, "eqd:1", 100000.0, portfolio_id=DARL)
    out = pm.release_margin(conn, "eqd:1", -45000.0)                                     # −15% of the DARLINGS bucket
    assert out["bucket"]["halted"] and out["bucket"]["newly_halted"]
    assert buckets.bucket(conn, DARL)["halted_at"] is not None
    assert not out["halted"]                                                             # account-level: −4.5% of 10L
    refused = pm.request_entry(conn, "eqd:2", 1000.0, portfolio_id=DARL)
    assert not refused["approved"] and refused["halt"]["event"] == "risk_of_ruin_halt"
    ok = pm.request_entry(conn, "opt00004", 20000.0)                                     # IDX keeps trading
    assert ok["approved"] and ok["portfolio_id"] == IDX
    assert buckets.bucket(conn, IDX)["halted_at"] is None


def test_shadow_account_bucket_gate_and_settle(conn):
    v = pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "opt00005", 30000.0, lots=2, primary_lots=3)
    assert v["approved"] and v["portfolio_id"] == L2
    lock = conn.execute("SELECT lots, portfolio_id FROM paper_margin_locks WHERE journal_ref = 'opt00005'").fetchone()
    assert tuple(lock) == (2, L2)
    assert _open_bucket_lock(conn, L2, "opt00005")[0] == 30000.0
    big = pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "opt00006", 180000.0)      # 2L bucket: 170k liquid
    assert not big["approved"] and big["portfolio_id"] == L2
    out = pm.paper_release_margin(conn, pm.ACCOUNT_PAPER_2L, "opt00005", -3000.0)
    assert out["released"]
    assert buckets.bucket(conn, L2)["realized_pnl"] == -3000.0 and _open_bucket_lock(conn, L2, "opt00005") is None
    assert buckets.bucket(conn, IDX)["realized_pnl"] == 0.0                              # the primary's bucket untouched


def test_shadow_halt_latched_on_its_own_bucket_only(conn):
    pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "opt00007", 50000.0)
    pm.paper_release_margin(conn, pm.ACCOUNT_PAPER_2L, "opt00007", -25000.0)            # −12.5% of the 2L bucket
    assert buckets.bucket(conn, L2)["halted_at"] is not None
    v = pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "opt00008", 1000.0)
    assert not v["approved"] and "halted since" in v["reason"]
    assert pm.request_entry(conn, "opt00009", 1000.0)["approved"]                        # the primary's IDX bucket is fine


def test_expiry_releases_the_bucket_lock_at_zero(conn):
    pm.request_entry(conn, "opt00010", 15000.0)
    pm.paper_request_entry(conn, pm.ACCOUNT_PAPER_2L, "opt00010", 5000.0)
    out = pm.expire_pending_lock(conn, "opt00010")
    assert set(out) == {"PAPER_10L", "PAPER_2L"}
    for pid in (IDX, L2):
        row = conn.execute("SELECT released_at, pnl_net FROM portfolio_margin_locks WHERE portfolio_id = ? AND "
                           "journal_ref = 'opt00010'", (pid,)).fetchone()
        assert row[0] is not None and row[1] == 0.0
    assert buckets.bucket(conn, IDX)["realized_pnl"] == 0.0


def test_lock_taken_with_the_switch_on_settles_with_it_off(conn, monkeypatch):
    pm.request_entry(conn, "opt00011", 10000.0)
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", False)
    out = pm.release_margin(conn, "opt00011", 500.0)
    assert out["released"] and out["bucket"]["portfolio_id"] == IDX
    assert _open_bucket_lock(conn, IDX, "opt00011") is None


def test_portfolio_id_rides_on_tickets_positions_outcomes_and_court_rows(conn):
    spread = {"strategy": "bull_call_spread", "direction": "bullish", "expiry": "2026-10-28", "lots": 1, "lot_size": 75,
              "legs": [{"side": "BUY", "option_type": "CE", "strike": 24000, "premium": 100.0},
                       {"side": "SELL", "option_type": "CE", "strike": 24100, "premium": 60.0}]}
    prop = {"ticker": "NIFTY", "short_id": "opt00012", "spread": spread, "signal": "x"}
    t = sr.issue(conn, prop, journal_ref="opt00012", account_id=pm.ACCOUNT_PAPER_2L)
    assert t["ticket"]["portfolio_id"] == L2
    assert conn.execute("SELECT portfolio_id FROM trade_tickets WHERE ticket_id = ?",
                        (t["ticket_id"],)).fetchone()[0] == L2
    entry = {"short_id": "opt00012", "ticker": "NIFTY", "spread": spread}
    view = {"lots": 1, "legs": [{"side": "BUY", "option_type": "CE", "strike": 24000, "avg_fill_price": 100.0},
                                {"side": "SELL", "option_type": "CE", "strike": 24100, "avg_fill_price": 60.0}]}
    live_pricer.open_position(conn, pm.ACCOUNT_PAPER_2L, entry, view)
    assert conn.execute("SELECT portfolio_id FROM paper_live_positions WHERE journal_ref = 'opt00012'").fetchone()[0] == L2
    oid = brain_map.record_outcome(conn, "opt00012", "2026-10-10", "NIFTY", archetype="bull_call_spread", r_multiple=1.0,
                                   portfolio_id=IDX)
    assert conn.execute("SELECT portfolio_id FROM outcomes WHERE id = ?", (oid,)).fetchone()[0] == IDX
    trial.ensure_schema(conn)
    r = trial.record_block(conn, "exposure", "2026-10-10", "NIFTY", "bullish", host_ref="opt00012", portfolio_id=IDX)
    assert conn.execute("SELECT portfolio_id FROM shadow_trades WHERE journal_ref = ?", (r["ref"],)).fetchone()[0] == IDX


def test_equity_exit_ticket_names_the_darlings_bucket():
    entry = {"id": 7, "ticker": "TCS", "funding": {"lock_ref": "eqd:7", "qty": 10}}
    t = sr.build_equity_exit_ticket(entry, 10, 3500.0, "target")
    assert t["portfolio_id"] == DARL
    opt = {"short_id": "opt00013", "ticker": "NIFTY",
           "spread": {"strategy": "bull_call_spread", "expiry": "2026-10-28", "lots": 1, "lot_size": 75,
                      "legs": [{"side": "BUY", "option_type": "CE", "strike": 24000, "premium": 100.0},
                               {"side": "SELL", "option_type": "CE", "strike": 24100, "premium": 60.0}]}}
    x = sr.build_exit_ticket(opt, {(24000.0, "CE"): 50.0, (24100.0, "CE"): 30.0}, "target")
    assert x["portfolio_id"] == IDX

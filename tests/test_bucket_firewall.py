"""M4 capital firewall: an options drawdown that latches IDX_SPREADS never freezes, lends from or liquidates
DARLINGS or EQ_SWING (with `multi_bucket_ledger` on)."""
import importlib.util
import sqlite3

import pytest

from src import buckets, config
from src import portfolio_manager as pm

spec = importlib.util.spec_from_file_location("m1", "scripts/migrate_m1_buckets.py")
m1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m1)

IDX, DARL, SWING = "PAPER_10L/IDX_SPREADS", "PAPER_10L/DARLINGS", "PAPER_10L/EQ_SWING"


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pm.get_account(c)
    pm.get_paper_account(c, pm.ACCOUNT_PAPER_2L)
    c.execute("CREATE TABLE IF NOT EXISTS treasury_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
              "equity_budget_rs REAL NOT NULL, updated_at TEXT NOT NULL)")
    c.execute("INSERT INTO treasury_state VALUES (1, 300000.0, 'now')")
    c.commit()
    m1.apply(c, m1.plan(c))
    c.execute("UPDATE portfolios SET starting_capital = 100000, peak_equity = 100000 WHERE portfolio_id = ?",
              (SWING,))
    c.execute("UPDATE portfolios SET starting_capital = starting_capital - 100000, "
              "peak_equity = peak_equity - 100000 WHERE portfolio_id = ?", (IDX,))
    c.commit()
    return c


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", True)


def _options_loss(c, pct_of_idx):
    """Lock an IDX spread and settle it at a loss of `pct_of_idx` % of the bucket's equity."""
    loss = round(buckets.equity(buckets.bucket(c, IDX)) * pct_of_idx / 100.0, 2)
    assert pm.request_entry(c, "opt-loser", 20000)["approved"]
    pm.release_margin(c, "opt-loser", -loss)
    return loss


def test_ten_pct_options_drawdown_halts_only_idx(switch_on):
    c = _db()
    assert pm.request_entry(c, "eqd:held", 50000)["approved"]
    assert pm.request_entry(c, "swing-held", 20000, portfolio_id=SWING)["approved"]
    _options_loss(c, 10.0)
    assert buckets.is_bucket_halted(c, IDX)
    assert not buckets.is_bucket_halted(c, DARL)
    assert not buckets.is_bucket_halted(c, SWING)

    refused = pm.request_entry(c, "opt-next", 10000)
    assert not refused["approved"] and refused["portfolio_id"] == IDX

    darl = pm.request_entry(c, "eqd:new", 40000)
    swing = pm.request_entry(c, "swing-new", 15000, portfolio_id=SWING)
    assert darl["approved"] and darl["portfolio_id"] == DARL
    assert swing["approved"] and swing["portfolio_id"] == SWING


def test_idx_halt_never_liquidates_or_blocks_settlement_elsewhere(switch_on):
    c = _db()
    assert pm.request_entry(c, "eqd:held", 50000)["approved"]
    assert pm.request_entry(c, "swing-held", 20000, portfolio_id=SWING)["approved"]
    _options_loss(c, 10.0)
    assert buckets.open_locks(c, DARL) == 50000.0
    assert buckets.open_locks(c, SWING) == 20000.0
    d = buckets.release(c, pm.ACCOUNT_PAPER_10L, "eqd:held", 4000.0)
    s = buckets.release(c, pm.ACCOUNT_PAPER_10L, "swing-held", 1500.0, portfolio_id=SWING)
    assert d["released"] and not d["halted"]
    assert s["released"] and not s["halted"]
    assert buckets.open_locks(c, DARL) == 0.0 and buckets.open_locks(c, SWING) == 0.0


def test_firm_wide_latch_from_options_loss_does_not_reach_routed_buckets(switch_on):
    c = _db()
    firm_peak = pm.get_account(c)["peak_equity"]
    pm.request_entry(c, "opt-loser", 20000)
    pm.release_margin(c, "opt-loser", -round(firm_peak * 0.105, 2))
    assert pm.trading_halted(c)
    assert buckets.is_bucket_halted(c, IDX)
    assert pm.request_entry(c, "eqd:new", 40000)["approved"]
    assert pm.request_entry(c, "swing-new", 15000, portfolio_id=SWING)["approved"]


def test_switch_off_the_flat_firm_halt_still_freezes_darlings(monkeypatch):
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", False)
    c = _db()
    firm_peak = pm.get_account(c)["peak_equity"]
    pm.request_entry(c, "opt-loser", 20000)
    pm.release_margin(c, "opt-loser", -round(firm_peak * 0.105, 2))
    assert not pm.request_entry(c, "eqd:new", 40000)["approved"]


def test_no_bucket_can_borrow_from_another(switch_on):
    c = _db()
    _options_loss(c, 10.0)
    ok, why = buckets.can_borrow_from_bucket(c, IDX, DARL, 10000)
    assert not ok and why.startswith("ring_fence_violation")
    ok, why = buckets.can_borrow_from_bucket(c, DARL, IDX, 10000)
    assert not ok and why.startswith("halted_bucket_quarantine")
    ok, why = buckets.can_borrow_from_bucket(c, SWING, DARL, 1)
    assert not ok


def test_idx_cash_exhaustion_does_not_spill_into_darlings(switch_on):
    c = _db()
    idx_cash = buckets.available_cash(c, buckets.bucket(c, IDX))
    r = pm.request_entry(c, "opt-huge", idx_cash + 1000)
    assert not r["approved"] and r["portfolio_id"] == IDX
    assert buckets.open_locks(c, DARL) == 0.0
    assert pm.request_entry(c, "eqd:new", 40000)["approved"]


def test_allocator_quarantines_idx_and_keeps_swing_and_darlings_whole():
    a = buckets.allocate_bucket_ceilings(1_000_000, halted_families={"IDX_SPREADS"},
                                         locked_in_halted_by_family={"IDX_SPREADS": 225_000})
    assert a["allocations"]["IDX_SPREADS"] == 0.0
    assert a["allocations"]["DARLINGS"] == 300_000.0
    assert a["allocations"]["EQ_SWING"] == 200_000.0
    assert a["quarantined_halted_rs"] == 225_000.0
    assert a["total_allocated_rs"] + a["quarantined_halted_rs"] <= 1_000_000.0


def test_is_bucket_halted_is_read_only_and_per_bucket(switch_on):
    c = _db()
    c.execute("UPDATE portfolios SET realized_pnl = -peak_equity * 0.2 WHERE portfolio_id = ?", (IDX,))
    assert buckets.is_bucket_halted(c, IDX)
    assert buckets.bucket(c, IDX)["halted_at"] is None
    assert not buckets.is_bucket_halted(c, DARL)
    assert not buckets.is_bucket_halted(c, "PAPER_10L/NOPE")


def test_idx_ruin_does_not_shrink_the_darlings_ceiling(switch_on):
    c = _db()
    before = buckets.ceiling_state(c, DARL)["ceiling_rs"]
    assert pm.request_entry(c, "eqd:big", 290000)["approved"]
    _options_loss(c, 10.0)
    st = buckets.ceiling_state(c, DARL)
    assert st["ceiling_rs"] == before and not st["close_only"]
    assert pm.request_entry(c, "eqd:more", 5000)["approved"]


def test_a_loss_short_of_the_latch_still_counts_in_the_ceiling(switch_on):
    c = _db()
    before = buckets.ceiling_state(c, DARL)["ceiling_rs"]
    _options_loss(c, 5.0)
    assert not buckets.is_bucket_halted(c, IDX)
    assert buckets.ceiling_state(c, DARL)["ceiling_rs"] < before

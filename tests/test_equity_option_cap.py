"""src/equity_option_cap.py — the 33% stock-option sub-cap (owner rule 2026-11-02)."""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from src import brain_map, buckets, config, equity_option_cap as cap, journal
from src import options_proposer as op, portfolio_manager as pm, proposal_ledger

IDX = "PAPER_10L/IDX_SPREADS"


def _conn():
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)                    # PAPER_10L, Rs.10,00,000
    return c


def _lock(c, ref, margin, released=False):
    c.execute("INSERT INTO margin_locks (journal_ref, margin_rs, locked_at, released_at) VALUES (?, ?, 'now', ?)",
              (ref, margin, "later" if released else None))


ENTRIES = [{"short_id": "tcs1", "ticker": "TCS.NS"}, {"short_id": "rel1", "ticker": "RELIANCE.NS"},
           {"short_id": "nifty1", "ticker": "NIFTY 50"}, {"short_id": "icic1", "ticker": "ICICIBANK.NS"}]


@pytest.fixture
def book():
    c = _conn()
    _lock(c, "tcs1", 200000.0)
    _lock(c, "rel1", 100000.0)
    _lock(c, "nifty1", 500000.0)            # index: never counted
    _lock(c, "eqd:abc", 90000.0)            # darling stock: never counted
    _lock(c, "icic1", 70000.0, released=True)   # settled: never counted
    c.commit()
    yield c
    c.close()


def test_consumed_counts_only_open_stock_option_locks(book):
    assert cap.consumed_rs(book, ENTRIES) == 300000.0
    assert cap.consumed_rs(book, ENTRIES, exclude_ref="tcs1") == 100000.0


def test_index_options_are_never_judged(book):
    assert cap.check(book, "NIFTY 50", 9e9, entries=ENTRIES) == (True, None)
    assert cap.check(book, "NIFTY BANK", 9e9, entries=ENTRIES) == (True, None)
    # and gate() opens no database for an index (a broken connect cannot touch it)
    with mock.patch.object(brain_map, "connect", side_effect=AssertionError("no db for index")):
        assert cap.gate("NIFTY FIN SERVICE", 1e9) == (True, None)


def test_stock_option_refused_once_33pct_of_the_budget_would_be_passed(book):
    st = cap.state(book, ENTRIES)
    assert st["budget_rs"] == 1000000.0 and st["ceiling_rs"] == 330000.0 and st["headroom_rs"] == 30000.0
    assert "no IDX_SPREADS bucket row" in st["budget_basis"]
    assert cap.check(book, "INFY.NS", 30000.0, entries=ENTRIES) == (True, None)      # lands exactly on 33%
    ok, why = cap.check(book, "INFY.NS", 30000.01, journal_ref="new1", entries=ENTRIES)
    assert ok is False and "stock-option sub-cap" in why and "33% of the options budget" in why
    ev = book.execute("SELECT event_type, detail FROM account_events WHERE event_type = ?",
                      (cap.REFUSED_EVENT,)).fetchall()
    assert len(ev) == 1 and "new1" in ev[0]["detail"]


def test_approval_rejudges_with_its_own_lock_excluded(book):
    # tcs1 (Rs.2L) already holds its lock: approving it again must not count it twice
    assert cap.check(book, "TCS.NS", 200000.0, journal_ref="tcs1", entries=ENTRIES) == (True, None)


def test_budget_follows_the_idx_bucket(book, monkeypatch):
    book.execute("INSERT INTO portfolios (portfolio_id, account_id, strategy_family, starting_capital, "
                 "realized_pnl, peak_equity, risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, created_at) "
                 "VALUES (?, 'PAPER_10L', 'index_options', 800000, 200000, 1000000, 2.0, 10.0, 3.0, 'now')", (IDX,))
    book.commit()
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", False)
    total, basis = cap.budget(book)
    assert total == 1000000.0 and basis == "IDX_SPREADS bucket equity"
    # ledger ON: the bucket's ceiling (25% of firm equity) is the active budget
    monkeypatch.setattr(config, "MULTI_BUCKET_LEDGER", True)
    total, basis = cap.budget(book)
    firm = buckets.ceiling_state(book, IDX)["firm_equity_rs"]
    assert total == round(min(1000000.0, firm * 0.25), 2) and "ceiling" in basis


def test_switch_off_and_fail_closed(book, monkeypatch):
    monkeypatch.setattr(config, "EQUITY_OPTION_CAP_PCT", 0.0)
    assert cap.check(book, "TCS.NS", 9e9, entries=ENTRIES) == (True, None)
    monkeypatch.setattr(config, "EQUITY_OPTION_CAP_PCT", 33.0)
    monkeypatch.setattr(cap, "budget", lambda conn: (0.0, "test zero"))
    ok, why = cap.check(book, "TCS.NS", 1.0, entries=ENTRIES)
    assert ok is False and "fails closed" in why
    monkeypatch.setattr(cap, "budget", lambda conn: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, why = cap.check(book, "TCS.NS", 1.0, entries=ENTRIES)
    assert ok is False and "boom" in why


def test_proposal_ledger_names_the_fate():
    assert proposal_ledger.classify({"proposed": False, "reason": "stock-option sub-cap: open ..."}) \
        == "REJECTED_EQUITY_CAP"


# ------------------------------------------------ run_headless / decide_pending wiring

def _stock_proposal():
    return {"action": "SPREAD", "ticker": "TCS.NS", "shares": 225, "price": 20.0, "signal": "t",
            "view": "bearish", "vix": 14.0, "lots": 1,
            "spread": {"strategy": "bear_put_spread", "direction": "bearish", "legs": [],
                       "lot_size": 225, "lots": 1, "expiry": "2026-12-29", "net_debit": 20.0,
                       "max_loss": 4500.0, "max_profit": 9000.0, "margin": {"total_margin": 4500.0}}}


def _headless(learner_on, cap_verdict):
    tmp = Path(tempfile.mkdtemp())
    learner, margin_gate = mock.Mock(return_value={"proposed": False, "reason": "learner"}), \
        mock.Mock(return_value=(True, "margin locked"))
    with mock.patch.object(journal, "DATA_DIR", tmp), \
         mock.patch.object(journal, "JOURNAL_PATH", tmp / "journal.jsonl"), \
         mock.patch.object(op, "build_proposal", return_value={"proposal": _stock_proposal(), "reason": "ok"}), \
         mock.patch.object(op, "_gex_own_entry"), \
         mock.patch.object(op, "_learner_enabled", return_value=learner_on), \
         mock.patch.object(op, "_learner_path", learner), \
         mock.patch("src.exposure_gate.gate_entry", return_value=(True, "allowed")), \
         mock.patch.object(cap, "gate", return_value=cap_verdict) as cap_gate, \
         mock.patch("src.portfolio_manager.gate_headless_entry", margin_gate), \
         mock.patch.object(op, "_memory_context_for", return_value=""), \
         mock.patch.object(op, "_skeptic_note_for", return_value=""), \
         mock.patch.object(op, "_format_proposal_alert", lambda p, action_note="": "alert"), \
         mock.patch.object(op, "_notify_discord"), \
         mock.patch("src.notifier.fire_broadcast"):
        result = op.run_headless("TCS.NS", state={})
    return result, learner, margin_gate, cap_gate


def test_capped_stock_option_goes_to_the_learner_and_never_locks_margin():
    result, learner, margin_gate, cap_gate = _headless(True, (False, "stock-option sub-cap: full"))
    learner.assert_called_once()
    assert learner.call_args[0][3] == "stock-option sub-cap: full"   # the learner takes it, reason named
    margin_gate.assert_not_called()
    assert cap_gate.call_args[0][0] == "TCS.NS" and cap_gate.call_args[0][1] == 4500.0


def test_capped_stock_option_without_learner_is_refused_by_name():
    result, learner, margin_gate, _ = _headless(False, (False, "stock-option sub-cap: full"))
    assert result["proposed"] is False and result["reason"] == "stock-option sub-cap: full"
    learner.assert_not_called()
    margin_gate.assert_not_called()


def test_under_the_cap_the_flow_is_unchanged():
    with mock.patch.object(op, "_judge_shadow_accounts", return_value={}), \
         mock.patch.object(op, "_gex_steering_read", return_value=None):
        result, learner, margin_gate, _ = _headless(True, (True, None))
    learner.assert_not_called()
    margin_gate.assert_called_once()


def test_approval_of_a_capped_stock_option_stays_pending_like_a_margin_block():
    from datetime import date
    entry = op.to_journal_entry(_stock_proposal(), "pending_approval", "t")
    entry["date"] = date.today().isoformat()
    journal.log(entry)
    margin_gate = mock.Mock(return_value=(True, "margin locked"))
    with mock.patch("src.exposure_gate.gate_approval", return_value=(True, "allowed", False)), \
         mock.patch.object(cap, "gate", return_value=(False, "stock-option sub-cap: full")) as cap_gate, \
         mock.patch("src.portfolio_manager.gate_headless_entry", margin_gate), \
         mock.patch("src.notifier.fire_broadcast"):
        out = op.decide_pending(entry["short_id"], approve=True, why="t")
    assert out["status"] == op.MARGIN_BLOCKED and out["reason"] == "stock-option sub-cap: full"
    assert cap_gate.call_args.kwargs["journal_ref"] == entry["short_id"]
    margin_gate.assert_not_called()
    assert journal.get_entry(entry["short_id"])["decision"] == "pending_approval"

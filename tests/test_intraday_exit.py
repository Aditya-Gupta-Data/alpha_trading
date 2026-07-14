"""
Tests for the intraday profit-take square-off (decision #69). Offline —
fake chain quotes, temp journal/portfolio, muzzled notifier.

Run either of these from the project folder:
    python -m pytest tests/test_intraday_exit.py
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import journal, live_bridge as lb, plan_tracker as pt
from src import portfolio as pf


def _spread_entry(short_id="iday0001", decision="approved",
                  ticker="NIFTY 50"):
    """A bear put spread: long 24000 PE @150, short 23500 PE @70 —
    net debit 80/share, max profit 420/share... using the constructor's
    field shape (max_profit/max_loss are per-LOT totals)."""
    lot = 75
    return {
        "short_id": short_id, "date": "2026-07-10", "ticker": ticker,
        "action": "BUY", "price": 80.0, "decision": decision,
        "signal": "bearish trend", "why": "test",
        "spread": {
            "strategy": "bear_put_spread", "expiry": "2026-07-21",
            "lot_size": lot, "lots": 1, "entry_spot": 24500.0,
            "max_profit": 420.0 * lot, "max_loss": 80.0 * lot,
            "legs": [
                {"side": "BUY", "option_type": "PE", "strike": 24000.0,
                 "premium": 150.0},
                {"side": "SELL", "option_type": "PE", "strike": 23500.0,
                 "premium": 70.0},
            ],
        },
    }


# Quotes where the basket exit mark = 460 - 80 = 380/share profit vs the
# 80 entry: profit 300/share = 71.4% of max profit 420 -> above the 65%
# threshold on REAL quotes.
WIN_QUOTES = {(24000.0, "PE"): 500.0, (23500.0, "PE"): 120.0}
# Quotes where real profit is only ~48% (model said 70) -> refuse.
WEAK_QUOTES = {(24000.0, "PE"): 330.0, (23500.0, "PE"): 48.0}


def _sandbox(tmp_path, monkeypatch, entries):
    """Point journal + portfolio + brain_map at temp files."""
    jpath = tmp_path / "journal.jsonl"
    monkeypatch.setattr(journal, "JOURNAL_PATH", jpath)
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    journal.rewrite_all(entries)
    ppath = tmp_path / "portfolio.json"
    ppath.write_text(json.dumps({"cash": 100000.0, "holdings": {}}))
    monkeypatch.setattr(pf, "PORTFOLIO_PATH", ppath, raising=False)
    from src import brain_map
    monkeypatch.setattr(brain_map, "DEFAULT_DB_PATH",
                        tmp_path / "brain.db")
    return jpath, ppath


def test_squares_off_on_real_quotes_and_settles_everything(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch, [_spread_entry()])
    out = pt.resolve_intraday_profit_take("iday0001", WIN_QUOTES,
                                          model_capture_pct=83.0,
                                          today=date(2026, 7, 14))
    assert out["status"] == "squared_off"
    assert out["pnl_rs"] > 0 and out["capture_pct"] > 65

    entry = journal.read_all()[0]
    o = entry["outcome"]
    assert o["resolution"] == "profit_take"
    assert o["exit_basis"] == "intraday_chain"
    assert o["model_capture_pct"] == 83.0        # signal-vs-fill measured
    assert o["exit_date"] == "2026-07-14"
    assert o["pnl_rs"] == out["pnl_rs"]
    # Cash settled net.
    book = json.loads((tmp_path / "portfolio.json").read_text())
    assert abs(book["cash"] - (100000.0 + out["pnl_rs"])) < 0.01
    # P&L clamped inside defined-risk bounds.
    assert o["pnl_rs"] <= 420.0 * 75


def test_idempotent_second_call_refuses(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch, [_spread_entry()])
    assert pt.resolve_intraday_profit_take(
        "iday0001", WIN_QUOTES, today=date(2026, 7, 14))["status"] == "squared_off"
    again = pt.resolve_intraday_profit_take(
        "iday0001", WIN_QUOTES, today=date(2026, 7, 14))
    assert again["status"] == "already_resolved"
    book = json.loads((tmp_path / "portfolio.json").read_text())
    # Cash settled exactly once.
    first_pnl = journal.read_all()[0]["outcome"]["pnl_rs"]
    assert abs(book["cash"] - (100000.0 + first_pnl)) < 0.01


def test_real_quote_verification_gate_refuses_weak_fills(tmp_path, monkeypatch):
    """The model said 70% but real quotes say ~48% — no exit; the trade
    stays open for the EOD path (model-vs-market divergence guard)."""
    _sandbox(tmp_path, monkeypatch, [_spread_entry()])
    out = pt.resolve_intraday_profit_take("iday0001", WEAK_QUOTES,
                                          model_capture_pct=70.0,
                                          today=date(2026, 7, 14))
    assert out["status"] == "below_threshold_on_real_quotes"
    assert out["real_capture_pct"] < 65
    assert journal.read_all()[0].get("outcome") is None   # untouched


def test_pending_and_missing_quotes_refuse(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch,
             [_spread_entry(decision="pending_approval")])
    out = pt.resolve_intraday_profit_take("iday0001", WIN_QUOTES,
                                          today=date(2026, 7, 14))
    assert out["status"] == "not_approved"
    _sandbox(tmp_path, monkeypatch, [_spread_entry()])
    out = pt.resolve_intraday_profit_take(
        "iday0001", {(24000.0, "PE"): 500.0},   # short leg quote missing
        today=date(2026, 7, 14))
    assert out["status"] == "missing_leg_quote"
    assert journal.read_all()[0].get("outcome") is None


def test_update_entry_merges_never_clobbers(tmp_path, monkeypatch):
    """Two sequential single-entry updates preserve each other's writes —
    the race-safety contract run_tracker/#69 rely on."""
    e1, e2 = _spread_entry("aaaa0001"), _spread_entry("bbbb0002")
    _sandbox(tmp_path, monkeypatch, [e1, e2])

    def close_a(entry):
        entry["outcome"] = {"pnl_rs": 1.0}
    def close_b(entry):
        entry["outcome"] = {"pnl_rs": 2.0}
    journal.update_entry("aaaa0001", close_a)
    journal.update_entry("bbbb0002", close_b)
    rows = {e["short_id"]: e for e in journal.read_all()}
    assert rows["aaaa0001"]["outcome"]["pnl_rs"] == 1.0
    assert rows["bbbb0002"]["outcome"]["pnl_rs"] == 2.0
    # Abort path writes nothing.
    assert journal.update_entry("aaaa0001", lambda e: False) is None


def test_live_cycle_default_stays_readonly_advisory(tmp_path, monkeypatch):
    """No square_off_fn (every legacy caller/test) -> byte-identical
    advisory behavior, nothing settled (decision #41 baseline)."""
    entries = [_spread_entry()]
    _sandbox(tmp_path, monkeypatch, entries)
    notes = []
    fired = lb.live_cycle(
        ["NIFTY 50"], quote_fn=lambda t: {"last_price": 23400.0},
        entries=entries, notify_fn=notes.append,
        now_fn=lambda: __import__("datetime").datetime(2026, 7, 14, 11, 0))
    assert journal.read_all()[0].get("outcome") is None
    assert all("Advisory only" in n for n in notes if "exit signal" in n)


def test_live_cycle_squares_off_when_armed(tmp_path, monkeypatch):
    entries = [_spread_entry()]
    _sandbox(tmp_path, monkeypatch, entries)
    notes = []

    def fake_square_off(sig):
        return lb.intraday_square_off(
            sig, entries=journal.read_all(),
            quotes_fn=lambda e: WIN_QUOTES, today=date(2026, 7, 14))

    # Spot low enough that the model fires profit_take (deep ITM move).
    fired = lb.live_cycle(
        ["NIFTY 50"], quote_fn=lambda t: {"last_price": 23000.0},
        entries=entries, notify_fn=notes.append,
        now_fn=lambda: __import__("datetime").datetime(2026, 7, 14, 11, 0),
        square_off_fn=fake_square_off)
    assert any(s.get("squared_off") for s in fired)
    assert journal.read_all()[0]["outcome"]["exit_basis"] == "intraday_chain"
    assert any("SQUARED OFF intraday" in n for n in notes)


def test_square_off_falls_back_to_advisory_without_quotes(tmp_path, monkeypatch):
    entries = [_spread_entry()]
    _sandbox(tmp_path, monkeypatch, entries)
    notes = []
    fired = lb.live_cycle(
        ["NIFTY 50"], quote_fn=lambda t: {"last_price": 23000.0},
        entries=entries, notify_fn=notes.append,
        now_fn=lambda: __import__("datetime").datetime(2026, 7, 14, 11, 0),
        square_off_fn=lambda sig: lb.intraday_square_off(
            sig, entries=journal.read_all(), quotes_fn=lambda e: None,
            today=date(2026, 7, 14)))
    assert journal.read_all()[0].get("outcome") is None      # untouched
    assert any("intraday fill declined: no_chain_quotes" in n for n in notes)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

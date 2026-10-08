"""Audit Chunk 3, Batch B: Z2/Z4 margin wall on real cash + stressed margin, Z3 sizing journaled, S5 duplicate trials."""
import sqlite3
from src import adaptive_sizing as az, options_proposer as op, portfolio_manager as pm


def test_z4_shadow_sizer_counts_lots_on_the_vix_stressed_margin():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    pm.ensure_accounts_schema(conn)
    s = {"max_loss": 1956.0, "margin": {"total_margin": 17673.0}}
    calm = pm.size_for_account(conn, pm.ACCOUNT_PAPER_2L, s, risk_pct=2.0, vix=12.0)
    hot = pm.size_for_account(conn, pm.ACCOUNT_PAPER_2L, s, risk_pct=2.0, vix=17.0)
    assert calm["by_margin"] == int(200000 // 17673)
    assert hot["by_margin"] == int(200000 // (17673 * 1.15)) and hot["lots"] <= calm["lots"]


def test_z3_sizing_rides_into_the_journal_entry():
    proposal = {"action": "SPREAD", "ticker": "NIFTY 50", "shares": 65, "price": 30.0, "signal": "x",
                "spread": {"strategy": "bull_call_spread", "legs": []}, "lots": 1,
                "sizing": {"by_risk": 0, "by_margin": 3, "floor_applied": True, "lots_final": 1}}
    e = op.to_journal_entry(proposal, "approved", "why")
    assert e["sizing"]["floor_applied"] is True and e["sizing"]["lots_final"] == 1


def test_z2_primary_cash_seam_falls_back_to_the_book_only_without_a_ledger():
    assert op._primary_available_cash({"cash": 28980.0}) == 28980.0     # pytest: no ledger read


def test_s5_identical_positions_collapse_to_one_trial():
    sp = {"strategy": "bear_put_spread", "expiry": "2026-10-27",
          "legs": [{"side": "BUY", "strike": 1340.0, "option_type": "PE"},
                   {"side": "SELL", "strike": 1300.0, "option_type": "PE"}]}
    e = [{"ticker": "ICICIBANK.NS", "date": "2026-09-23", "spread": sp,
          "outcome": {"r_multiple": 0.9, "exit_date": "2026-09-24"}} for _ in range(3)]
    e.append({"ticker": "ICICIBANK.NS", "date": "2026-09-25", "spread": sp,
              "outcome": {"r_multiple": -1.0, "exit_date": "2026-09-26"}})
    rows = az.options_history(entries=e)
    assert len(rows) == 2 and sorted(r["n_rows"] for r in rows) == [1, 3]
    assert sum(r["weight"] for r in rows) == 2.0

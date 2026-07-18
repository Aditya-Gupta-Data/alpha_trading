"""
Tests for the paper portfolio and strategy engine, using fake data so they
run instantly with no internet.

Run either of these from the project folder:
    python tests/test_portfolio.py      (simple, no extra installs)
    python -m pytest tests/             (if you have pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.portfolio import buy, sell, total_value, max_affordable_shares, STARTING_CASH
from src.strategy import propose
from src.review import verdict_for


def fresh_book():
    return {"cash": STARTING_CASH, "holdings": {}, "created": "2026-01-01"}


def make_analysis(ticker="TEST.NS", uptrend=True, fresh_cross=False, rsi=50, price=100.0):
    return {
        "ticker": ticker,
        "uptrend": uptrend,
        "fresh_cross": fresh_cross,
        "rsi": rsi,
        "price": price,
    }


def test_buy_updates_cash_and_holdings():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    assert book["cash"] == STARTING_CASH - 1023.67  # includes BUY frictions (Rs. 23.67)
    assert book["holdings"]["TEST.NS"]["shares"] == 10


def test_buy_cannot_overspend():
    book = fresh_book()
    try:
        buy(book, "TEST.NS", 2000, 100.0)  # Rs.2,00,000 > cash
        assert False, "should have raised"
    except ValueError:
        pass


def test_sell_returns_cash_and_clears_position():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    sell(book, "TEST.NS", 110.0)
    # 10 shares x Rs.10 profit (Rs.100) minus BUY and SELL frictions (Rs.23.67 + Rs.25.30 = Rs.48.97)
    assert book["cash"] == STARTING_CASH + 51.03
    assert "TEST.NS" not in book["holdings"]


def test_position_cap_limits_shares():
    book = fresh_book()
    # 25% of Rs.1,00,000 = Rs.25,000 -> at Rs.100/share, max 250 even though cash allows 1000
    assert max_affordable_shares(book, 100.0, {}) == 250


def test_total_value_uses_live_prices():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    assert total_value(book, {"TEST.NS": 120.0}) == STARTING_CASH + 176.33


def test_propose_buys_on_dip_in_uptrend():
    book = fresh_book()
    prop = propose(make_analysis(uptrend=True, rsi=25), book, {})
    assert prop is not None and prop["action"] == "BUY"


def test_propose_ignores_neutral_uptrend():
    book = fresh_book()
    assert propose(make_analysis(uptrend=True, rsi=50), book, {}) is None


def test_propose_sells_holding_in_downtrend():
    book = fresh_book()
    buy(book, "TEST.NS", 10, 100.0)
    prop = propose(make_analysis(uptrend=False, rsi=50), book, {"TEST.NS": 100.0})
    assert prop is not None and prop["action"] == "SELL" and prop["shares"] == 10


def test_propose_never_sells_what_we_dont_hold():
    book = fresh_book()
    assert propose(make_analysis(uptrend=False, rsi=50), book, {}) is None


def test_verdicts():
    approved_buy = {"action": "BUY", "decision": "approved"}
    assert verdict_for(approved_buy, +5.0).startswith("WIN")
    assert verdict_for(approved_buy, -5.0).startswith("LOSS")
    assert "flat" in verdict_for(approved_buy, +0.5)
    rejected_buy = {"action": "BUY", "decision": "rejected"}
    assert verdict_for(rejected_buy, -5.0).startswith("GOOD SKIP")
    assert verdict_for(rejected_buy, +5.0).startswith("MISSED GAIN")


def test_calculate_trade_frictions_and_slippage():
    from src.portfolio import calculate_trade_frictions
    from src.plan_tracker import apply_slippage, _get_instrument_type

    # 1. Friction tests (2026 stack: STT sell-only, Stamp buy-only, Rs.20
    #    brokerage, NSE exchange 0.00345%, SEBI 0.0001%, 18% GST on the
    #    brokerage+exchange+SEBI service charges)
    # BUY 100 x Rs.150 = turnover Rs.15,000:
    #   Stamp 0.45 + Brokerage 20 + Exchange 0.5175 + SEBI 0.015
    #   + GST 0.18*(20+0.5175+0.015)=3.69585  -> 24.68 (STT=0 on buys)
    assert calculate_trade_frictions("STOCK", "BUY", 150.0, 100) == 24.68

    # SELL same turnover:
    #   STT 22.50 + Brokerage 20 + Exchange 0.5175 + SEBI 0.015
    #   + GST 3.69585  -> 46.73 (Stamp=0 on sells)
    assert calculate_trade_frictions("STOCK", "SELL", 150.0, 100) == 46.73

    # 2. Slippage tests
    assert _get_instrument_type("RELIANCE.NS") == "STOCK"
    assert _get_instrument_type("NIFTY 50") == "INDEX"
    assert _get_instrument_type("NIFTY26JUL25000CE") == "OPTION"

    # Index slippage: 0.05%
    assert apply_slippage(10000.0, "INDEX") == 5.0

    # Option slippage (liquidity dummy):
    # premium < 50: 0.50%
    assert apply_slippage(40.0, "OPTION") == 0.20
    # premium < 150: 0.30%
    assert apply_slippage(100.0, "OPTION") == 0.30
    # premium >= 150: 0.10%
    assert apply_slippage(200.0, "OPTION") == 0.20


# --- Phase 6G: the capital & margin allocation layer --------------------
# All against an in-memory brain_map DB — no file, no network, instant.

from src import brain_map
from src import portfolio_manager as pm


def pm_conn():
    return brain_map.connect(":memory:")


def test_pm_account_initializes_with_ten_lakh():
    conn = pm_conn()
    acct = pm.get_account(conn)
    assert acct["starting_capital"] == 1_000_000.0
    assert acct["realized_pnl"] == 0
    assert pm.equity(conn) == 1_000_000.0
    assert pm.available_cash(conn) == 1_000_000.0
    assert pm.drawdown_pct(conn) == 0.0
    assert not pm.trading_halted(conn)


def test_pm_entry_locks_margin_and_reduces_available_cash():
    conn = pm_conn()
    verdict = pm.request_entry(conn, "trade001", 250_000.0)
    assert verdict["approved"]
    assert pm.locked_margin(conn) == 250_000.0
    assert pm.available_cash(conn) == 750_000.0
    # equity is untouched by a lock — margin is blocked, not spent
    assert pm.equity(conn) == 1_000_000.0


def test_pm_relock_same_ref_is_idempotent():
    conn = pm_conn()
    pm.request_entry(conn, "trade001", 250_000.0)
    verdict = pm.request_entry(conn, "trade001", 250_000.0)
    assert verdict["approved"]
    assert pm.locked_margin(conn) == 250_000.0  # not double-locked


def test_pm_margin_exhaustion_rejects_and_logs_event():
    conn = pm_conn()
    assert pm.request_entry(conn, "t1", 600_000.0)["approved"]
    verdict = pm.request_entry(conn, "t2", 500_000.0)  # only 4L liquid left
    assert not verdict["approved"]
    assert "margin exhaustion" in verdict["reason"]
    assert pm.locked_margin(conn) == 600_000.0  # nothing new locked
    events = conn.execute("SELECT event_type FROM account_events").fetchall()
    assert [e[0] for e in events] == ["margin_exhaustion"]


def test_pm_margin_exactly_equal_to_cash_is_allowed():
    conn = pm_conn()
    assert pm.request_entry(conn, "t1", 1_000_000.0)["approved"]
    assert pm.available_cash(conn) == 0.0
    # and now literally any further entry is margin-exhausted
    assert not pm.request_entry(conn, "t2", 0.01)["approved"]


def test_pm_release_settles_pnl_and_frees_margin():
    conn = pm_conn()
    pm.request_entry(conn, "t1", 300_000.0)
    result = pm.release_margin(conn, "t1", pnl_net=42_000.0)
    assert result["released"]
    assert pm.locked_margin(conn) == 0.0
    assert pm.equity(conn) == 1_042_000.0
    assert pm.available_cash(conn) == 1_042_000.0


def test_pm_release_unknown_ref_is_safe_noop():
    conn = pm_conn()
    result = pm.release_margin(conn, "never-locked", pnl_net=99_999.0)
    assert not result["released"]
    assert pm.equity(conn) == 1_000_000.0  # P&L NOT applied


def test_pm_equity_curve_records_every_settlement():
    conn = pm_conn()
    for i, pnl in enumerate((10_000.0, -5_000.0, 2_500.0)):
        ref = f"t{i}"
        pm.request_entry(conn, ref, 100_000.0)
        pm.release_margin(conn, ref, pnl)
    rows = conn.execute("SELECT equity, peak_equity, drawdown_pct "
                        "FROM equity_curve ORDER BY rowid").fetchall()
    assert len(rows) == 3
    assert rows[0][0] == 1_010_000.0 and rows[0][1] == 1_010_000.0
    assert rows[1][0] == 1_005_000.0 and rows[1][1] == 1_010_000.0  # peak holds
    assert rows[1][2] > 0                                            # in drawdown
    assert rows[2][0] == 1_007_500.0


def test_pm_peak_ratchets_and_drawdown_trails_from_it():
    conn = pm_conn()
    pm.request_entry(conn, "win", 100_000.0)
    pm.release_margin(conn, "win", 50_000.0)          # equity 10.5L, peak 10.5L
    pm.request_entry(conn, "loss", 100_000.0)
    pm.release_margin(conn, "loss", -100_000.0)       # equity 9.5L
    # drawdown measured from the RATCHETED 10.5L peak, not the 10L start
    assert pm.drawdown_pct(conn) == round((1_050_000 - 950_000) / 1_050_000 * 100, 4)
    assert not pm.trading_halted(conn)                # 9.52% < 10%


def _backdate_settlement(conn, ref):
    """Move a settled lock to a past day: the LIFETIME drawdown persists
    across days while the daily circuit breaker (merged 2026-07-19, a
    tighter same-day fuse) resets at the IST day boundary — backdating
    isolates the lifetime halt this test is about."""
    conn.execute("UPDATE margin_locks SET released_at = "
                 "'2000-01-01T10:00:00' WHERE journal_ref = ?", (ref,))
    conn.commit()


def test_pm_consecutive_losses_trip_the_risk_of_ruin_halt():
    conn = pm_conn()
    # three consecutive Rs.40,000 losses (each settled on its own past
    # day — same-day they'd trip the DAILY breaker first, by design): the
    # third carries the account through the 10% line (Rs.1.2L = 12%).
    for i, pnl in enumerate((-40_000.0, -40_000.0, -40_000.0)):
        ref = f"loss{i}"
        assert pm.request_entry(conn, ref, 50_000.0)["approved"], \
            f"entry {i} should still be allowed"
        pm.release_margin(conn, ref, pnl)
        _backdate_settlement(conn, ref)
    assert pm.drawdown_pct(conn) == 12.0
    assert pm.trading_halted(conn)
    # the halt was logged the moment the breach happened
    events = [e[0] for e in conn.execute(
        "SELECT event_type FROM account_events").fetchall()]
    assert "risk_of_ruin_halt" in events


def test_pm_halt_blocks_even_easily_affordable_entries():
    conn = pm_conn()
    pm.request_entry(conn, "big", 100_000.0)
    pm.release_margin(conn, "big", -150_000.0)        # 15% drawdown — halted
    verdict = pm.request_entry(conn, "tiny", 1_000.0)  # trivially affordable
    assert not verdict["approved"]
    assert "risk-of-ruin" in verdict["reason"]


def test_pm_drawdown_boundary_is_inclusive_at_ten_percent():
    conn = pm_conn()
    pm.request_entry(conn, "a", 200_000.0)
    pm.release_margin(conn, "a", -99_900.0)           # 9.99% — still trading
    _backdate_settlement(conn, "a")   # isolate the lifetime halt (see above)
    assert not pm.trading_halted(conn)
    assert pm.request_entry(conn, "b", 1_000.0)["approved"]
    pm.release_margin(conn, "b", -100.0)              # exactly 10.00% — halted
    assert pm.drawdown_pct(conn) == 10.0
    assert pm.trading_halted(conn)


def test_pm_extreme_loss_streak_never_allows_overlocking():
    conn = pm_conn()
    # grind the account down with losses while margin stays locked: at no
    # point may active locks exceed what the shrinking equity can cover.
    for i in range(20):
        ref = f"grind{i}"
        verdict = pm.request_entry(conn, ref, 400_000.0)
        if not verdict["approved"]:
            break
        assert pm.locked_margin(conn) <= pm.equity(conn) + 1e-9
        pm.release_margin(conn, ref, -30_000.0)
    assert pm.trading_halted(conn) or not verdict["approved"]


def test_pm_required_margin_scales_by_lots():
    proposal = {"spread": {"margin": {"total_margin": 2_800.0}, "lots": 3},
                "lots": 3}
    assert pm.required_margin_for(proposal) == 8_400.0


def test_pm_gate_and_release_fail_open_on_a_dead_db():
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
        def executescript(self, *a, **k):
            raise RuntimeError("db down")

    allowed, reason = pm.gate_headless_entry("x", 100.0, conn=Boom())
    assert allowed is True                 # fail OPEN — never stall the loop
    assert "unavailable" in reason
    result = pm.release_entry("x", 5_000.0, conn=Boom())
    assert not result["released"]


def test_pm_run_headless_silently_rejects_when_the_gate_says_no():
    from src import options_proposer as op
    from src import journal as journal_mod

    spread = {"strategy": "iron_condor", "lot_size": 35, "lots": 2,
              "expiry": "2026-07-16", "entry_spot": 52_000.0,
              "net_credit": 120.0, "net_debit": None, "spread_width": 200.0,
              "max_loss": 2_800.0, "max_profit": 4_200.0,
              "margin": {"total_margin": 2_800.0, "naked_margin": 90_000.0,
                         "offset_savings": 87_200.0},
              "legs": [
                  {"side": "SELL", "option_type": "PE", "strike": 51_000.0, "premium": 90.0},
                  {"side": "BUY", "option_type": "PE", "strike": 50_800.0, "premium": 55.0},
                  {"side": "SELL", "option_type": "CE", "strike": 53_000.0, "premium": 95.0},
                  {"side": "BUY", "option_type": "CE", "strike": 53_200.0, "premium": 60.0}]}
    canned = {"proposal": {"action": "SPREAD", "ticker": "NIFTY BANK",
                           "shares": 70, "price": 120.0, "signal": "test",
                           "spread": spread, "view": "neutral", "vix": 14.0,
                           "lots": 2},
              "view": "neutral", "vix": 14.0, "reason": "ok"}

    logged, notified = [], []
    saved = (op.build_proposal, journal_mod.log, op._notify_discord,
             op._memory_context_for, op._skeptic_note_for,
             pm.gate_headless_entry)
    try:
        op.build_proposal = lambda *a, **k: canned
        journal_mod.log = lambda e: logged.append(e)
        op._notify_discord = lambda text: notified.append(text) or True
        op._memory_context_for = lambda *a, **k: ""
        op._skeptic_note_for = lambda *a, **k: ""

        # gate says NO -> silent rejection: no journal line, no Discord
        pm.gate_headless_entry = lambda ref, margin, conn=None: (
            False, "margin exhaustion: test")
        result = op.run_headless("NIFTY BANK", {})
        assert result["proposed"] is False
        assert "margin exhaustion" in result["reason"]
        assert logged == [] and notified == []

        # gate says YES -> the normal flow resumes untouched
        seen = {}
        pm.gate_headless_entry = lambda ref, margin, conn=None: (
            seen.update(ref=ref, margin=margin) or True, "margin locked")
        result = op.run_headless("NIFTY BANK", {})
        assert result["proposed"] is True
        assert len(logged) == 1 and len(notified) == 1
        assert seen["margin"] == 5_600.0   # 2,800/lot SPAN x 2 lots
        assert seen["ref"] == logged[0]["short_id"]

        # an injected book is its own capital world — the account gate
        # must NOT be consulted (this is what keeps every other test and
        # the simulator from ever touching the real brain_map account)
        calls = []
        pm.gate_headless_entry = lambda ref, margin, conn=None: (
            calls.append(ref) or (False, "should never be called"))
        result = op.run_headless("NIFTY BANK",
                                 {"book": {"cash": 1.0, "holdings": {}}})
        assert result["proposed"] is True
        assert calls == []
    finally:
        (op.build_proposal, journal_mod.log, op._notify_discord,
         op._memory_context_for, op._skeptic_note_for,
         pm.gate_headless_entry) = saved


def test_pm_decide_pending_approval_is_margin_gated():
    from src import options_proposer as op
    from src import journal as journal_mod
    from src import notifier as notifier_mod

    def pending_entry():
        return {"short_id": "gate0001", "decision": "pending_approval",
                "ticker": "NIFTY BANK", "signal": "test", "outcome": None,
                "spread": {"strategy": "iron_condor", "lot_size": 35,
                           "lots": 3, "expiry": "2026-07-16",
                           "max_loss": 2_800.0, "max_profit": 4_200.0,
                           "margin": {"total_margin": 2_800.0}}}

    entries = [pending_entry()]
    rewrites, gate_calls = [], []
    saved = (journal_mod.read_all, journal_mod.rewrite_all,
             op._notify_discord, notifier_mod.fire_broadcast,
             pm.gate_headless_entry)
    try:
        journal_mod.read_all = lambda: entries
        journal_mod.rewrite_all = lambda e: rewrites.append(e)
        op._notify_discord = lambda text: True
        notifier_mod.fire_broadcast = lambda payload: None

        # margin refused -> the approval is blocked, nothing is written
        pm.gate_headless_entry = lambda ref, margin, conn=None: (
            gate_calls.append((ref, margin)) or (False, "margin exhaustion"))
        result = op.decide_pending("gate0001", approve=True, why="go")
        assert result["status"] == "margin_blocked"
        assert entries[0]["decision"] == "pending_approval"  # untouched
        assert rewrites == []
        assert gate_calls == [("gate0001", 8_400.0)]  # 2,800/lot x 3 lots

        # margin granted -> the normal approval flow resumes
        pm.gate_headless_entry = lambda ref, margin, conn=None: (True, "locked")
        result = op.decide_pending("gate0001", approve=True, why="go")
        assert result["status"] == "approved"
        assert entries[0]["decision"] == "approved"
        assert len(rewrites) == 1

        # rejections need no margin and must never consult the gate
        entries[0].update(decision="pending_approval")
        pm.gate_headless_entry = lambda ref, margin, conn=None: (
            (_ for _ in ()).throw(AssertionError("gate consulted on reject")))
        result = op.decide_pending("gate0001", approve=False, why="no")
        assert result["status"] == "rejected"
    finally:
        (journal_mod.read_all, journal_mod.rewrite_all,
         op._notify_discord, notifier_mod.fire_broadcast,
         pm.gate_headless_entry) = saved


if __name__ == "__main__":
    # Simple runner so you don't even need pytest installed.
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

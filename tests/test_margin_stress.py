"""
Department 3 — the 2026-07-19 SPAN work: entry-time VIX-stress margin,
the composed entry-halt list, the daily circuit breaker merged from
next_gen_engine (its four staging tests live here now), and the
report-only margin audit.

Hermetic: sqlite ':memory:' for the account layer, injected entries for
the audit, no network (the Discord card seam is monkeypatched).
"""
import sqlite3

import pytest

from src import margin_audit as MA
from src import portfolio_manager as pm
from src.portfolio import span_stress_factor


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    pm.ensure_schema(c)
    pm.get_account(c)
    yield c
    c.close()


def _proposal(total_margin=20_000.0, lots=1, vix=None):
    p = {"spread": {"margin": {"total_margin": total_margin}, "lots": lots}}
    if vix is not None:
        p["vix"] = vix
    return p


# ------------------------------------------------------- the stress factor

def test_stress_factor_bands_and_boundaries():
    assert span_stress_factor(12.0) == 1.0
    assert span_stress_factor(15.99) == 1.0
    assert span_stress_factor(16.0) == 1.15      # elevated band start
    assert span_stress_factor(24.99) == 1.15
    assert span_stress_factor(25.0) == 1.30      # panic band start
    assert span_stress_factor(40.0) == 1.30


def test_stress_factor_fails_toward_one_on_bad_input():
    assert span_stress_factor(None) == 1.0
    assert span_stress_factor("n/a") == 1.0


def test_required_margin_unstressed_when_calm_or_unknown():
    assert pm.required_margin_for(_proposal(vix=13.4)) == 20_000.0
    assert pm.required_margin_for(_proposal()) == 20_000.0      # no vix key


def test_required_margin_stressed_from_the_proposals_own_vix():
    assert pm.required_margin_for(_proposal(vix=27.0)) == 26_000.0
    assert pm.required_margin_for(_proposal(vix=18.0, lots=2)) == 46_000.0


def test_required_margin_explicit_vix_overrides_the_proposal():
    assert pm.required_margin_for(_proposal(vix=12.0), vix=30.0) == 26_000.0


# ------------------- the daily circuit breaker (ported from staging on merge)

def test_breaker_trips_at_the_daily_loss_limit():
    v = pm.check_daily_breaker(1_000_000, pnl_today=-30_000)  # exactly 3%
    assert v["halted"] is True
    assert v["daily_loss_pct"] == 3.0
    assert "TRIPPED" in v["reason"]


def test_breaker_stays_open_within_budget_and_on_profit():
    assert pm.check_daily_breaker(1_000_000, -29_999)["halted"] is False
    ok = pm.check_daily_breaker(1_000_000, +50_000)
    assert ok["halted"] is False and ok["daily_loss_pct"] == 0.0


def test_breaker_abstains_without_equity_but_says_so():
    v = pm.check_daily_breaker(0, -50_000)
    assert v["halted"] is False and "abstains" in v["error"]


def test_realized_pnl_counts_only_today():
    rows = [
        {"resolved_at": "2026-07-17T10:00:00+05:30", "pnl_net": -1000.0},
        {"closed_at": "2026-07-17T11:00:00+05:30", "pnl": -500.0},
        {"resolved_at": "2026-07-16T14:00:00+05:30", "pnl_net": -9999.0},
        {"resolved_at": "2026-07-17T12:00:00+05:30"},          # no pnl: skip
        {"pnl_net": -400.0},                                   # no stamp: skip
    ]
    assert pm.realized_pnl_today(rows, today="2026-07-17") == -1500.0


# ------------------------------------- the live gate + composed halt list

def _settle(conn, ref, pnl):
    assert pm.request_entry(conn, ref, 20_000.0)["approved"] is True
    assert pm.release_margin(conn, ref, pnl_net=pnl)["released"] is True


def test_a_three_pct_bleed_halts_new_entries_for_the_day(conn, monkeypatch):
    cards = []
    from src import notifier
    monkeypatch.setattr(notifier, "fire_broadcast", cards.append)

    _settle(conn, "loss-1", -30_050.0)          # 3.005% of session-open 10L
    status = pm.daily_breaker_status(conn)
    assert status["halted"] is True
    assert pm.daily_realized_pnl(conn) == -30_050.0

    verdict = pm.request_entry(conn, "next-entry", 10_000.0)
    assert verdict["approved"] is False
    assert "daily circuit breaker TRIPPED" in verdict["reason"]
    ev = conn.execute("SELECT COUNT(*) FROM account_events WHERE "
                      "event_type = 'daily_breaker_halt'").fetchone()[0]
    assert ev == 1
    # The rejection fired ONE review card; a second rejection de-dups.
    assert len(cards) == 1 and cards[0]["event"] == "daily_breaker"
    assert pm.request_entry(conn, "next-entry-2", 10_000.0)["approved"] is False
    assert len(cards) == 1


def test_a_small_loss_day_does_not_halt(conn):
    _settle(conn, "loss-small", -20_000.0)      # 2% — inside budget
    assert pm.daily_breaker_status(conn)["halted"] is False
    assert pm.request_entry(conn, "next", 10_000.0)["approved"] is True


def test_a_profitable_day_never_halts(conn):
    _settle(conn, "win-1", +50_000.0)
    assert pm.daily_breaker_status(conn)["halted"] is False


def test_risk_of_ruin_outranks_the_daily_breaker(conn):
    _settle(conn, "blowup", -110_000.0)         # 11% > both thresholds
    verdict = pm.request_entry(conn, "next", 10_000.0)
    assert verdict["approved"] is False
    assert "risk-of-ruin" in verdict["reason"]  # first in ENTRY_HALT_CHECKS


def test_stamps_and_breaker_share_the_ist_day(conn):
    # Issue-16 discipline: the released_at stamp the breaker reads back
    # must carry the IST date, whatever the host timezone says.
    _settle(conn, "stamp-check", -1_000.0)
    stamp = conn.execute("SELECT released_at FROM margin_locks WHERE "
                         "journal_ref = 'stamp-check'").fetchone()[0]
    assert stamp.startswith(pm.ist_today())
    assert pm.daily_realized_pnl(conn) == -1_000.0


# ------------------------------------------------------------- the audit

def _entry(short_id, total_margin, lots=1, vix=None, decision="approved",
           outcome=None, legs=None, lot_size=65):
    e = {"short_id": short_id, "decision": decision,
         "spread": {"strategy": "bear_put_spread", "lots": lots,
                    "lot_size": lot_size,
                    "margin": {"total_margin": total_margin},
                    "legs": legs or [
                        {"side": "BUY", "option_type": "PE",
                         "strike": 25000.0, "premium": 400.0},
                        {"side": "SELL", "option_type": "PE",
                         "strike": 24600.0, "premium": 200.0},
                    ]}}
    if vix is not None:
        e["receipt"] = {"vix": vix}
    if outcome:
        e["outcome"] = {"resolution": "closed"}
    return e


def test_audit_totals_stress_and_open_book(monkeypatch):
    entries = [
        _entry("open-1", 10_000.0, vix=13.0),
        _entry("open-2", 10_000.0, vix=27.0),
        _entry("done-1", 10_000.0, vix=13.0, outcome=True),
        _entry("novix-1", 10_000.0),
    ]
    # Pin recomputation to the recorded number: this test is about totals,
    # not the SPAN math (which test_portfolio covers).
    monkeypatch.setattr(MA, "calculate_span_margin",
                        lambda legs, lot: {"total_margin": 10_000.0})
    r = MA.audit(entries, pool=1_000_000.0)
    assert r["n_spreads"] == 4 and r["n_open"] == 3
    assert r["n_margin_drift"] == 0
    assert r["n_missing_entry_vix"] == 1
    assert r["n_entries_born_stressed"] == 1          # the vix-27 entry
    assert r["open_book_base_margin_rs"] == 30_000.0
    assert r["open_book_panic_margin_rs"] == 39_000.0
    assert r["n_squeezed_out_at_panic"] == 0
    assert MA.render(r)                               # renders without error


def test_audit_flags_margin_drift(monkeypatch):
    monkeypatch.setattr(MA, "calculate_span_margin",
                        lambda legs, lot: {"total_margin": 12_345.0})
    r = MA.audit([_entry("drifted", 10_000.0)], pool=1_000_000.0)
    assert r["n_margin_drift"] == 1
    assert r["rows"][0]["margin_drift"] is True


def test_audit_greedy_replay_squeezes_entries_out_of_a_small_pool(monkeypatch):
    monkeypatch.setattr(MA, "calculate_span_margin",
                        lambda legs, lot: {"total_margin": 10_000.0})
    # Base margins fit a 25k pool (10k + 10k); at panic x1.3 (13k each)
    # the second entry no longer fits.
    r = MA.audit([_entry("a", 10_000.0), _entry("b", 10_000.0)], pool=25_000.0)
    assert r["n_squeezed_out_at_panic"] == 1
    assert r["squeezed_out_ids"] == ["b"]


# ------------------------------------------------- capital injection (08-07)
# The architect raised the pool from decision #84's Rs.2,00,000 to
# Rs.10,00,000 after Friday's session refused entries for margin with
# Rs.2,957 liquid against Rs.2,36,466 locked. Before this the only door was
# hand-SQL, which leaves no trace of who moved what.

def test_injection_raises_the_base_and_the_liquid_cash(conn):
    pm.request_entry(conn, "t1", 50_000.0)
    before = pm.account_summary(conn)
    out = pm.inject_capital(800_000.0, why="architect: 2L -> 10L", conn=conn)
    after = out["after"]
    assert after["starting_capital"] == before["starting_capital"] + 800_000.0
    assert after["available_cash"] == before["available_cash"] + 800_000.0
    assert after["locked_margin"] == before["locked_margin"]   # locks intact


def test_an_injection_is_not_a_profit(conn):
    """Folding it into realized_pnl would corrupt every performance number
    computed off this row."""
    conn.execute("UPDATE account_state SET realized_pnl = 39423.99 WHERE id = 1")
    conn.commit()
    pm.inject_capital(800_000.0, conn=conn)
    assert pm.account_summary(conn)["realized_pnl"] == 39423.99


def test_the_peak_ratchets_so_the_ruin_halt_still_arms(conn):
    """A 2L-era peak under a 10L book would leave equity permanently above
    the peak — a drawdown halt that can never arm."""
    pm.inject_capital(800_000.0, conn=conn)
    after = pm.account_summary(conn)
    assert after["peak_equity"] >= after["equity"]
    assert after["drawdown_pct"] == 0.0


def test_every_injection_is_written_to_the_append_only_trail(conn):
    pm.inject_capital(800_000.0, why="architect order", conn=conn)
    rows = conn.execute("SELECT event_type, detail FROM account_events "
                        "WHERE event_type = 'capital_injection'").fetchall()
    assert len(rows) == 1
    assert "architect order" in rows[0][1] and "800,000.00" in rows[0][1]


def test_a_zero_injection_changes_nothing_and_logs_nothing(conn):
    out = pm.inject_capital(0, conn=conn)
    assert out["before"] == out["after"]
    assert conn.execute("SELECT COUNT(*) FROM account_events WHERE "
                        "event_type = 'capital_injection'").fetchone()[0] == 0


def test_a_withdrawal_is_the_same_door_in_reverse(conn):
    pm.inject_capital(800_000.0, conn=conn)
    pm.inject_capital(-300_000.0, why="trim", conn=conn)
    assert pm.account_summary(conn)["starting_capital"] == 1_500_000.0


# --- capital flow must not launder the drawdown (architect, 2026-08-17) --
# The 2026-08-07 ₹8L injection ratcheted peak_equity to the new equity and
# reported 0.00% drawdown to an account that was 1.96% down. Funds move in
# real life; the trading record must not move with them.

def _drawn_down_account(conn, start=1_000_000.0, loss=-100_000.0):
    """A book at its peak, then down `loss` — equity 9L under a 10L peak."""
    conn.execute("UPDATE account_state SET starting_capital = ?, "
                 "realized_pnl = 0, peak_equity = ? WHERE id = 1",
                 (start, start))
    conn.commit()
    pm.request_entry(conn, "dd1", 50_000.0)
    pm.release_margin(conn, "dd1", loss)
    return pm.account_summary(conn)


def test_a_ten_percent_drawdown_survives_a_large_infusion(conn):
    """THE REGRESSION: 10% down, then a 9x deposit. The old ratchet moved
    peak to the new equity and printed 0.00% — a losing book declared
    whole by a bank transfer."""
    before = _drawn_down_account(conn)
    assert before["drawdown_pct"] == pytest.approx(10.0)
    gap_before = before["peak_equity"] - before["equity"]

    after = pm.inject_capital(9_000_000.0, why="architect: scale up",
                              conn=conn)["after"]

    # The rupee distance from the high-water mark is EXACTLY preserved.
    assert after["peak_equity"] - after["equity"] == pytest.approx(gap_before)
    assert after["peak_equity"] == pytest.approx(10_000_000.0)
    assert after["equity"] == pytest.approx(9_900_000.0)
    # The loss is still on the record — never laundered to zero.
    assert after["drawdown_pct"] > 0.0
    assert after["realized_pnl"] == pytest.approx(-100_000.0)


def test_an_infusion_does_not_reset_the_high_water_mark(conn):
    """Directly the 2026-08-07 shape: down 1.96%, deposit, still down."""
    before = _drawn_down_account(conn, start=244_215.34, loss=-4_791.35)
    assert before["drawdown_pct"] > 0.0
    pm.inject_capital(800_000.0, conn=conn)
    after = pm.account_summary(conn)
    assert after["peak_equity"] == pytest.approx(1_044_215.34)
    assert after["drawdown_pct"] > 0.0


def test_a_modest_infusion_keeps_an_armed_ruin_halt_armed(conn):
    """A deposit that does not materially change the base must not clear
    the halt. Under the OLD ratchet any injection cleared it instantly."""
    _drawn_down_account(conn, loss=-150_000.0)      # 15% down, halted
    assert pm.trading_halted(conn) is True
    pm.inject_capital(50_000.0, why="rounding top-up", conn=conn)
    assert pm.trading_halted(conn) is True          # 150k / 1.05L = 14.3%


def test_a_recapitalisation_large_enough_DOES_clear_the_halt(conn):
    """Stated out loud rather than hidden: rupee distance is preserved,
    percent is not, so a big enough deposit dilutes the drawdown below the
    10% threshold and the halt disarms. Arithmetic, not an accident — and
    the Dept 3 ruling on whether a halt should survive its own
    recapitalisation is deliberately NOT made in this layer."""
    _drawn_down_account(conn)                       # 10% down, halted
    assert pm.trading_halted(conn) is True
    pm.inject_capital(9_000_000.0, why="scale up", conn=conn)
    assert pm.trading_halted(conn) is False
    assert pm.account_summary(conn)["drawdown_pct"] == pytest.approx(1.0)


def test_a_withdrawal_does_not_manufacture_a_drawdown(conn):
    """Taking money out is not a loss: peak falls by the same rupees."""
    conn.execute("UPDATE account_state SET starting_capital = 1000000.0, "
                 "realized_pnl = 0, peak_equity = 1000000.0 WHERE id = 1")
    conn.commit()
    pm.inject_capital(-300_000.0, why="owner withdrawal", conn=conn)
    after = pm.account_summary(conn)
    assert after["peak_equity"] == pytest.approx(700_000.0)
    assert after["equity"] == pytest.approx(700_000.0)
    assert after["drawdown_pct"] == 0.0


def test_peak_never_sits_below_equity_after_a_capital_move(conn):
    """The invariant guard: a peak under equity is a halt that can never
    arm, which is how the pre-fix docstring justified the ratchet."""
    conn.execute("UPDATE account_state SET starting_capital = 200000.0, "
                 "realized_pnl = 0, peak_equity = 100.0 WHERE id = 1")
    conn.commit()
    pm.inject_capital(800_000.0, conn=conn)
    after = pm.account_summary(conn)
    assert after["peak_equity"] >= after["equity"]

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

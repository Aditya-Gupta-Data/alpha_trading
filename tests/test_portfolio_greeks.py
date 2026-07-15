"""
Tests for src/portfolio_greeks.py — the portfolio-level Greeks advisory.

Offline: chains, spots, equity, and the notify sink are all injected; no
Dhan call, no journal write, no Discord. Ledger writes are redirected to
a temp path so no test touches logs/greeks_snapshots.jsonl.

    python tests/test_portfolio_greeks.py     (plain)
    python -m pytest tests/test_portfolio_greeks.py
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import portfolio_greeks as pg

IST = timezone(timedelta(hours=5, minutes=30))


def make_chain(spot, greeks_by_key):
    """A minimal chain: {strike: {ce/pe: {greeks:{...}}}} keyed Dhan-style,
    plus top-level last_price = spot. greeks_by_key maps (strike, 'ce'/'pe')
    -> {delta,theta,gamma,vega} (or omit a leg to make it un-priceable)."""
    oc = {}
    for (strike, side), g in greeks_by_key.items():
        node = oc.setdefault(f"{float(strike):.6f}", {})
        node[side] = {"greeks": g} if g is not None else {}
    return {"last_price": spot, "oc": oc}


def bull_call_spread(ticker="NIFTY 50", lot=65, lots=1, expiry="2026-08-25"):
    """A long bull call spread: BUY 25000 CE / SELL 25200 CE."""
    return {
        "trade_id": "abc123", "ticker": ticker, "expiry": expiry,
        "spread": {"strategy": "bull_call_spread", "lot_size": lot,
                   "lots": lots, "expiry": expiry,
                   "legs": [
                       {"side": "BUY", "option_type": "CE", "strike": 25000.0,
                        "premium": 120.0},
                       {"side": "SELL", "option_type": "CE", "strike": 25200.0,
                        "premium": 60.0}]}}


G_LONG = {"delta": 0.55, "theta": -14.0, "gamma": 0.0013, "vega": 12.0}
G_SHORT = {"delta": 0.30, "theta": -9.0, "gamma": 0.0009, "vega": 8.0}


def full_chain(spot=25100.0):
    return make_chain(spot, {
        (25000.0, "ce"): G_LONG,
        (25200.0, "ce"): G_SHORT})


# --------------------------------------------------------- leg / position

def test_leg_greeks_reads_nested_dhan_structure():
    g = pg.leg_greeks(full_chain(), 25000.0, "CE")
    assert g == G_LONG


def test_leg_greeks_none_when_any_greek_missing():
    ch = make_chain(25100.0, {(25000.0, "ce"): {"delta": 0.5, "theta": -1.0,
                                                "gamma": 0.001}})  # no vega
    assert pg.leg_greeks(ch, 25000.0, "CE") is None


def test_position_greeks_signs_buy_plus_sell_minus_and_scales():
    spread = bull_call_spread(lot=65, lots=1)["spread"]
    pgreeks = pg.position_greeks(spread, full_chain())
    # BUY(+) 0.55 - SELL(-) 0.30 = 0.25 per share; x65:
    assert pgreeks["delta"] == round((0.55 - 0.30) * 65, 4)
    # long vega 12 - short vega 8 = +4/share x65 -> net long vega
    assert pgreeks["vega"] == round((12.0 - 8.0) * 65, 4)


def test_position_greeks_abstains_if_a_leg_is_unpriceable():
    ch = make_chain(25100.0, {(25000.0, "ce"): G_LONG})  # SELL leg missing
    assert pg.position_greeks(bull_call_spread()["spread"], ch) is None


# ------------------------------------------------------------- aggregate

def test_aggregate_splits_priced_and_unpriced():
    priced = bull_call_spread()
    unpriced = bull_call_spread()
    unpriced["trade_id"] = "zzz999"
    chains = {("NIFTY 50", "2026-08-25"): full_chain()}
    # unpriced position points at an expiry with no chain
    unpriced["expiry"] = "2026-09-29"
    unpriced["spread"]["expiry"] = "2026-09-29"
    agg = pg.aggregate([priced, unpriced], chains, {"NIFTY 50": 25100.0})
    assert agg["priced_count"] == 1 and agg["unpriced_count"] == 1
    assert agg["unpriced_ids"] == ["zzz999"]
    assert agg["net_delta_notional"] == round((0.55 - 0.30) * 65 * 25100.0, 2)


def test_aggregate_delta_notional_is_additive_across_underlyings():
    n = bull_call_spread(ticker="NIFTY 50")
    b = bull_call_spread(ticker="NIFTY BANK", lot=30)
    b["trade_id"] = "bank01"
    chains = {("NIFTY 50", "2026-08-25"): full_chain(25100.0),
              ("NIFTY BANK", "2026-08-25"): full_chain(52000.0)}
    agg = pg.aggregate([n, b], chains,
                       {"NIFTY 50": 25100.0, "NIFTY BANK": 52000.0})
    expect = (0.25 * 65 * 25100.0) + (0.25 * 30 * 52000.0)
    assert agg["net_delta_notional"] == round(expect, 2)
    assert agg["priced_count"] == 2


# ------------------------------------------------------------- evaluate

def test_evaluate_flags_vega_breach():
    # net vega 4/share x65 = 260/IV-pt; +5pt shock = Rs.1,300 loss.
    agg = pg.aggregate([bull_call_spread()],
                       {("NIFTY 50", "2026-08-25"): full_chain()},
                       {"NIFTY 50": 25100.0})
    # equity so small that the 1,300 shock blows the 15% budget (195)
    v = pg.evaluate(agg, equity=1300.0, config={})
    assert v["verdict"] == "breach" and "vega" in v["breaches"]


def test_evaluate_ok_when_within_budget():
    agg = pg.aggregate([bull_call_spread()],
                       {("NIFTY 50", "2026-08-25"): full_chain()},
                       {"NIFTY 50": 25100.0})
    v = pg.evaluate(agg, equity=10_000_000.0, config={})
    assert v["verdict"] == "ok" and v["breaches"] == []


def test_evaluate_abstains_on_zero_equity_or_no_priced():
    agg = pg.aggregate([bull_call_spread()],
                       {("NIFTY 50", "2026-08-25"): full_chain()},
                       {"NIFTY 50": 25100.0})
    assert pg.evaluate(agg, equity=0.0)["verdict"] == "abstain"
    empty = pg.aggregate([], {}, {})
    assert pg.evaluate(empty, equity=1_000_000.0)["verdict"] == "abstain"


def test_delta_budget_respects_config_override():
    agg = pg.aggregate([bull_call_spread()],
                       {("NIFTY 50", "2026-08-25"): full_chain()},
                       {"NIFTY 50": 25100.0})
    # notional ~ 0.25*65*25100 = 407,875. Equity 1,000,000.
    # default delta budget 30% = 300,000 -> breach.
    assert "delta" in pg.evaluate(agg, 1_000_000.0, {})["breaches"]
    # loosen to 50% = 500,000 -> no delta breach.
    assert "delta" not in pg.evaluate(
        agg, 1_000_000.0, {"greeks_delta_budget_pct": 50.0})["breaches"]


# ------------------------------------------------- snapshot + dedup + card

def _redirect_ledger(tmp_path, monkeypatch):
    p = tmp_path / "greeks_snapshots.jsonl"
    monkeypatch.setattr(pg, "LEDGER_PATH", p)
    return p


def test_snapshot_writes_and_cards_once_per_day(tmp_path, monkeypatch):
    ledger = _redirect_ledger(tmp_path, monkeypatch)
    agg = pg.aggregate([bull_call_spread()],
                       {("NIFTY 50", "2026-08-25"): full_chain()},
                       {"NIFTY 50": 25100.0})
    verdict = pg.evaluate(agg, equity=1300.0, config={})  # vega breach
    sent = []
    now = lambda: datetime(2026, 7, 15, 12, 0, tzinfo=IST)

    r1 = pg.snapshot_and_notify(agg, verdict, notify_fn=sent.append, now_fn=now)
    # tiny equity blows both budgets; ONE card carries both breaches
    assert r1["carded"] == ["vega", "delta"] and len(sent) == 1
    # same day, same breaches -> snapshot again but NO second card
    r2 = pg.snapshot_and_notify(agg, verdict, notify_fn=sent.append, now_fn=now)
    assert r2["carded"] == [] and len(sent) == 1
    # two snapshot rows on disk though (history is every run)
    assert len(ledger.read_text().splitlines()) == 2


def test_new_day_re_cards(tmp_path, monkeypatch):
    _redirect_ledger(tmp_path, monkeypatch)
    agg = pg.aggregate([bull_call_spread()],
                       {("NIFTY 50", "2026-08-25"): full_chain()},
                       {"NIFTY 50": 25100.0})
    verdict = pg.evaluate(agg, equity=1300.0, config={})
    sent = []
    pg.snapshot_and_notify(agg, verdict, notify_fn=sent.append,
                           now_fn=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=IST))
    pg.snapshot_and_notify(agg, verdict, notify_fn=sent.append,
                           now_fn=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=IST))
    assert len(sent) == 2  # a fresh IST day re-cards


def test_card_is_honest_about_unpriced_coverage():
    priced = bull_call_spread()
    unpriced = bull_call_spread()
    unpriced["trade_id"] = "zzz999"
    unpriced["expiry"] = unpriced["spread"]["expiry"] = "2026-09-29"
    agg = pg.aggregate([priced, unpriced],
                       {("NIFTY 50", "2026-08-25"): full_chain()},
                       {"NIFTY 50": 25100.0})
    card = pg.build_card(agg, pg.evaluate(agg, 10_000_000.0, {}))
    assert "1 position(s) priced" in card and "un-priceable" in card


# ------------------------------------------------------ run_advisory seam

def test_run_advisory_honors_kill_switch():
    out = pg.run_advisory(config={"portfolio_greeks_advisory": False})
    assert out == {"skipped": "disabled in config"}


def test_run_advisory_end_to_end_injected(tmp_path, monkeypatch):
    _redirect_ledger(tmp_path, monkeypatch)
    entry = {"short_id": "abc123", "ticker": "NIFTY 50", "decision": "approved",
             "outcome": None, "date": "2026-07-10",
             "spread": bull_call_spread()["spread"]}
    sent = []
    out = pg.run_advisory(
        entries=[entry],
        chain_fn=lambda t, e: full_chain(),
        spot_fn=lambda t: 25100.0,
        equity=1300.0,                      # tiny -> vega breach
        notify_fn=sent.append,
        config={"portfolio_greeks_advisory": True},
        now_fn=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=IST))
    assert out["verdict"]["verdict"] == "breach"
    assert out["aggregate"]["priced_count"] == 1
    assert len(sent) == 1


def test_run_advisory_skips_when_no_open_spreads():
    out = pg.run_advisory(entries=[], config={"portfolio_greeks_advisory": True})
    assert out == {"skipped": "no open spreads"}


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(
        ["python", "-m", "pytest", __file__, "-q"]))

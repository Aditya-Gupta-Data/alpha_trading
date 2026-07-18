"""
Department 8 (Analysis) — the signal modules under the manager.

Companion to tests/test_regime_filters.py (which pins the manager seam and the
proposer contract). This file closes the rest of the review #2 coverage gap:
direct tests for the five signal/research modules' math — value-weighting,
strict point-in-time windows, NULL-honest abstention, factor weighting, and
threshold boundaries — plus the IST-clock fix in regime_filters._distribution
(date.today() on a UTC VM lags IST until 05:30; the decision day must come
from the shared IST clock).

Hermetic: no network; the only file I/O is a tmp_path ledger this file writes
itself.
"""
import json
from datetime import datetime, timedelta, timezone

from src.analysis import conviction as CV
from src.analysis import institutional_alpha as IA
from src.analysis import macro_shocks as MS
from src.analysis import regime_filters as RF
from src.analysis import sector_trend as ST
from src.analysis import smart_money_trend as SM

IST = timezone(timedelta(hours=5, minutes=30))


def _deal(as_of, side, value, qty=0, price=0.0, deal_type="bulk"):
    return {"as_of": as_of, "side": side, "value_rs": value,
            "qty": qty, "price": price, "deal_type": deal_type}


# ---------------------------------------------------------- smart_money_trend

def test_niv_is_value_weighted_net_of_buys_minus_sells():
    deals = [_deal("2026-01-05", "buy", 100.0),
             _deal("2026-01-06", "buy", 40.0),
             _deal("2026-01-07", "sell", 60.0)]
    niv = SM.net_institutional_volume(deals, "2026-02-01", 90)
    assert niv["n_deals"] == 3
    assert niv["net_value_rs"] == 80.0
    assert niv["accumulation"] is True


def test_niv_window_excludes_the_decision_day_itself():
    # STRICT point-in-time: a deal dated the decision day discloses post-close
    # and must NOT be known; the day before must be.
    deals = [_deal("2026-02-01", "sell", 500.0),
             _deal("2026-01-31", "buy", 10.0)]
    niv = SM.net_institutional_volume(deals, "2026-02-01", 90)
    assert niv["n_deals"] == 1
    assert niv["net_value_rs"] == 10.0


def test_niv_abstains_with_none_on_an_empty_window():
    niv = SM.net_institutional_volume([], "2026-02-01", 90)
    assert niv["n_deals"] == 0 and niv["accumulation"] is None


def test_block_vwap_uses_block_buys_only():
    deals = [_deal("2026-01-05", "buy", 0, qty=100, price=50.0, deal_type="block"),
             _deal("2026-01-06", "buy", 0, qty=100, price=70.0, deal_type="block"),
             _deal("2026-01-07", "buy", 0, qty=1000, price=999.0, deal_type="bulk"),
             _deal("2026-01-08", "sell", 0, qty=1000, price=999.0, deal_type="block")]
    vw = SM.block_deal_vwap(deals, "2026-02-01")
    assert vw["n_deals"] == 2          # bulk buy and block SELL both excluded
    assert vw["vwap"] == 60.0          # (100*50 + 100*70) / 200


def test_block_vwap_is_none_with_no_qualifying_deals():
    assert SM.block_deal_vwap([], "2026-02-01")["vwap"] is None


def test_smart_money_ok_honest_abstain_without_deals():
    v = SM.smart_money_ok([], "2026-02-01", current_price=100.0)
    assert v["smart_money_ok"] is None


def test_smart_money_ok_confirms_on_accumulation_or_vwap_floor():
    accum = [_deal("2026-01-05", "buy", 100.0)]
    assert SM.smart_money_ok(accum, "2026-02-01", 100.0)["smart_money_ok"] is True
    # Distribution but price above the block-VWAP floor still confirms.
    floor = [_deal("2026-01-05", "sell", 100.0),
             _deal("2026-01-06", "buy", 10.0, qty=10, price=90.0,
                   deal_type="block")]
    v = SM.smart_money_ok(floor, "2026-02-01", current_price=95.0)
    assert v["above_block_vwap"] is True and v["smart_money_ok"] is True


def test_load_deals_by_ticker_groups_sorts_and_skips_junk(tmp_path):
    ledger = tmp_path / "deals.jsonl"
    rows = [
        {"ticker": "AAA.NS", "as_of": "2026-01-07", "value_rs": 2.0},
        {"ticker": "AAA.NS", "as_of": "2026-01-05", "value_rs": 1.0},
        {"ticker": "BBB.NS", "as_of": "2026-01-06", "value_rs": 3.0},
        {"ticker": "AAA.NS", "as_of": "2026-01-06"},          # no value_rs
        {"as_of": "2026-01-06", "value_rs": 9.0},             # no ticker
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows))
    by = SM.load_deals_by_ticker(path=ledger)
    assert set(by) == {"AAA.NS", "BBB.NS"}
    assert [d["as_of"] for d in by["AAA.NS"]] == ["2026-01-05", "2026-01-07"]


# --------------------------------------------------------------- sector_trend

def _bars(closes):
    return [(f"day-{i}", 0.0, 0.0, c) for i, c in enumerate(closes)]


def test_sector_bullish_above_both_smas():
    v = ST.is_sector_bullish("IT", index_bars=_bars(range(1, 252)), universe={})
    assert v["bullish"] is True
    assert v["above_sma50"] is True and v["above_sma200"] is True


def test_sector_not_bullish_below_the_smas():
    v = ST.is_sector_bullish("IT", index_bars=_bars(range(251, 0, -1)),
                             universe={})
    assert v["bullish"] is False


def test_sector_verdict_is_null_honest_on_short_history():
    v = ST.is_sector_bullish("IT", index_bars=_bars(range(1, 100)), universe={})
    assert v["bullish"] is None
    assert "insufficient index history" in v["error"]


def test_relative_strength_leader_and_laggard():
    flat = list(range(100, 164))                     # +63% over the lookback
    hot = [c * 2 for c in flat]                      # same %, doubled level
    surge = flat[:-1] + [flat[-1] * 2]               # last bar doubles: leader
    v = ST.get_relative_strength("X.NS", "IT", stock_bars=_bars(surge),
                                 index_bars=_bars(hot), universe={})
    assert v["leader"] is True
    v = ST.get_relative_strength("X.NS", "IT", stock_bars=_bars(flat),
                                 index_bars=_bars(surge), universe={})
    assert v["leader"] is False


def test_relative_strength_errors_without_stock_bars():
    v = ST.get_relative_strength("X.NS", "IT", stock_bars=None,
                                 index_bars=_bars(range(100)), universe={})
    assert v["leader"] is None and "no stock_bars" in v["error"]


def test_relative_strength_errors_on_short_history():
    v = ST.get_relative_strength("X.NS", "IT", stock_bars=_bars([1, 2, 3]),
                                 index_bars=_bars(range(100, 164)), universe={})
    assert v["leader"] is None and "insufficient history" in v["error"]


# --------------------------------------------------------------- macro_shocks

def test_active_shock_window_boundaries_inclusive():
    assert MS.active_shock("2020-02-20") == "2020_COVID_crash"   # start day
    assert MS.active_shock("2020-04-07") == "2020_COVID_crash"   # end day
    assert MS.active_shock("2020-04-08") is None                 # day after
    assert MS.active_shock("2022-03-01") == "2022_Russia_Ukraine"
    assert MS.active_shock("2021-06-15") is None


# ----------------------------------------------------------------- conviction

def test_fundamental_factor_pristine_scores_full_marks():
    # 25% ROE, zero leverage, CFO covering PAT -> every sub-score maxed.
    assert CV.fundamental_factor(0.25, 0.0, 120.0, 100.0) == 1.0


def test_fundamental_factor_value_trap_is_crushed():
    # Decent ROE but negative cash flow: score * 0.25.
    trapped = CV.fundamental_factor(0.15, 0.5, -10.0, 50.0)
    untrapped = CV.fundamental_factor(0.15, 0.5, 50.0, 50.0)
    assert trapped < 0.15 < untrapped


def test_fundamental_factor_neutral_when_roe_missing():
    assert CV.fundamental_factor(None, 0.0, 100.0, 100.0) == 0.5


def test_smart_money_factor_neutral_on_no_deals():
    assert CV.smart_money_factor([], "2026-02-01") == 0.5


def test_sector_factor_half_credit_per_leg():
    assert CV.sector_factor(True, True) == 1.0
    assert CV.sector_factor(True, False) == 0.5
    assert CV.sector_factor(False, False) == 0.0


def test_conviction_score_weighting_40_40_20():
    assert CV.conviction_score(1.0, 0.0, 0.0) == 0.40
    assert CV.conviction_score(0.0, 1.0, 0.0) == 0.40
    assert CV.conviction_score(0.0, 0.0, 1.0) == 0.20
    assert CV.conviction_score(1.0, 1.0, 1.0) == 1.0


def test_score_from_inputs_veto_boundary_at_0_40():
    # All-neutral inputs: sm 0.5, fundamentals 0.5, sector 0.0 -> exactly 0.40,
    # which is NOT a veto (the rule is strictly below).
    at_line = CV.score_from_inputs([], "2026-02-01", (None, None, None, None),
                                   is_top3=False, sector_outperforms=False)
    assert at_line["conviction"] == 0.40 and at_line["veto"] is False
    # A cash-negative value trap drags it under the line -> veto.
    trap = CV.score_from_inputs([], "2026-02-01", (0.10, 1.5, -5.0, 10.0),
                                is_top3=False, sector_outperforms=False)
    assert trap["conviction"] < 0.40 and trap["veto"] is True


# --------------------------------------------------------- institutional_alpha

def test_accumulation_needs_two_buys_and_dominant_net():
    two = [_deal("2026-01-05", "buy", 5000.0, qty=100, price=50.0),
           _deal("2026-01-06", "buy", 5000.0, qty=100, price=50.0)]
    a = IA.accumulation(two, "2026-02-01")
    assert a["accumulating"] is True and a["vwap"] == 50.0


def test_accumulation_rejects_a_single_print():
    one = [_deal("2026-01-05", "buy", 5000.0, qty=100, price=50.0)]
    assert IA.accumulation(one, "2026-02-01")["accumulating"] is False


def test_accumulation_rejects_balanced_churn_below_net_ratio():
    # net/gross = 1000/21000 < the 0.20 MIN_NET_RATIO: churn, not accumulation.
    churn = [_deal("2026-01-05", "buy", 6000.0, qty=100, price=60.0),
             _deal("2026-01-06", "buy", 5000.0, qty=100, price=50.0),
             _deal("2026-01-07", "sell", 10000.0)]
    assert IA.accumulation(churn, "2026-02-01")["accumulating"] is False


def test_accumulation_window_excludes_the_decision_day():
    deals = [_deal("2026-02-01", "buy", 5000.0, qty=100, price=50.0),
             _deal("2026-01-31", "buy", 5000.0, qty=100, price=50.0)]
    a = IA.accumulation(deals, "2026-02-01")
    assert a["n_buy_deals"] == 1        # the as_of-day print is unknown


def test_pullback_trigger_holds_at_the_defense_line():
    # From above (105 > 100), dips to the VWAP (99 <= 100), close holds (>= 96).
    assert IA.pullback_trigger(105.0, 99.0, 97.0, 100.0) is True


def test_pullback_trigger_fails_below_the_invalidation_band():
    assert IA.pullback_trigger(105.0, 99.0, 95.0, 100.0) is False


def test_pullback_trigger_needs_the_approach_from_above():
    assert IA.pullback_trigger(98.0, 97.0, 99.0, 100.0) is False


def test_pullback_trigger_false_without_a_vwap():
    assert IA.pullback_trigger(105.0, 99.0, 97.0, None) is False


# --------------------------------- regime_filters: the IST decision-day fix

def test_distribution_defaults_to_the_ist_date_not_host_tz(monkeypatch):
    # 01:00 IST on the 19th == 19:30 UTC on the 18th. A UTC host's
    # date.today() would say the 18th and shift the whole window; the IST
    # clock must say the 19th, making an 18th-dated deal visible.
    from src import market_loop
    monkeypatch.setattr(market_loop, "ist_now",
                        lambda: datetime(2026, 7, 19, 1, 0, tzinfo=IST))
    deals = {"HDFCBANK.NS": [_deal("2026-07-18", "sell", 5e7)],
             "ICICIBANK.NS": [_deal("2026-07-18", "sell", 3e7)]}
    hit, why = RF._distribution("NIFTY BANK", deals)
    assert hit is True and "2/3" in why


def test_distribution_honors_an_explicit_as_of():
    deals = {"HDFCBANK.NS": [_deal("2025-12-31", "sell", 5e7)],
             "ICICIBANK.NS": [_deal("2025-12-31", "sell", 3e7)]}
    assert RF._distribution("NIFTY BANK", deals, as_of="2026-01-01")[0] is True
    # Strictly-before: the deals aren't known on their own disclosure day.
    assert RF._distribution("NIFTY BANK", deals, as_of="2025-12-31")[0] is False


def test_advise_threads_as_of_into_the_distribution_radar():
    deals = {"HDFCBANK.NS": [_deal("2025-12-31", "sell", 5e7)],
             "ICICIBANK.NS": [_deal("2025-12-31", "sell", 3e7)]}
    v = RF.advise("NIFTY BANK", vix=14.0, as_of="2026-01-01",
                  deals_by_ticker=deals)
    assert v["block_bullish"] is True

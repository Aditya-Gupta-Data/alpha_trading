"""
Department 8 (Analysis) — the signal modules under the manager.

Companion to tests/test_regime_filters.py (which pins the manager seam and the
proposer contract). Direct tests for the live signal modules' math —
value-weighting, strict point-in-time windows, NULL-honest abstention, and
threshold boundaries — plus the IST-clock fix in regime_filters._distribution
(conviction/institutional_alpha sections moved to research_archive/tests/
with their modules, Phase-1 cleanup 2026-07-25)
(date.today() on a UTC VM lags IST until 05:30; the decision day must come
from the shared IST clock).

Hermetic: no network; the only file I/O is a tmp_path ledger this file writes
itself.
"""
import json
from datetime import datetime, timedelta, timezone

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

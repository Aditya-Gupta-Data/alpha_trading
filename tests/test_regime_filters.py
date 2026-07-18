"""
Department 8 (Analysis) — regime_filters, the manager seam.

Review #2 finding (2026-07-18): the advisory veto/crisis logic is LIVE on the
VM (composed in market_loop.fetch_market_state, honored in
options_proposer.build_proposal via advisory=) but had ZERO dedicated tests —
the 56 options tests only proved the fail-open path (advisory absent =
proposer unchanged). This file pins BOTH sides:

  * the radars themselves — the >=2-of-top-3 distribution veto, the sector
    veto, and every crisis trigger (VIX panic / d-o-d spike / macro-shock
    window), each with its fail-open behavior;
  * the proposer contract — a veto blocks ONLY a bullish view, crisis blocks
    ONLY the short-premium (neutral) view, and a permissive verdict leaves
    build_proposal byte-identical to no verdict at all;
  * the market_loop composition — an exploding deals ledger costs the cycle
    its advisory, never the cycle.

Hermetic: no network, no files, no clocks beyond date.today() (which
_distribution uses internally — deal fixtures are dated relative to it).
"""
from datetime import date, timedelta

from src.analysis import regime_filters as RF


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _deal(side: str, value: float, days_ago: int = 5) -> dict:
    return {"as_of": _days_ago(days_ago), "side": side, "value_rs": value}


PERMISSIVE = {"block_bullish": False, "crisis": False}


# ---------------------------------------------------------------- crisis_regime

def test_crisis_on_vix_panic_level():
    v = RF.crisis_regime(vix=25.0)
    assert v["crisis"] is True
    assert "panic" in v["reason"]


def test_crisis_on_abrupt_dod_vix_spike():
    # 20 / 17 - 1 = 17.6% >= the 15% spike threshold
    v = RF.crisis_regime(vix=20.0, prev_vix=17.0)
    assert v["crisis"] is True
    assert "spiked" in v["reason"]


def test_no_crisis_just_below_both_thresholds():
    v = RF.crisis_regime(vix=24.9, prev_vix=22.0)  # +13.2% < 15%
    assert v["crisis"] is False
    assert v["reason"] == "calm"


def test_crisis_on_known_macro_shock_window():
    v = RF.crisis_regime(vix=12.0, as_of="2020-03-15")  # COVID crash window
    assert v["crisis"] is True
    assert "shock window" in v["reason"]


def test_calm_date_outside_every_shock_window():
    assert RF.crisis_regime(vix=12.0, as_of="2021-06-15")["crisis"] is False


def test_crisis_fails_open_on_missing_vix():
    assert RF.crisis_regime(vix=None)["crisis"] is False


def test_crisis_fails_open_on_zero_prev_vix():
    # prev_vix=0 must not divide-by-zero its way into a verdict
    assert RF.crisis_regime(vix=20.0, prev_vix=0.0)["crisis"] is False


# --------------------------------------------------------- the distribution veto

def test_two_of_three_heavyweights_distributing_vetoes():
    deals = {
        "HDFCBANK.NS": [_deal("sell", 5e7), _deal("buy", 1e7)],   # net sell
        "ICICIBANK.NS": [_deal("sell", 3e7)],                     # net sell
        "SBIN.NS": [_deal("buy", 2e7)],                           # net buy
    }
    hit, why = RF._distribution("NIFTY BANK", deals)
    assert hit is True
    assert "2/3" in why


def test_one_distributing_heavyweight_is_not_enough():
    deals = {
        "HDFCBANK.NS": [_deal("sell", 5e7)],
        "ICICIBANK.NS": [_deal("buy", 3e7)],
        "SBIN.NS": [_deal("buy", 2e7)],
    }
    hit, _ = RF._distribution("NIFTY BANK", deals)
    assert hit is False


def test_distribution_needs_deals_inside_the_90d_window():
    # Heavy selling, but all of it older than the 90-day lookback.
    deals = {
        "HDFCBANK.NS": [_deal("sell", 5e7, days_ago=120)],
        "ICICIBANK.NS": [_deal("sell", 3e7, days_ago=120)],
    }
    hit, _ = RF._distribution("NIFTY BANK", deals)
    assert hit is False


def test_distribution_fails_open_without_deals_or_mapping():
    assert RF._distribution("NIFTY BANK", None)[0] is False
    assert RF._distribution("NIFTY BANK", {})[0] is False
    assert RF._distribution("UNKNOWN INDEX", {"X": [_deal("sell", 1e7)]})[0] is False


# ------------------------------------------------------------- the sector veto

def test_sector_bearish_when_parent_sector_below_smas(monkeypatch):
    from src.analysis import sector_trend
    monkeypatch.setattr(sector_trend, "is_sector_bullish",
                        lambda s: {"bullish": False})
    hit, why = RF._sector_bearish("NIFTY BANK")
    assert hit is True
    assert "FINANCIALS" in why


def test_sector_ok_when_parent_sector_bullish(monkeypatch):
    from src.analysis import sector_trend
    monkeypatch.setattr(sector_trend, "is_sector_bullish",
                        lambda s: {"bullish": True})
    assert RF._sector_bearish("NIFTY BANK")[0] is False


def test_sector_fails_open_on_missing_data(monkeypatch):
    from src.analysis import sector_trend

    def boom(s):
        raise FileNotFoundError("no sector bars on this box")
    monkeypatch.setattr(sector_trend, "is_sector_bullish", boom)
    assert RF._sector_bearish("NIFTY BANK")[0] is False


def test_nifty50_has_no_sector_mapping_by_design():
    assert RF._sector_bearish("NIFTY 50")[0] is False


# ------------------------------------------------------------------- advise()

def test_advise_verdict_shape_and_permissive_calm_path():
    v = RF.advise("NIFTY 50", vix=14.0, deals_by_ticker={})
    assert set(v) == {"block_bullish", "bullish_reason", "crisis", "crisis_reason"}
    assert v["block_bullish"] is False and v["crisis"] is False


def test_advise_blocks_bullish_on_distribution():
    deals = {"HDFCBANK.NS": [_deal("sell", 5e7)],
             "ICICIBANK.NS": [_deal("sell", 3e7)]}
    v = RF.advise("NIFTY BANK", vix=14.0, deals_by_ticker=deals)
    assert v["block_bullish"] is True
    assert "distributing" in v["bullish_reason"]


def test_advise_fails_open_to_permissive_when_a_radar_explodes(monkeypatch):
    def boom(underlying, deals):
        raise RuntimeError("radar meltdown")
    monkeypatch.setattr(RF, "_distribution", boom)
    v = RF.advise("NIFTY BANK", vix=30.0, deals_by_ticker={})
    assert v["block_bullish"] is False and v["crisis"] is False
    assert "failed-open" in v["bullish_reason"]


# ----------------------------------------- the proposer contract (build_proposal)

BULLISH = {"uptrend": True, "fresh_cross": True, "rsi": 50.0, "price": 100.0}
NEUTRAL = {"uptrend": True, "fresh_cross": False, "rsi": 50.0, "price": 100.0}
BEARISH = {"uptrend": False, "fresh_cross": False, "rsi": 50.0, "price": 100.0}


def _build(analysis, advisory):
    """build_proposal with every network seam injected: an empty chain stops
    the pipeline right AFTER the advisory gate, so tests never fetch."""
    from src.options_proposer import build_proposal
    return build_proposal("NIFTY 50", analysis=analysis, vix=15.0,
                          expiry="2099-12-31", chain={}, advisory=advisory)


def test_veto_blocks_a_bullish_proposal_with_the_advisory_reason():
    r = _build(BULLISH, {"block_bullish": True,
                         "bullish_reason": "smart-money/sector veto: test",
                         "crisis": False})
    assert r["proposal"] is None
    assert r["reason"] == "smart-money/sector veto: test"


def test_crisis_disables_the_short_premium_neutral_structure():
    r = _build(NEUTRAL, {"block_bullish": False, "crisis": True,
                         "crisis_reason": "VIX 30.0 >= panic 25.0"})
    assert r["proposal"] is None
    assert "war playbook" in r["reason"]
    assert "VIX 30.0" in r["reason"]


def test_veto_does_not_touch_a_bearish_view():
    r = _build(BEARISH, {"block_bullish": True, "crisis": False})
    # Passed the advisory gate; stopped later by the injected empty chain.
    assert r["reason"] == "option chain unavailable"


def test_crisis_does_not_touch_the_long_premium_bullish_view():
    r = _build(BULLISH, {"block_bullish": False, "crisis": True})
    assert r["reason"] == "option chain unavailable"


def test_permissive_advisory_is_byte_identical_to_no_advisory():
    assert _build(BULLISH, dict(PERMISSIVE)) == _build(BULLISH, None)
    assert _build(NEUTRAL, dict(PERMISSIVE)) == _build(NEUTRAL, None)


# ------------------------------------- market_loop composition (fail-open cycle)

def _patch_loop_seams(monkeypatch):
    from src import market_loop, vol_bridge
    monkeypatch.setattr(market_loop, "analyze", lambda u: dict(NEUTRAL))
    monkeypatch.setattr(market_loop, "get_india_vix", lambda: 15.0)
    monkeypatch.setattr(vol_bridge, "compute_regime_overrides", lambda: {})


def test_fetch_market_state_composes_the_advisory(monkeypatch):
    from src import market_loop
    from src.analysis import smart_money_trend
    _patch_loop_seams(monkeypatch)
    monkeypatch.setattr(smart_money_trend, "load_deals_by_ticker", lambda: {})
    state = market_loop.fetch_market_state("NIFTY 50")
    assert state["advisory"]["block_bullish"] is False
    assert state["advisory"]["crisis"] is False


def test_unreadable_deals_ledger_costs_the_advisory_not_the_cycle(monkeypatch):
    from src import market_loop
    from src.analysis import smart_money_trend
    _patch_loop_seams(monkeypatch)

    def boom():
        raise OSError("deals ledger unreadable")
    monkeypatch.setattr(smart_money_trend, "load_deals_by_ticker", boom)
    state = market_loop.fetch_market_state("NIFTY 50")
    assert state is not None and "advisory" not in state
    assert state["analysis"] == NEUTRAL and state["vix"] == 15.0

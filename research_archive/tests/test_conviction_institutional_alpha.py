"""
ARCHIVED with research_archive/analysis/{conviction,institutional_alpha}.py
(Phase-1 cleanup, 2026-07-25). Extracted verbatim from
tests/test_analysis_signals.py. These imports point at the OLD src paths and
are kept as frozen reference alongside the archived modules — they are NOT
collected by pytest (testpaths = tests).
"""
from datetime import datetime, timedelta, timezone

from src.analysis import conviction as CV                 # noqa: archived path
from src.analysis import institutional_alpha as IA        # noqa: archived path

IST = timezone(timedelta(hours=5, minutes=30))


def _deal(as_of, side, value, qty=0, price=0.0, deal_type="bulk"):
    return {"as_of": as_of, "side": side, "value_rs": value,
            "qty": qty, "price": price, "deal_type": deal_type}

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


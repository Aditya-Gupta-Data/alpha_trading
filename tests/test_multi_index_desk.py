"""
The 3-dimensional options desk (2026-08-05): multi-index universe, equity
options activated, macro-trend underlying router, and signal-matched time
horizons.

Every constant asserted here was VERIFIED against Dhan's
api-scrip-master-detailed.csv on 2026-08-05 (202,948 rows) — ids, lot
sizes and the actual expiry ladders. Two lot sizes were WRONG before that
check and are pinned here so they cannot silently regress.

Hermetic: injected clocks, injected scorers, no network, no DB.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import market_loop as ml
from src import options_proposer as op
from src.analysis import underlying_router as R
from src.dhan_client import SECURITY_ID_MAP, _ALIASES

TODAY = date(2026, 8, 5)


# ============================================ 1. multi-index universe

def test_the_two_new_index_ids_are_the_verified_ones():
    """27 / 442 read off the scrip master (NSE, segment I, INSTRUMENT=INDEX)
    and cross-checked against the UNDERLYING_SECURITY_ID their OPTIDX rows
    point at (26037 / 26074). A guessed id prices the WRONG instrument."""
    assert SECURITY_ID_MAP["NIFTY FIN SERVICE"] == {
        "id": "27", "seg": "IDX_I", "inst": "INDEX"}
    assert SECURITY_ID_MAP["NIFTY MID SELECT"] == {
        "id": "442", "seg": "IDX_I", "inst": "INDEX"}


def test_the_trading_aliases_resolve():
    assert _ALIASES["FINNIFTY"] == "NIFTY FIN SERVICE"
    assert _ALIASES["MIDCPNIFTY"] == "NIFTY MID SELECT"


def test_index_lot_sizes_match_the_scrip_master():
    """Uniform across every live OPTIDX contract on 2026-08-05:
    NIFTY 65 (4,008 rows), BANKNIFTY 30 (2,358), FINNIFTY 60 (1,084),
    MIDCPNIFTY 120 (1,510)."""
    assert op.LOT_SIZES == {"NIFTY 50": 65, "NIFTY BANK": 30,
                            "NIFTY FIN SERVICE": 60,
                            "NIFTY MID SELECT": 120}


def test_the_universe_is_four_indices_plus_five_stocks():
    assert ml.INDEX_UNDERLYINGS == ("NIFTY 50", "NIFTY BANK",
                                    "NIFTY FIN SERVICE", "NIFTY MID SELECT")
    assert len(ml.UNDERLYINGS) == 9
    assert set(op.EQUITY_OPTION_UNDERLYINGS) <= set(ml.UNDERLYINGS)


def test_every_underlying_in_the_live_universe_is_priceable():
    """A name the desk scans but cannot quote is a silent dead cycle."""
    for u in ml.UNDERLYINGS:
        assert u in SECURITY_ID_MAP, u


# ============================================ 2. equity options activated

def test_the_corrected_lot_sizes_are_pinned():
    """VERIFIED 2026-08-05; TWO of the five were wrong and are the reason
    this test exists. HDFCBANK 550->650 (316 contracts, all lot 650) and
    TCS 175->225 (442 contracts, all lot 225)."""
    assert op.EQUITY_OPTION_UNDERLYINGS == {
        "RELIANCE.NS": 500, "HDFCBANK.NS": 650, "ICICIBANK.NS": 700,
        "INFY.NS": 400, "TCS.NS": 225}


def test_activation_did_not_weaken_the_settlement_guard():
    """The guard was explicitly out of scope for this change; prove it."""
    assert op.EQUITY_MIN_DAYS_TO_EXPIRY == 7
    assert op.EQUITY_FORCED_EXIT_DAYS == 7
    ok, why = op.physical_settlement_gate("RELIANCE.NS", "2026-08-08",
                                          today=TODAY)
    assert ok is False and "PHYSICAL SETTLEMENT GATE" in why
    assert op.physical_settlement_gate("NIFTY MID SELECT", "2026-08-06",
                                       today=TODAY) == (True, None)


# ============================================ 3. macro-trend router

def _rs(mapping):
    return lambda u, sector: {"rs_spread_pct": mapping.get(u)}


def test_an_outperforming_midcap_is_prioritised():
    """The brief's own example: if midcaps are outperforming, MIDCPNIFTY
    should get looked at first."""
    order = R.prioritise(
        ["NIFTY 50", "NIFTY BANK", "NIFTY MID SELECT"],
        rs_fn=_rs({"NIFTY MID SELECT": 4.0, "NIFTY BANK": 0.2}),
        macro_fn=lambda u: None)
    assert order[0] == "NIFTY MID SELECT"


def test_the_router_NEVER_drops_an_underlying():
    """It is a prioritiser, not a gate. Only Risk may block (#63), and a
    router that silently shortened the universe would be taking that
    authority by accident."""
    universe = list(ml.UNDERLYINGS)
    order = R.prioritise(universe, rs_fn=_rs({"NIFTY BANK": 9.0}),
                         macro_fn=lambda u: None)
    assert sorted(order) == sorted(universe)
    assert len(order) == 9


def test_a_flat_signal_reproduces_todays_order_exactly():
    """No signal must mean no change — the sort is stable, so an absent
    reading can never shuffle the universe."""
    universe = list(ml.UNDERLYINGS)
    order = R.prioritise(universe, rs_fn=lambda u, s: {"rs_spread_pct": None},
                         macro_fn=lambda u: None)
    assert list(order) == universe


def test_a_strongly_NEGATIVE_macro_read_ranks_as_high_as_a_positive_one():
    """The desk trades both directions. Ranking on the SIGNED score would
    quietly bias the book long without anyone deciding to."""
    up = R.score_underlying("A", rs_fn=lambda u, s: {"rs_spread_pct": 0.0},
                            macro_fn=lambda u: 4.0)
    down = R.score_underlying("B", rs_fn=lambda u, s: {"rs_spread_pct": 0.0},
                              macro_fn=lambda u: -4.0)
    assert up["rank"] == down["rank"] > 0


def test_absent_macro_is_None_not_a_fabricated_zero():
    """'No macro read' and 'neutral macro read' must stay distinguishable."""
    s = R.score_underlying("X", rs_fn=lambda u, sec: {"rs_spread_pct": 0.0},
                           macro_fn=lambda u: None)
    assert s["macro"] is None and s["rank"] == 0.0


def test_momentum_saturates_so_one_violent_day_cannot_monopolise():
    hot = R.score_underlying("NIFTY BANK",
                             rs_fn=_rs({"NIFTY BANK": 500.0}),
                             macro_fn=lambda u: None)
    assert hot["momentum"] == 1.0


def test_the_benchmark_has_nothing_to_outperform():
    """NIFTY 50 IS the benchmark — no parent sector, so a 0 momentum leg
    is correct rather than a missing reading."""
    assert R.momentum_score("NIFTY 50") == 0.0


def test_the_router_fails_open_to_the_input_order():
    def boom(u, sector):
        raise RuntimeError("sector data down")
    universe = ["NIFTY 50", "NIFTY BANK"]
    assert list(R.prioritise(universe, rs_fn=boom,
                             macro_fn=lambda u: None)) == universe

    def boom_macro(u):
        raise RuntimeError("brain map down")
    assert list(R.prioritise(universe, rs_fn=_rs({}),
                             macro_fn=boom_macro)) == universe


def test_the_router_reads_the_dual_horizon_column(tmp_path):
    """End to end against a real brain_map: the macro leg consumes the
    long_term_macro_score the Level-1 work started storing."""
    from src import brain_map
    conn = brain_map.connect(str(tmp_path / "b.db"))
    brain_map.ingest_existing(conn, journal_entries=[], news={
        "generated": "2026-08-05",
        "tickers": {"RELIANCE.NS": {"sentiment_score": 1,
                                    "short_term_catalyst_score": 1,
                                    "long_term_macro_score": -4,
                                    "headline_focus": "f"}}})
    assert R.macro_score("RELIANCE.NS", conn=conn) == -4.0
    assert R.macro_score("NOBODY.NS", conn=conn) is None
    conn.close()


def test_render_line_names_the_reason_not_just_the_order():
    line = R.render_line(["NIFTY MID SELECT", "NIFTY 50"],
                         rs_fn=_rs({"NIFTY MID SELECT": 3.0}),
                         macro_fn=lambda u: None)
    assert line.startswith("underlying router:")
    assert "rank" in line and "rs" in line and "macro" in line


# ============================================ 4. time horizons

# The REAL ladders, read off the scrip master on 2026-08-05.
NIFTY_LADDER = ["2026-08-11", "2026-08-18", "2026-08-25", "2026-09-01",
                "2026-09-08", "2026-09-29", "2026-10-27", "2026-12-29",
                "2027-03-30", "2027-06-29"]
SHALLOW_LADDER = ["2026-08-25", "2026-09-29", "2026-10-27"]   # FINNIFTY etc.


def test_a_short_horizon_keeps_the_near_contract():
    assert op.pick_expiry(NIFTY_LADDER, today=TODAY, underlying="NIFTY 50",
                          horizon="short") == "2026-08-18"


def test_a_long_horizon_reaches_3_to_6_months_out():
    exp = op.pick_expiry(NIFTY_LADDER, today=TODAY, underlying="NIFTY 50",
                         horizon="long")
    assert exp == "2026-12-29"
    days = (date.fromisoformat(exp) - TODAY).days
    assert op.LONG_HORIZON_MIN_DAYS <= days <= op.LONG_HORIZON_MAX_DAYS


def test_a_shallow_chain_takes_the_furthest_it_HAS_and_does_not_refuse():
    """FINNIFTY / MIDCPNIFTY / every stock option stop at ~3 months. A long
    horizon there is best-effort, never a fabricated expiry and never a
    silent fallback to the near month."""
    exp = op.pick_expiry(SHALLOW_LADDER, today=TODAY,
                         underlying="NIFTY MID SELECT", horizon="long")
    assert exp == "2026-10-27"
    assert exp != op.pick_expiry(SHALLOW_LADDER, today=TODAY,
                                 underlying="NIFTY MID SELECT",
                                 horizon="short")


def test_the_long_horizon_still_respects_the_equity_entry_floor():
    """The settlement guard outranks the horizon preference."""
    tight = ["2026-08-08", "2026-08-09"]        # both inside the 7d floor
    assert op.pick_expiry(tight, today=TODAY, underlying="RELIANCE.NS",
                          horizon="long") is None


def test_a_short_lived_signal_keeps_the_short_horizon_even_under_macro():
    """The TRIGGER defines the holding period, not the backdrop. Putting an
    RSI bounce in a 4-month option pays for time the thesis never uses."""
    assert op.horizon_for({"fresh_cross": True, "rsi": 50},
                          macro_score=5.0) == "short"
    assert op.horizon_for({"fresh_cross": False, "rsi": 25},
                          macro_score=5.0) == "short"
    assert op.horizon_for({"fresh_cross": False, "rsi": 85},
                          macro_score=-5.0) == "short"


def test_a_strong_macro_score_buys_time():
    assert op.horizon_for({"fresh_cross": False, "rsi": 50},
                          macro_score=3.0) == "long"
    assert op.horizon_for({"fresh_cross": False, "rsi": 50},
                          macro_score=-4.0) == "long"
    assert op.horizon_for({"fresh_cross": False, "rsi": 50},
                          macro_score=2.9) == "short"


def test_a_deep_trend_is_structural_on_its_own():
    assert op.horizon_for({"fresh_cross": False, "rsi": 50,
                           "sma_slow_distance_pct": -8.0}) == "long"
    assert op.horizon_for({"fresh_cross": False, "rsi": 50,
                           "sma_slow_distance_pct": -3.0}) == "short"


def test_horizon_defaults_to_short_on_absent_inputs():
    """The pre-2026-08-05 behaviour, so nothing already journalled changes."""
    assert op.horizon_for({}) == "short"
    assert op.horizon_for(None) == "short"
    assert op.horizon_for({"rsi": None, "fresh_cross": None}) == "short"


def test_legacy_pick_expiry_calls_are_byte_identical():
    """No underlying, no horizon — the original index contract."""
    # 2026-08-11 is only 6 days out — inside MIN_DAYS_TO_EXPIRY=7, so the
    # first ELIGIBLE contract is 08-18. Unchanged by this work.
    assert op.pick_expiry(NIFTY_LADDER, today=TODAY) == "2026-08-18"

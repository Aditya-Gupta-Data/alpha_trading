"""
Alpha Trading — Phase 6I: the Technical-to-Options Strategy Planner
====================================================================

A PURE evaluation matrix from a technical market read to the
mathematically appropriate defined-risk options structure — the
"which strategy, which strikes" brain, with zero side effects: no
market data, no DB, no journal, no network. Same inputs, same plan,
forever (fully deterministic — the whole surface tests offline).

    map_technical_to_strategy(technical_state) -> plan dict

The routing matrix (rows = trend read, columns = IV regime):

                 low IV (<13)       high IV (13-16)      extreme (>16)
  range-bound    no_trade (thin)    IRON CONDOR          no_trade (gate)
  strong bull    BULL CALL SPREAD   no_trade (rich)      no_trade (rich)
  bearish        BEAR PUT SPREAD    BEAR CALL SPREAD     no_trade (gate)

  * Range-Bound + High IV -> iron condor: rich premium is the only reason
    to sell a range; "high" here means ELEVATED BUT TRADEABLE — the
    project's hard regime gate (strategy.VIX_BLOCK_ABOVE = 16, decision
    #26 spirit) still blocks all short-range structures above 16, and
    this planner NEVER contradicts that gate.
  * Strong Bullish + Low IV -> bull call spread: debit structures want
    cheap options; the same trend read in high IV buys overpriced calls,
    so it's a deliberate no_trade.
  * Bearish + High IV -> bear CALL spread (credit above the market):
    rich call premium + a falling market = sell the rally that isn't
    coming, with the wing capping the surprise. In low IV the debit-side
    bear put spread (the proposer's own bearish structure) is cheaper.

Strikes are expressed as concrete levels AND offsets from ATM, snapped
to the underlying's strike grid — optimized for Bank Nifty pricing
(step 100, lot 35) by default. Support/resistance boundaries, when
supplied, override the default 2%-OTM short-strike placement: the short
put tucks under support, the short call above resistance — the
technical boundary becomes the structure's defense line.

This module PLANS; it never executes. The output legs are a structural
spec (side/type/strike/offset) for the proposer/constructor layer —
consistent with options_proposer's own geometry (WING_STEPS wings,
SHORT_STRIKE_OTM_PCT shorts) so a planned condor is the same condor the
headless pipeline would build.
"""

from src.options_proposer import LOT_SIZES, SHORT_STRIKE_OTM_PCT, WING_STEPS
from src.simulator import STRIKE_STEPS
from src.strategy import VIX_BLOCK_ABOVE

# IV regime boundaries. Below IV_LOW_BELOW premium is too thin to sell;
# above VIX_BLOCK_ABOVE (16) the existing hard gate refuses short-range
# structures entirely — "high" is the tradeable band between the two.
IV_LOW_BELOW = 13.0

# Trend classification: % distance of spot from the slow (200) SMA marks
# strength; the fast (50) SMA distance must agree in sign to confirm.
STRONG_TREND_PCT = 2.0
TREND_CONFIRM_PCT = 0.5

DEFAULT_UNDERLYING = "NIFTY BANK"


# --- pure classifiers ----------------------------------------------------

def classify_iv(vix) -> str:
    """"low" (<13), "high" (13..16 — rich but under the regime gate),
    "extreme" (>16 — the hard gate territory), "unknown" (no reading)."""
    if vix is None:
        return "unknown"
    vix = float(vix)
    if vix < IV_LOW_BELOW:
        return "low"
    if vix <= VIX_BLOCK_ABOVE:
        return "high"
    return "extreme"


def classify_trend(sma_fast_distance_pct, sma_slow_distance_pct) -> str:
    """Trend read from spot's % distance to the fast/slow SMAs:
    "strong_bullish" / "bullish" / "neutral" / "bearish" /
    "strong_bearish" / "unknown". Mixed signs (spot above one average,
    below the other) is the definition of range-bound -> "neutral"."""
    fast, slow = sma_fast_distance_pct, sma_slow_distance_pct
    if fast is None or slow is None:
        return "unknown"
    fast, slow = float(fast), float(slow)
    if slow >= STRONG_TREND_PCT and fast >= TREND_CONFIRM_PCT:
        return "strong_bullish"
    if slow <= -STRONG_TREND_PCT and fast <= -TREND_CONFIRM_PCT:
        return "strong_bearish"
    if slow > 0 and fast > 0:
        return "bullish"
    if slow < 0 and fast < 0:
        return "bearish"
    return "neutral"


# --- strike geometry ------------------------------------------------------

def _snap_down(level: float, step: float) -> float:
    return (level // step) * step


def _snap_up(level: float, step: float) -> float:
    snapped = (level // step) * step
    return snapped if snapped == level else snapped + step


def _atm(spot: float, step: float) -> float:
    return round(spot / step) * step


def _short_put_strike(spot: float, step: float, support) -> float:
    """The condor/put-side defense line: just under support when a valid
    boundary is supplied (below spot), else SHORT_STRIKE_OTM_PCT out.
    Always at least one full step below spot — never at the money."""
    if support is not None and float(support) < spot:
        strike = _snap_down(float(support), step)
    else:
        strike = _snap_down(spot * (1 - SHORT_STRIKE_OTM_PCT / 100), step)
    return min(strike, _snap_down(spot - step, step))


def _short_call_strike(spot: float, step: float, resistance) -> float:
    """Mirror image above the market: just over resistance when a valid
    boundary (above spot) is supplied, else SHORT_STRIKE_OTM_PCT out."""
    if resistance is not None and float(resistance) > spot:
        strike = _snap_up(float(resistance), step)
    else:
        strike = _snap_up(spot * (1 + SHORT_STRIKE_OTM_PCT / 100), step)
    return max(strike, _snap_up(spot + step, step))


def _leg(side: str, option_type: str, strike: float, atm: float,
         role: str) -> dict:
    return {"side": side, "option_type": option_type, "strike": strike,
            "offset_from_atm": round(strike - atm, 2), "role": role}


# --- the evaluation matrix ------------------------------------------------

def map_technical_to_strategy(technical_state: dict) -> dict:
    """The planner. `technical_state` keys (all optional except spot):

      spot                    float — REQUIRED anchor for strike math
      underlying              default "NIFTY BANK"
      trend                   explicit read ("strong_bullish"/"bullish"/
                              "neutral"/"bearish"/"strong_bearish");
                              when absent it's derived from:
      sma_fast_distance_pct   spot vs fast SMA (50), in %
      sma_slow_distance_pct   spot vs slow SMA (200), in %
      iv_regime               explicit "low"/"high"/"extreme"; when
                              absent classified from:
      vix                     India VIX level (None = unknown)
      support / resistance    technical boundaries — override the default
                              2%-OTM short-strike placement when valid
                              (support below spot / resistance above)

    Returns a plan dict: {"strategy", "view", "iv_regime", "tradeable",
    "underlying", "lot_size", "strike_step", "atm", "legs", "rationale"}.
    `strategy` is "iron_condor" / "bull_call_spread" / "bear_call_spread"
    / "bear_put_spread" / "no_trade"; `legs` structurally defines every
    required option leg (side, CE/PE, strike, offset from ATM) and is
    empty for no_trade. Pure: the input dict is never mutated."""
    state = dict(technical_state or {})
    spot = state.get("spot")
    if spot is None:
        return _no_trade(None, "unknown", "unknown",
                         "no spot price — nothing to anchor strikes to",
                         state.get("underlying", DEFAULT_UNDERLYING))
    spot = float(spot)
    underlying = state.get("underlying", DEFAULT_UNDERLYING)
    step = STRIKE_STEPS.get(underlying, 100.0)
    lot = LOT_SIZES.get(underlying, 35)
    atm = _atm(spot, step)
    wing = WING_STEPS * step

    trend = state.get("trend") or classify_trend(
        state.get("sma_fast_distance_pct"), state.get("sma_slow_distance_pct"))
    iv = state.get("iv_regime") or classify_iv(state.get("vix"))

    base = {"view": trend, "iv_regime": iv, "underlying": underlying,
            "lot_size": lot, "strike_step": step, "atm": atm}

    # -- range-bound rows ---------------------------------------------
    if trend == "neutral":
        if iv == "high":
            put_short = _short_put_strike(spot, step, state.get("support"))
            call_short = _short_call_strike(spot, step, state.get("resistance"))
            return dict(base, strategy="iron_condor", tradeable=True, legs=[
                _leg("SELL", "PE", put_short, atm, "short put (range floor)"),
                _leg("BUY", "PE", put_short - wing, atm, "put wing"),
                _leg("SELL", "CE", call_short, atm, "short call (range cap)"),
                _leg("BUY", "CE", call_short + wing, atm, "call wing"),
            ], rationale=(
                "range-bound read + rich-but-tradeable IV: sell the range "
                f"({put_short:g}P/{call_short:g}C, wings {wing:g} wide) and "
                "let the premium decay inside the boundaries"))
        if iv == "low":
            return _no_trade(base, trend, iv,
                             "range-bound but IV too thin — a condor's "
                             "credit wouldn't pay for its risk")
        return _no_trade(base, trend, iv,
                         "range-bound structures are blocked above VIX "
                         f"{VIX_BLOCK_ABOVE:g} (or with VIX unknown) — the "
                         "regime gate stands")

    # -- directional rows -----------------------------------------------
    if trend == "strong_bullish":
        if iv == "low":
            return dict(base, strategy="bull_call_spread", tradeable=True,
                        legs=[
                            _leg("BUY", "CE", atm, atm, "long call (ATM)"),
                            _leg("SELL", "CE", atm + wing, atm,
                                 "short call (target cap)"),
                        ], rationale=(
                            "strong uptrend + cheap options: buy the ATM "
                            f"call, finance it {wing:g} higher — defined "
                            "risk, debit kept honest by low IV"))
        return _no_trade(base, trend, iv,
                         "strong uptrend but options are expensive — "
                         "buying a debit spread in rich IV pays twice")

    if trend in ("bearish", "strong_bearish"):
        if iv == "high":
            call_short = _short_call_strike(spot, step,
                                            state.get("resistance"))
            return dict(base, strategy="bear_call_spread", tradeable=True,
                        legs=[
                            _leg("SELL", "CE", call_short, atm,
                                 "short call (above resistance)"),
                            _leg("BUY", "CE", call_short + wing, atm,
                                 "call wing"),
                        ], rationale=(
                            "falling market + rich call premium: sell the "
                            f"rally at {call_short:g} CE, wing {wing:g} "
                            "higher caps the surprise — credit collected "
                            "up front"))
        if iv == "low":
            return dict(base, strategy="bear_put_spread", tradeable=True,
                        legs=[
                            _leg("BUY", "PE", atm, atm, "long put (ATM)"),
                            _leg("SELL", "PE", atm - wing, atm,
                                 "short put (target floor)"),
                        ], rationale=(
                            "downtrend + cheap options: the debit-side "
                            "bear put spread — same structure the "
                            "proposer builds on a bearish view"))
        return _no_trade(base, trend, iv,
                         "bearish read but VIX beyond the tradeable band "
                         "(or unknown) — even credit structures stand "
                         "aside in a panic regime")

    # bullish-but-not-strong, unknown trend, unknown everything
    return _no_trade(base, trend, iv,
                     "no matrix row matches this read with conviction — "
                     "the planner only routes setups it can defend")


def _no_trade(base, trend, iv, why, underlying=None) -> dict:
    if base is None:
        base = {"view": trend, "iv_regime": iv, "underlying": underlying,
                "lot_size": LOT_SIZES.get(underlying, 35),
                "strike_step": STRIKE_STEPS.get(underlying, 100.0),
                "atm": None}
    return dict(base, strategy="no_trade", tradeable=False, legs=[],
                rationale=why)

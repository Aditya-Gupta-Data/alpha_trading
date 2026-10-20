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
from src.simulator import STRIKE_STEPS, build_synthetic_chain
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


# --- event gates (Architect ruling 2026-10-20, decision #151) ---------------

# Structures exposed to an earnings gap's vega/gamma (spec §4 item 3) ...
SHORT_VOL_STRATEGIES = frozenset({"iron_condor", "iron_butterfly",
                                  "bull_put_spread", "bear_call_spread"})
# ... and the ones the proximity gate refuses: short-vol plus the bear put.
EARNINGS_GATED_STRATEGIES = SHORT_VOL_STRATEGIES | {"bear_put_spread"}
# A confirmed glassbreaking gap-up refuses every bearish or short-premium structure.
GLASSBREAKING_BLOCKED_STRATEGIES = EARNINGS_GATED_STRATEGIES


# --- GEX zones (Architect ruling 2026-10-20, decision #155: D4 + D6) -------

GEX_TRANSITION_PCT = 0.5      # D4: spot within +/-0.5% of the zero-gamma level S* = transition
MOMENTUM_MIN_PCT = 0.5        # session move (vs previous close) that picks a direction in short gamma
MEAN_REVERSION_STRATEGIES = SHORT_VOL_STRATEGIES   # condors, butterflies, credit verticals


def gamma_zone(spot, zero_gamma, regime_at_spot, band_pct: float = GEX_TRANSITION_PCT) -> dict:
    """D4: "transition" when spot is within band_pct of S*; otherwise "long_gamma" / "short_gamma" by the
    sign of dealer net gamma AT spot. On the textbook curve (negative below S*, positive above) that is
    exactly spot > S* + 0.5% = long and spot < S* - 0.5% = short; under the ruled non_client sign (#154)
    NIFTY's curve runs the other way, so the sign at spot decides, never the side of S*. No read = None."""
    try:
        s = float(spot)
    except (TypeError, ValueError):
        s = None
    out = {"zone": None, "distance_pct": None, "zero_gamma": zero_gamma, "regime_at_spot": regime_at_spot,
           "band_pct": band_pct, "reason": None}
    if not s or regime_at_spot not in ("positive", "negative"):
        out["reason"] = "no GEX read"
        return out
    if zero_gamma:
        out["distance_pct"] = round((s / float(zero_gamma) - 1) * 100, 3)
        if abs(out["distance_pct"]) <= band_pct:
            out["zone"] = "transition"
            return out
    out["zone"] = "long_gamma" if regime_at_spot == "positive" else "short_gamma"
    return out


def _short_gamma_plan(base: dict, state: dict, zone: dict) -> dict:
    """D6: dealers short gamma amplify moves — route to a directional momentum debit spread in the session's
    direction, or long volatility (ATM straddle) when the move is under MOMENTUM_MIN_PCT. The SMA trend is
    not consulted; mean-reversion credit structures never come out of here."""
    atm, step, spot = base["atm"], base["strike_step"], base["spot"]
    wing = WING_STEPS * step
    m = state.get("momentum_pct")
    where = (f"short-gamma zone (spot {zone['distance_pct']:+.2f}% from S* {zone['zero_gamma']:g})"
             if zone.get("distance_pct") is not None else "short-gamma zone (no S* within the grid)")
    if m is not None and m >= MOMENTUM_MIN_PCT:
        return _tradeable(dict(base, view="momentum_bullish"), state, strategy="bull_call_spread", legs=[
            _leg("BUY", "CE", atm, atm, "long call (ATM)"),
            _leg("SELL", "CE", atm + wing, atm, "short call (target cap)")],
            rationale=f"{where}, session {m:+.2f}% up: momentum bull call — SMA read overridden (D6)")
    if m is not None and m <= -MOMENTUM_MIN_PCT:
        return _tradeable(dict(base, view="momentum_bearish"), state, strategy="bear_put_spread", legs=[
            _leg("BUY", "PE", atm, atm, "long put (ATM)"),
            _leg("SELL", "PE", atm - wing, atm, "short put (target floor)")],
            rationale=f"{where}, session {m:+.2f}% down: momentum bear put — SMA read overridden (D6)")
    move = "no session move read" if m is None else f"session {m:+.2f}% (< {MOMENTUM_MIN_PCT:g}%)"
    return _tradeable(dict(base, view="long_volatility"), state, strategy="long_straddle", legs=[
        _leg("BUY", "CE", atm, atm, "long call (ATM)"),
        _leg("BUY", "PE", atm, atm, "long put (ATM)")],
        rationale=f"{where}, {move}: long volatility (ATM straddle) — SMA read overridden (D6)")


def earnings_proximity_block(strategy, days_to_results, gate_days=None):
    """The refusal reason when results are `gate_days` (EARNINGS_PRE_DAYS, 5) or fewer days
    away and `strategy` is gated, else None. Unknown days = no reading = no block."""
    if strategy not in EARNINGS_GATED_STRATEGIES or days_to_results is None:
        return None
    if gate_days is None:
        from src.config import EARNINGS_PRE_DAYS as gate_days
    days = int(days_to_results)
    if 0 <= days <= int(gate_days):
        return (f"earnings proximity gate: results in {days} day(s) (<= {int(gate_days)}) — "
                f"{strategy} refused (#151)")
    return None


def glassbreaking_signals(history: list, today: dict, require_volume: bool = None) -> dict:
    """The gap-up + institutional-volume read that bypasses the 50/200 SMA trend
    (`strategies.glassbreaking.early_breakout_signal`, the one rule). SEAM: today's
    institutional volume is not wired yet — with no `volume` and no `bulk_block_deal`
    in `today` the read abstains (fired False, volume_confirmed None) unless
    GLASSBREAKING_REQUIRE_VOLUME is off, when a >= 2% gap alone fires. Pure."""
    from src.strategies import glassbreaking as gb
    if require_volume is None:
        from src.config import GLASSBREAKING_REQUIRE_VOLUME as require_volume
    out = {"fired": False, "gap_pct": None, "volume_confirmed": None,
           "sma_bypassed": True, "reason": None, "signal": None}
    prev = gb._f((history[-1] if history else {}).get("close"))
    px = gb._f((today or {}).get("open"))
    if prev is None or prev <= 0 or px is None:
        out["reason"] = "no previous close or no price for today"
        return out
    out["gap_pct"] = round((px - prev) / prev * 100.0, 2)
    if out["gap_pct"] < gb.GAP_UP_MIN_PCT:
        out["reason"] = f"no gap-up ({out['gap_pct']:+.2f}% < {gb.GAP_UP_MIN_PCT:g}%)"
        return out
    if today.get("volume") is None and today.get("bulk_block_deal") is None:
        if require_volume:
            out["reason"] = ("gap-up seen but the institutional volume feed is not wired — "
                             "abstained")
            return out
        out.update(fired=True, reason=f"gap-up {out['gap_pct']:+.2f}% (volume not required)")
        return out
    sig = gb.early_breakout_signal(history, today)
    out["volume_confirmed"] = sig is not None
    out.update(fired=sig is not None, signal=sig,
               reason=(f"gap-up {out['gap_pct']:+.2f}% with institutional volume"
                       if sig else "gap-up without institutional volume"))
    return out


def glassbreaking_block(strategy, read: dict):
    """The refusal reason when a fired glassbreaking read meets a bearish/short-premium structure."""
    if not (read or {}).get("fired") or strategy not in GLASSBREAKING_BLOCKED_STRATEGIES:
        return None
    return (f"glassbreaking: {read.get('reason')} — the SMA trend read is bypassed and "
            f"{strategy} refused (#151)")


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


# --- theoretical economics (the anti-Rs.0 rule) ---------------------------

DEFAULT_DAYS_TO_EXPIRY = 7   # same floor the proposer's expiry picker uses


def _fallback_premium(spot: float, strike: float, option_type: str) -> float:
    """For a strike outside the synthetic chain's span (only reachable via
    an extreme support/resistance override): intrinsic + the chain's own
    0.5 minimum time value — never zero, never None."""
    intrinsic = (max(0.0, spot - strike) if option_type == "CE"
                 else max(0.0, strike - spot))
    return round(intrinsic + 0.5, 2)


def estimate_plan_economics(plan: dict, vix=None,
                            days_to_expiry: int = DEFAULT_DAYS_TO_EXPIRY) -> dict:
    """Price a tradeable plan's legs theoretically and derive its
    defined-risk economics, so nothing downstream ever broadcasts a Rs.0
    placeholder. Premiums come from the SAME modeled world everything
    else prices in — the simulator's synthetic chain (intrinsic + ATM
    time value ~0.4·S·σ·√T decaying with strike distance, decision #36)
    — so a planned condor's numbers agree with what the replay/tracker
    would compute for it.

    Returns a NEW plan dict (input untouched, purity holds): each leg
    gains "premium"; the plan gains net_credit/net_debit, spread_width,
    max_profit, max_loss (rupees per lot: credit structures make the
    credit and lose width−credit; debit structures lose the debit and
    make width−debit), and a "pricing" block recording the model inputs.
    No-trade plans pass through unchanged."""
    if not plan.get("tradeable") or not plan.get("legs"):
        return dict(plan)
    spot, step, lot = plan["spot"], plan["strike_step"], plan["lot_size"]
    dte = max(1, int(days_to_expiry or DEFAULT_DAYS_TO_EXPIRY))
    chain = build_synthetic_chain(spot, vix, dte, step)["oc"]

    legs = []
    for leg in plan["legs"]:
        quote = chain.get(f"{leg['strike']:.6f}")
        kind = "ce" if leg["option_type"] == "CE" else "pe"
        premium = (float(quote[kind]["last_price"]) if quote is not None
                   else _fallback_premium(spot, leg["strike"],
                                          leg["option_type"]))
        legs.append(dict(leg, premium=premium))

    credit = sum(l["premium"] for l in legs if l["side"] == "SELL")
    debit = sum(l["premium"] for l in legs if l["side"] == "BUY")
    net = round(credit - debit, 2)
    # worst-case width per option type (a condor can only lose one side)
    widths = []
    for kind in ("CE", "PE"):
        strikes = sorted(l["strike"] for l in legs if l["option_type"] == kind)
        if len(strikes) == 2:
            widths.append(strikes[1] - strikes[0])
    width = max(widths) if widths else 0.0

    if net >= 0:   # credit structure (condor, bear call spread)
        max_profit = net * lot
        max_loss = max(0.0, width - net) * lot
    else:          # debit structure (bull call / bear put spread, long straddle)
        max_loss = -net * lot
        max_profit = (None if plan.get("strategy") == "long_straddle"      # unbounded: no cap leg
                      else max(0.0, width + net) * lot)

    return dict(plan, legs=legs,
                net_credit=net if net >= 0 else None,
                net_debit=round(-net, 2) if net < 0 else None,
                spread_width=width,
                max_profit=None if max_profit is None else round(max_profit, 2),
                max_loss=round(max_loss, 2),
                pricing={"model": "synthetic_chain", "vix_used": vix,
                         "days_to_expiry": dte})


def _tradeable(base: dict, state: dict, **kw) -> dict:
    """Every tradeable plan leaves the matrix WITH economics attached."""
    return estimate_plan_economics(
        dict(base, tradeable=True, **kw), vix=state.get("vix"),
        days_to_expiry=state.get("days_to_expiry", DEFAULT_DAYS_TO_EXPIRY))


# --- the evaluation matrix ------------------------------------------------

def map_technical_to_strategy(technical_state: dict) -> dict:
    """The plan: a short-gamma zone (#155 D4/D6, state key `gex` = {zero_gamma, regime_at_spot}) routes to
    momentum spreads / long volatility over the SMA matrix; every other zone keeps the matrix. Then the #151
    event gates (`days_to_results`, `glassbreaking`) turn a gated structure into no_trade, named; a short-gamma
    zone also refuses any mean-reversion credit structure."""
    state = technical_state or {}
    gex = state.get("gex") or {}
    zone = gamma_zone(state.get("spot"), gex.get("zero_gamma"), gex.get("regime_at_spot"))
    plan = _matrix_plan(technical_state)
    if zone["zone"] == "short_gamma" and plan.get("spot") is not None:
        base = {k: plan[k] for k in ("iv_regime", "underlying", "lot_size", "strike_step", "atm", "spot")
                if k in plan}
        plan = _short_gamma_plan(dict(base, view=plan.get("view")), state, zone)
    plan = dict(plan, gamma_zone=zone)
    why = (earnings_proximity_block(plan.get("strategy"), state.get("days_to_results"))
           or glassbreaking_block(plan.get("strategy"), state.get("glassbreaking"))
           or (f"short-gamma zone: {plan.get('strategy')} is mean reversion, refused (D6, #155)"
               if zone["zone"] == "short_gamma" and plan.get("strategy") in MEAN_REVERSION_STRATEGIES else None))
    if not why:
        return plan
    return dict(_no_trade({k: plan[k] for k in ("view", "iv_regime", "underlying", "lot_size",
                                                "strike_step", "atm", "spot") if k in plan},
                          plan.get("view"), plan.get("iv_regime"), why), gamma_zone=zone)


def _matrix_plan(technical_state: dict) -> dict:
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
            "lot_size": lot, "strike_step": step, "atm": atm, "spot": spot}

    # -- range-bound rows ---------------------------------------------
    if trend == "neutral":
        if iv == "high":
            put_short = _short_put_strike(spot, step, state.get("support"))
            call_short = _short_call_strike(spot, step, state.get("resistance"))
            return _tradeable(base, state, strategy="iron_condor", legs=[
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
            return _tradeable(base, state, strategy="bull_call_spread",
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
            return _tradeable(base, state, strategy="bear_call_spread",
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
            return _tradeable(base, state, strategy="bear_put_spread",
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

"""
Alpha Trading — Phase 5: the options spread proposer
====================================================

The wiring between market data and the Phase 5 machinery: reads the
underlying's trend (same suggestions.analyze() the equity engine uses),
fetches India VIX and the real Dhan option chain, picks strikes, builds
the regime-matched defined-risk spread via strategy.StrategyConstructor,
sizes it by absolute max loss (OPTIONS_RISK_PER_TRADE_PCT), and — on the
user's approval — journals an entry the plan tracker resolves atomically.

Regime -> structure (DECISIONS.md #27):
  bullish  (uptrend dip / fresh golden cross)  -> bull call spread (debit)
  bearish  (downtrend / fresh death cross)     -> bear put spread (debit)
  neutral  (no directional signal)             -> iron condor (credit),
           STRICTLY blocked when India VIX > 16 or VIX is unavailable.

PAPER ONLY, human-in-the-loop (decision #11): this module proposes and
journals; it never touches a broker. Approved spreads don't move cash at
entry — the tracker net-settles the P&L at the atomic basket exit.

Run interactively from the project folder:

    python3 -m src.options_proposer                   # NIFTY 50 (default)
    python3 -m src.options_proposer "NIFTY BANK"
    python3 -m src.options_proposer --review-pending  # decide market-loop
                                                      # PENDING_APPROVAL entries
                                                      # (no market data fetched)

Every data input is injectable so tests run fully offline.
"""

import os
from datetime import date, timedelta

from src import journal
from src import portfolio as pf
from src.config import MAX_RISK_PER_TRADE_RS, OPTIONS_RISK_PER_TRADE_PCT
from src.dhan_client import get_expiry_list, get_india_vix, get_option_chain
from src.strategy import StrategyConstructor
from src.suggestions import analyze

# NSE lot sizes for the option-enabled underlyings (contract spec, not
# market data — revised rarely and loudly by the exchange). CURRENT as of
# the Jan-2026 SEBI revision: NIFTY 50 75->65, NIFTY BANK 35->30 (verified
# 2026-07-15 against NSE lot-size bulletins). The simulator replays history
# with these current sizes; that only scales absolute-rupee P&L, never the
# R-multiples or win-rates the validation harness actually scores (both are
# lot-size-invariant), so the learning signal is unaffected. If a change is
# announced, update HERE — it is the single source every consumer imports.
# 2026-08-05: multi-index expansion, and ALL FOUR lot sizes RE-VERIFIED
# against api-scrip-master-detailed.csv the same day (NSE OPTIDX rows,
# uniform across every live contract): NIFTY 65 (4,008 contracts),
# BANKNIFTY 30 (2,358), FINNIFTY 60 (1,084), MIDCPNIFTY 120 (1,510).
LOT_SIZES = {"NIFTY 50": 65, "NIFTY BANK": 30,
             "NIFTY FIN SERVICE": 60, "NIFTY MID SELECT": 120}

# Never open a position that the 2-days-before-expiry rule would
# immediately close: skip expiries closer than this many days out.
MIN_DAYS_TO_EXPIRY = 7


# =========== EQUITY OPTIONS + PHYSICAL SETTLEMENT (2026-08-05) ==========
#
# The desk was index-only (NIFTY 50 / NIFTY BANK). Adding NSE stock options
# adds ONE risk that indices do not have and that dwarfs every other
# consideration here:
#
#   INDEX options are CASH-SETTLED. Worst case at expiry is a cash debit
#   bounded by the structure's own max loss.
#
#   STOCK options are PHYSICALLY SETTLED. An in-the-money short leg held
#   into expiry becomes a DELIVERY OBLIGATION — the full notional of the
#   underlying, not the spread's max loss. NSE also escalates margin
#   through expiry week (delivery margin phases in from ~E-4), so a
#   defined-risk spread stops being defined-risk in its final days: the
#   margin can multiply while the position is still "within max loss".
#
# So equity options get a STRICTER clock than the index book, in both
# directions, and the two are kept apart by `is_equity_option()`:
#
#   ENTRY  : no NEW equity-option position inside
#            EQUITY_MIN_DAYS_TO_EXPIRY (7). Index entries keep the
#            existing MIN_DAYS_TO_EXPIRY.
#   EXIT   : an OPEN equity-option position is forced out BEFORE expiry
#            week — EQUITY_FORCED_EXIT_DAYS (7) — rather than the index
#            book's PRE_EXPIRY_EXIT_DAYS (2). We leave before the delivery
#            margin period starts, not during it.
#
# The universe is deliberately a SHORT hardcoded list of the most liquid
# F&O names rather than "anything with options": an illiquid stock option
# cannot be exited at a fair price in the week you must exit it, which is
# exactly when this guard forces you to. Widening it is a deliberate edit.

# LOT SIZES VERIFIED 2026-08-05 against Dhan's
# api-scrip-master-detailed.csv (NSE, INSTRUMENT=OPTSTK, uniform across
# every live contract for each name). This CLOSED blocker A2 and caught
# TWO wrong values that had been guessed:
#     HDFCBANK  550 -> 650   (316 contracts, all lot 650)
#     TCS       175 -> 225   (442 contracts, all lot 225)
# RELIANCE 500 / ICICIBANK 700 / INFY 400 were already correct.
# A wrong lot size mis-sizes every position on that name, so re-verify
# here — never guess — whenever the exchange revises contract specs.
EQUITY_OPTION_UNDERLYINGS = {
    "RELIANCE.NS": 500,
    "HDFCBANK.NS": 650,
    "ICICIBANK.NS": 700,
    "INFY.NS": 400,
    "TCS.NS": 225,
}

# Entry: no new stock-option position within this many days of expiry.
EQUITY_MIN_DAYS_TO_EXPIRY = 7
# Exit: force an open stock-option position out this many days before
# expiry — i.e. before expiry week and its delivery-margin escalation.
EQUITY_FORCED_EXIT_DAYS = 7


def is_equity_option(underlying: str) -> bool:
    """True for a PHYSICALLY-SETTLED stock option, False for a cash-settled
    index. The one predicate every settlement-risk branch asks; nothing
    else in the codebase may re-derive this from a string suffix."""
    return str(underlying or "").upper() in EQUITY_OPTION_UNDERLYINGS


def min_days_to_expiry_for(underlying: str) -> int:
    """The entry clock for this underlying — stricter for stock options."""
    return (EQUITY_MIN_DAYS_TO_EXPIRY if is_equity_option(underlying)
            else MIN_DAYS_TO_EXPIRY)


def physical_settlement_gate(underlying: str, expiry: str,
                             today: date = None) -> tuple:
    """(allowed, reason) for OPENING a position on `underlying`.

    Index options pass unconditionally — they are cash-settled and the
    existing expiry choice already handles them. Stock options must clear
    EQUITY_MIN_DAYS_TO_EXPIRY. FAIL-CLOSED on an unparseable or missing
    expiry: an unknown settlement date on a physically-settled instrument
    is exactly the case you must not guess at."""
    if not is_equity_option(underlying):
        return True, None
    today = today or date.today()
    # Require the explicit YYYY-MM-DD form. Python 3.11+ also accepts the
    # BASIC ISO form ("20260812"), so a stray int would parse and silently
    # pass a safety gate — exactly the ambiguity a fail-closed check must
    # refuse rather than resolve.
    text = expiry if isinstance(expiry, str) else ""
    try:
        days = (date.fromisoformat(text) - today).days if text.count("-") == 2 \
            else None
    except ValueError:
        days = None
    if days is None:
        return False, ("equity option with no readable expiry — physical "
                       "settlement risk cannot be assessed (fail-closed)")
    if days < EQUITY_MIN_DAYS_TO_EXPIRY:
        return False, (f"PHYSICAL SETTLEMENT GATE: {days}d to expiry, "
                       f"minimum {EQUITY_MIN_DAYS_TO_EXPIRY}d for a stock "
                       "option — an ITM short leg becomes a delivery "
                       "obligation, and delivery margin escalates through "
                       "expiry week")
    return True, None


def forced_exit_days_for(underlying: str) -> int:
    """How many days before expiry an OPEN position must be closed.
    Stock options leave before expiry week; index options keep the
    tracker's existing 2-day pre-expiry rule."""
    from src.plan_tracker import PRE_EXPIRY_EXIT_DAYS
    return (EQUITY_FORCED_EXIT_DAYS if is_equity_option(underlying)
            else PRE_EXPIRY_EXIT_DAYS)

# Condor short strikes sit ~this far OTM on each side (rounded to a real
# chain strike); protective wings sit WING_STEPS strike-steps further out.
SHORT_STRIKE_OTM_PCT = 2.0
WING_STEPS = 4


# ============ G3 diversity wiring (2026-08-05) ==========================
#
# THE BUG THIS FIXES, measured before it was touched. 19 of 19 resolved
# trades were `bear_put_spread`; bull calls, condors and butterflies had
# fired ZERO times. The diagnostic falsified the obvious suspect — VIX
# never once exceeded the 16 gate (12 readings, 12.00-14.16) — and found
# the cause in this function:
#
#     if not analysis["uptrend"]: return "bearish"     # unconditional
#
# `uptrend` is ONE BIT: sma50 > sma200. NIFTY BANK has been below its
# 200-SMA since 2026-04-15, so every cycle for four months took that first
# branch. Worse, "neutral" sat at the END of the cascade, reachable only
# when uptrend was TRUE — so RANGE was subordinated to DIRECTION and a
# sideways market below its 200-SMA (exactly when a condor is right) could
# not be seen at all. Replay over 90 sessions: 77 bearish, 12 neutral, 1
# bullish, and all 12 neutrals fell inside the brief window where
# sma50 > sma200 still held.
#
# THE FIX, in the order the checks now run:
#   1. RANGE FIRST, direction second. `classify_trend` (the graded
#      classifier that already lived in trade_planner and was wired to
#      NOTHING) plus an explicit flat band. A market hugging its averages
#      is neutral whichever side of them it sits on.
#   2. MEAN-REVERSION, but not into a falling knife. An oversold reading
#      turns bullish only when the graded read is `bearish`, never
#      `strong_bearish` — the grade is exactly the distinction the old
#      binary bit could not express.
#   3. GRADED DIRECTION for everything else.
#
# `uptrend`/`fresh_cross` are still honoured; the entry criteria did not
# get looser, they got SIGHTED. A structure the market is not offering
# must still not be proposed.

# Spot within this % of BOTH averages = flat, regardless of sign. Sized
# from the observed regime: NIFTY BANK's current sma50/sma200 deficit is
# 1.05%, i.e. the market that produced 19 identical trades is genuinely
# range-bound, not trending.
FLAT_BAND_PCT = 1.5

# The tradeable IV band is (IV_LOW_BELOW, VIX_BLOCK_ABOVE] = (13, 16].
# Its UPPER HALF is where an ATM butterfly beats a condor: the body is
# richest exactly when premium is dear, and the tighter structure takes more
# credit for the same wing width.
BUTTERFLY_MIN_VIX = 14.5


def market_view(analysis: dict) -> str:
    """suggestions.analyze() result -> 'bullish' / 'bearish' / 'neutral'.

    Range is judged BEFORE direction (see the block comment above). Falls
    back to the original binary read when the graded inputs are absent, so
    an injected/legacy analysis dict without the SMA distances behaves
    exactly as it did before this change."""
    from src.config import RSI_OVERSOLD
    from src.trade_planner import classify_trend

    fast_pct = analysis.get("sma_fast_distance_pct")
    slow_pct = analysis.get("sma_slow_distance_pct")
    rsi = analysis.get("rsi")

    if fast_pct is None or slow_pct is None:
        # Legacy path, byte-identical to the pre-2026-08-05 behaviour.
        if not analysis["uptrend"]:
            return "bearish"
        if analysis["fresh_cross"]:
            return "bullish"
        if rsi is not None and rsi <= RSI_OVERSOLD:
            return "bullish"
        return "neutral"

    grade = classify_trend(fast_pct, slow_pct)

    # 1. RANGE FIRST — a market pinned to its averages is a range whether
    #    it sits above or below them. This is the decoupling.
    if abs(slow_pct) < FLAT_BAND_PCT and abs(fast_pct) < FLAT_BAND_PCT:
        return "neutral"
    if grade == "neutral":          # mixed signs = the classifier's own range
        return "neutral"

    # 2. A fresh golden cross is still the strongest bullish tell.
    if analysis.get("fresh_cross") and grade in ("bullish", "strong_bullish"):
        return "bullish"

    # 3. MEAN REVERSION, deliberately NOT in a strong downtrend. Oversold
    #    inside a mild pullback is a bounce; oversold inside a collapse is
    #    a falling knife, and the graded read is what tells them apart.
    if rsi is not None and rsi <= RSI_OVERSOLD and grade != "strong_bearish":
        return "bullish"

    if grade in ("bullish", "strong_bullish"):
        return "bullish"
    return "bearish"


# ============ TIME HORIZONS (2026-08-05) ================================
#
# A signal's SHELF LIFE should pick its expiry. An RSI bounce plays out in
# days and wants the near contract; a structural macro read plays out over
# months and is destroyed by buying a 30-day option and paying theta four
# times to stay in the trade.
#
# MEASURED against the scrip master the same day — the available depth is
# very uneven, and this is the constraint the horizon logic must respect:
#     NIFTY        18 expiries, out to 2031-06-24   <- real LEAPS
#     BANKNIFTY     6 expiries, out to 2027-06-29   <- ~11 months
#     FINNIFTY      3 expiries, out to 2026-10-27   <- ~3 months only
#     MIDCPNIFTY    3 expiries, out to 2026-10-27   <- ~3 months only
#     stock options 3 expiries, out to 2026-10-27   <- ~3 months only
#
# So a "3-6 month" target is only fully satisfiable on NIFTY. Rather than
# refuse a long-horizon trade everywhere else, `pick_expiry` takes the
# FURTHEST AVAILABLE contract inside the window and — when nothing reaches
# the window at all — the furthest that exists, which is the honest
# best-effort. It never invents an expiry and never silently falls back to
# the near month while claiming a long horizon.
LONG_HORIZON_MIN_DAYS = 90       # 3 months
LONG_HORIZON_MAX_DAYS = 200      # ~6.5 months, a little slack over 6

HORIZONS = ("short", "long")


# A macro read this strong (|long_term_macro_score|, the -5..+5 dimension
# news_processor emits and brain_map now stores) is treated as structural
# rather than tactical, and buys time instead of paying theta four times.
LONG_HORIZON_MACRO_SCORE = 3.0
# A trend this deep is structural on its own, macro score or not.
LONG_HORIZON_SLOW_SMA_PCT = 5.0


def horizon_for(analysis: dict, macro_score: float = None) -> str:
    """"short" | "long" — which shelf life this signal has.

    SHORT-LIVED SIGNALS KEEP THE NEAR CONTRACT. An RSI bounce or a fresh
    cross is a days-to-weeks event; putting it in a 4-month option pays for
    time the thesis will never use. Those are checked FIRST and win, even
    under a strong macro score, because the trigger is what defines the
    holding period — not the backdrop.

    LONG is chosen only when the driver itself is structural: a
    |long_term_macro_score| >= 3, or spot more than 5% from its 200-SMA.
    Defaults to "short" on absent inputs — the pre-2026-08-05 behaviour."""
    a = analysis or {}
    from src.config import RSI_OVERBOUGHT, RSI_OVERSOLD
    rsi = a.get("rsi")
    if a.get("fresh_cross"):
        return "short"
    if rsi is not None and (rsi <= RSI_OVERSOLD or rsi >= RSI_OVERBOUGHT):
        return "short"
    try:
        if macro_score is not None and abs(float(macro_score)) >= LONG_HORIZON_MACRO_SCORE:
            return "long"
    except (TypeError, ValueError):
        pass
    slow = a.get("sma_slow_distance_pct")
    try:
        if slow is not None and abs(float(slow)) >= LONG_HORIZON_SLOW_SMA_PCT:
            return "long"
    except (TypeError, ValueError):
        pass
    return "short"


def pick_expiry(expiries: list, today: date = None,
                underlying: str = None, horizon: str = "short") -> str | None:
    """`horizon="short"` (default): the FIRST expiry at least
    `min_days_to_expiry_for(underlying)` days out — byte-identical to the
    pre-2026-08-05 behaviour for every existing caller.

    `horizon="long"`: the FURTHEST expiry inside the 90-200 day window, or
    — when the chain does not reach that far, which is the case for
    FINNIFTY / MIDCPNIFTY / every stock option — the furthest contract that
    exists beyond the entry floor. Returns None only when nothing clears
    the floor at all."""
    today = today or date.today()
    floor = min_days_to_expiry_for(underlying) if underlying else MIN_DAYS_TO_EXPIRY

    dated = []
    for exp in sorted(expiries or []):
        try:
            dated.append((exp, (date.fromisoformat(exp) - today).days))
        except ValueError:
            continue
    eligible = [(e, d) for e, d in dated if d >= floor]
    if not eligible:
        return None
    if horizon != "long":
        return eligible[0][0]

    in_window = [e for e, d in eligible
                 if LONG_HORIZON_MIN_DAYS <= d <= LONG_HORIZON_MAX_DAYS]
    if in_window:
        return in_window[-1]          # furthest INSIDE the window
    return eligible[-1][0]            # honest best effort: furthest there is


def _strikes(chain: dict) -> list:
    """Sorted strike floats from a Dhan option-chain payload."""
    return sorted(float(s) for s in (chain.get("oc") or {}))


def _leg_fill(chain: dict, strike: float, kind: str, side: str,
              lots: int = 1) -> tuple:
    """(fill_price, basis) for one leg (kind 'ce'/'pe') — the honest
    paper fill (decision #70): a BUY crosses to the ask, a SELL hits the
    bid, which is what a real fill pays on entry. Falls back to
    last_price (basis "ltp") when the quoted side is missing/zero (thin
    strike, closed market, simulator chains) or deviates >50% from a
    live LTP (stale/crossed book — a data-quality refusal). (None, None)
    means the leg is untradeable. Tries the exact key format Dhan uses
    (six decimals) then a plain match.

    P1 book-depth: `lots` > 1 walks the book — a synthetic +0.05%/10-lots
    penalty worsens the fill (BUY pays up, SELL receives less). Default
    lots=1 => no penalty (byte-identical to the pre-P1 fill)."""
    oc = chain.get("oc") or {}
    node = oc.get(f"{strike:.6f}") or oc.get(str(strike)) or {}
    leg = node.get(kind) or {}
    ltp = leg.get("last_price")
    ltp = float(ltp) if ltp else None
    quote = leg.get("top_ask_price" if side == "BUY" else "top_bid_price")
    quote = float(quote) if quote else None
    if quote is not None and ltp is not None and abs(quote - ltp) > 0.5 * ltp:
        quote = None
    price, basis = (quote, "quoted") if quote is not None else \
        ((ltp, "ltp") if ltp is not None else (None, None))
    if price is not None and lots > 1:
        depth_pen = (max(0, int(lots) - 1) / 10.0) * 0.0005
        price = price * (1 + depth_pen) if side == "BUY" else price * (1 - depth_pen)
    return (price, basis) if price is not None else (None, None)


def _nearest_strike(strikes: list, target: float) -> float:
    return min(strikes, key=lambda s: abs(s - target))


def _step(strikes: list) -> float:
    """The chain's strike interval (e.g. 50 for NIFTY)."""
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(gaps) if gaps else 0.0


def build_proposal(underlying: str = "NIFTY 50", *, analysis: dict = None,
                   vix: float = None, expiry: str = None, chain: dict = None,
                   book: dict = None, prices: dict = None,
                   risk_pct: float = None,
                   short_strike_otm_pct: float = None,
                   advisory: dict = None, today: date = None,
                   horizon: str = None, macro_score: float = None) -> dict:
    """The full pipeline, every input injectable for offline tests.

    `risk_pct` overrides OPTIONS_RISK_PER_TRADE_PCT (e.g. vol_bridge may
    scale it down 30 % under an Expansion regime).  `short_strike_otm_pct`
    overrides SHORT_STRIKE_OTM_PCT for the iron condor's put short strike
    (vol_bridge widen_wings mode widens it to buffer tail risk).  Both fall
    back to their module constants when None.

    Returns {"proposal": dict-or-None, "reason": str, "view": str-or-None,
    "vix": float-or-None} — `reason` always explains a None proposal."""
    _risk_pct = risk_pct if risk_pct is not None else OPTIONS_RISK_PER_TRADE_PCT
    _otm_pct = (short_strike_otm_pct if short_strike_otm_pct is not None
                else SHORT_STRIKE_OTM_PCT)
    if analysis is None:
        analysis = analyze(underlying)
    if analysis is None:
        return {"proposal": None, "view": None, "vix": vix,
                "reason": f"not enough price history for {underlying}"}
    view = market_view(analysis)

    if vix is None:
        vix = get_india_vix()

    # --- Advisory regime radars (regime_filters; composed in fetch_market_state,
    # fail-open). Task 1: veto a BULLISH index spread when the index's heavyweights
    # show institutional distribution or the sector trend is bearish. Task 2 (War
    # Playbook): in a crisis/VIX-spike regime, disable SHORT-premium (iron condor)
    # so only defined-risk long-premium debit spreads ride the fat tail. ---
    if advisory:
        if view == "bullish" and advisory.get("block_bullish"):
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": advisory.get("bullish_reason", "smart-money/sector veto")}
        if view == "neutral" and advisory.get("crisis"):
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "war playbook — short-premium (iron condor) disabled in "
                              f"crisis regime ({advisory.get('crisis_reason', '')})"}

    horizon = horizon or horizon_for(analysis, macro_score=macro_score)
    if expiry is None:
        expiry = pick_expiry(get_expiry_list(underlying), underlying=underlying,
                             horizon=horizon)
    if expiry is None:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "no usable expiry (need >= "
                          f"{min_days_to_expiry_for(underlying)} days out)"}

    # PHYSICAL SETTLEMENT GATE (2026-08-05). Checked AFTER the expiry is
    # known and BEFORE a single leg is priced, because for a stock option
    # this is the risk that is not bounded by the structure: an ITM short
    # leg held into expiry is a delivery obligation, not a max loss. Index
    # options pass through untouched — they are cash-settled.
    settle_ok, settle_why = physical_settlement_gate(underlying, expiry,
                                                     today=today)
    if not settle_ok:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": settle_why}
    if chain is None:
        chain = get_option_chain(underlying, expiry)
    if not chain or not chain.get("oc"):
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "option chain unavailable"}

    strikes = _strikes(chain)
    step = _step(strikes)
    if step <= 0 or len(strikes) < 2 * WING_STEPS + 1:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "option chain too thin to build a spread"}
    spot = float(chain.get("last_price") or analysis["price"])
    atm = _nearest_strike(strikes, spot)
    lot_size = LOT_SIZES.get(underlying) or EQUITY_OPTION_UNDERLYINGS.get(
        str(underlying).upper(), 75)
    sc = StrategyConstructor(vix=vix, lot_size=lot_size)

    fill_bases = {}

    def leg_premiums(pairs):
        """[(strike, 'ce'/'pe', 'BUY'/'SELL'), ...] -> fill premiums, or
        None if any leg has no tradeable quote (never build on a dead
        strike). Each leg's fill basis is recorded so the tracker knows
        whether the spread was already crossed at entry (#70)."""
        prems = []
        for s, k, side in pairs:
            price, basis = _leg_fill(chain, s, k, side)
            if price is None:
                return None
            fill_bases[(s, k.upper(), side)] = basis
            prems.append(price)
        return prems

    if view == "bullish":
        lo, hi = atm, atm + WING_STEPS * step
        prems = leg_premiums([(lo, "ce", "BUY"), (hi, "ce", "SELL")])
        if prems is None:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "no tradeable quotes at the chosen strikes"}
        spread = sc.construct_bull_call_spread(lo, hi, prems[0], prems[1])
        signal = (f"bullish trend read on {underlying} — bull call spread "
                  f"{lo:g}/{hi:g} CE, defined risk")
    elif view == "bearish":
        hi, lo = atm, atm - WING_STEPS * step
        prems = leg_premiums([(hi, "pe", "BUY"), (lo, "pe", "SELL")])
        if prems is None:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "no tradeable quotes at the chosen strikes"}
        spread = sc.construct_bear_put_spread(hi, lo, prems[0], prems[1])
        signal = (f"bearish trend read on {underlying} — bear put spread "
                  f"{hi:g}/{lo:g} PE, defined risk")
    elif view == "neutral" and vix is not None and vix >= BUTTERFLY_MIN_VIX:
        # IRON BUTTERFLY (wired 2026-08-05). `construct_iron_butterfly`
        # was fully implemented, tested and reachable from NOWHERE — no
        # threshold could ever have fired it. It takes the upper half of
        # the tradeable IV band (>= BUTTERFLY_MIN_VIX, still under the
        # hard 16 gate): the ATM body is richest exactly when premium is
        # dear, so a tighter structure earns more credit for the same
        # wing width than a condor's OTM shorts would.
        allowed, why_regime = sc.validate_regime("iron_butterfly")
        if not allowed:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": f"range-bound structure blocked: {why_regime}"}
        wing = WING_STEPS * step
        prems = leg_premiums([(atm, "ce", "SELL"), (atm, "pe", "SELL"),
                              (atm + wing, "ce", "BUY"),
                              (atm - wing, "pe", "BUY")])
        if prems is None:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "no tradeable quotes at the chosen strikes"}
        spread = sc.construct_iron_butterfly(atm, wing, prems[0], prems[1],
                                             prems[2], prems[3])
        signal = (f"neutral range read on {underlying} (VIX {vix:.1f}, upper "
                  f"half of the tradeable band) — iron butterfly {atm:g} body, "
                  f"wings {wing:g} wide")
    else:  # neutral -> iron condor, VIX-gated inside the constructor
        allowed, why_regime = sc.validate_regime("iron_condor")
        if not allowed:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": f"range-bound structure blocked: {why_regime}"}
        put_short = _nearest_strike(strikes, spot * (1 - _otm_pct / 100))
        call_short = _nearest_strike(strikes, spot * (1 + _otm_pct / 100))
        wing = WING_STEPS * step
        prems = leg_premiums([(put_short, "pe", "SELL"),
                              (put_short - wing, "pe", "BUY"),
                              (call_short, "ce", "SELL"),
                              (call_short + wing, "ce", "BUY")])
        if prems is None:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "no tradeable quotes at the chosen strikes"}
        spread = sc.construct_iron_condor(put_short, call_short, wing,
                                          prems[0], prems[1], prems[2], prems[3])
        signal = (f"neutral range read on {underlying} (VIX {vix:.1f}) — iron "
                  f"condor {put_short:g}P/{call_short:g}C, wings {wing:g} wide")

    if spread is None:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "structure failed to build (regime gate or "
                          "incoherent strikes)"}

    for leg in spread["legs"]:
        leg["fill_basis"] = fill_bases.get(
            (leg["strike"], leg["option_type"].upper(), leg["side"].upper()),
            "ltp")

    if book is None:
        book = pf.load()
    if prices is None:
        prices = {}
    lots = sc.size_lots(spread, book, prices, risk_pct=_risk_pct)
    if lots <= 0:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": (f"max loss Rs.{spread['max_loss']:,.0f}/lot doesn't fit "
                           f"the {_risk_pct:g}% options risk "
                           f"budget (or SPAN margin exceeds cash)")}
    # Owner hard cap (decision #84): max_loss × lots may never exceed
    # MAX_RISK_PER_TRADE_RS, whatever the percentage budget allowed.
    if spread["max_loss"] > 0:
        lots = min(lots, int(MAX_RISK_PER_TRADE_RS // spread["max_loss"]))
    if lots <= 0:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": (f"max loss Rs.{spread['max_loss']:,.0f}/lot exceeds "
                           f"the Rs.{MAX_RISK_PER_TRADE_RS:,.0f} hard "
                           f"per-trade risk cap")}
    # Adaptive sizing feedback (Directive 2, decision #81): the real
    # resolved record may shrink (floor 1 lot) or veto this archetype.
    # Fails open inside the module; a crashed layer changes nothing.
    try:
        from src.adaptive_sizing import adjust_option_lots
        lots, _sizing = adjust_option_lots(spread["strategy"], lots)
    except Exception:
        _sizing = None
    if lots <= 0:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": ("adaptive sizing veto: "
                           + (_sizing or {}).get("detail", "earned veto"))}

    net = spread["net_credit"] if spread["net_credit"] is not None else -spread["net_debit"]
    proposal = {
        # journal.new_entry() contract keys:
        "action": "SPREAD",
        "ticker": underlying,
        "shares": spread["lot_size"] * lots,
        "price": abs(net),
        "signal": signal,
        # Phase 5 payload — exactly what plan_tracker._spread_trackable needs:
        "spread": dict(spread, lots=lots, expiry=expiry, entry_spot=spot),
        "view": view,
        "vix": vix,
        "lots": lots,
    }
    return {"proposal": proposal, "view": view, "vix": vix, "reason": "ok",
            "horizon": horizon}


def to_journal_entry(proposal: dict, decision: str, why: str) -> dict:
    """A tracker-resolvable journal record: the standard new_entry()
    fields (short_id, date, decision, why, ...) plus the spread payload.
    Regime-Aware Memory: the market conditions the proposal was born
    under (trend view + VIX band) ride along, so the Brain Map can later
    answer "how does this setup do in conditions like these?"."""
    from src.regime import regime_for
    entry = journal.new_entry(proposal, decision, why,
                              pattern_tags=[proposal["spread"]["strategy"]])
    entry["spread"] = proposal["spread"]
    entry["regime"] = regime_for(proposal.get("view"), proposal.get("vix"))
    return entry


def _describe(p: dict) -> list:
    s = p["spread"]
    lines = [
        f"{s['strategy'].replace('_', ' ').title()} on {p['ticker']} "
        f"(view: {p['view']}, VIX: {p['vix'] if p['vix'] is not None else 'n/a'})",
        f"  expiry {s['expiry']}  |  {p['lots']} lot(s) x {s['lot_size']}",
    ]
    for leg in s["legs"]:
        lines.append(f"  {leg['side']:4} {leg['option_type']} {leg['strike']:g} "
                     f"@ Rs.{leg['premium']:,.2f}")
    net = s["net_credit"] if s["net_credit"] is not None else s["net_debit"]
    kind = "credit" if s["net_credit"] is not None else "debit"
    lines += [
        f"  net {kind} Rs.{net:,.2f}/share",
        f"  max loss Rs.{s['max_loss'] * p['lots']:,.0f}  |  "
        f"max profit Rs.{s['max_profit'] * p['lots']:,.0f}  |  "
        f"SPAN margin Rs.{s['margin']['total_margin'] * p['lots']:,.0f} "
        f"(naked would block Rs.{s['margin']['naked_margin'] * p['lots']:,.0f})",
        "  exits: auto at 65% of max profit, or 2 days before expiry (atomic basket)",
    ]
    memory = p.get("memory_context")
    if memory:
        lines.append("  memory (linked patterns):")
        lines += [f"    {line}" for line in memory.splitlines()]
    if p.get("skeptic_note"):
        lines.append("  " + p["skeptic_note"].replace("**", ""))
    return lines


def _memory_context_for(nodes, engine=None) -> str:
    """Phase 6C/6D knowledge-graph lookup, fail-safe. `nodes` is one seed or
    a list of seeds (ticker, regime/view, strategy) — their linked context
    from the Brain Map's `graph_edges` is merged (de-duplicated) into one
    block, or "" when the graph is empty or unavailable. Seeding by strategy
    and view is what surfaces the Phase 6D causal triples (e.g. iron_condor
    RESULTS_IN loss), which are keyed by concept, not ticker. Read-only
    inference (decision #33): never raises, never writes, so the proposal
    path is never blocked. `engine` is injectable so tests stay offline."""
    try:
        if isinstance(nodes, str):
            nodes = [nodes]
        nodes = [n for n in nodes if n]
        if not nodes:
            return ""
        if engine is None:
            from src.graph_engine import GraphEngine
            engine = GraphEngine()
        lines, seen = [], set()
        for node in nodes:
            for line in engine.summarize_context(node).splitlines():
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        print(f"  (memory-graph lookup skipped: {e})")
        return ""


def _memory_seeds(p: dict) -> list:
    """The nodes a proposal is queried against: its underlying, its regime
    view, and its spread strategy — so both ticker-anchored and concept-
    anchored (Phase 6D causal) edges can match."""
    return [p.get("ticker"), p.get("view"), (p.get("spread") or {}).get("strategy")]


def _skeptic_note_for(p: dict, auditor=None, engine=None) -> str:
    """Phase 11: the Random Forest Skeptic's numerical audit of this
    proposal — the quantitative cross-check on the knowledge graph's
    semantic reasoning. Gathers the same seeds' 2-hop graph edges plus the
    Brain Map's realized stats for the strategy/view tags, hands both with
    the proposal's market numbers to skeptic_agent.RandomForestAuditor,
    and returns the strictly formatted "⚠️ Skeptic Agent Warning" block —
    or "" when the model abstains (untrained — the scaffolding default),
    the probability is healthy, or anything at all fails. ADVISORY ONLY:
    this never gates, resizes, or rejects a proposal (the human decides).
    `auditor`/`engine` are injectable so tests stay offline."""
    try:
        if auditor is None:
            from src.skeptic_agent import RandomForestAuditor
            auditor = RandomForestAuditor()
        if engine is None:
            from src.graph_engine import GraphEngine
            engine = GraphEngine()
        edges, seen = [], set()
        for node in _memory_seeds(p):
            if not node:
                continue
            for e in engine.get_relevant_context(node):
                key = (e.get("source"), e.get("relation"), e.get("target"))
                if key not in seen:
                    seen.add(key)
                    edges.append(e)
        memory_stats = None
        try:
            from src import brain_map
            conn = brain_map.connect()
            memory_stats = brain_map.query_similar_events(
                conn, [t for t in ((p.get("spread") or {}).get("strategy"),
                                   p.get("view")) if t])
            conn.close()
        except Exception:
            pass  # stats are one optional feature; the audit runs without
        result = auditor.audit(p, graph_context=edges,
                               memory_stats=memory_stats)
        if not result.get("warn"):
            return ""
        return (f"⚠️ **Skeptic Agent Warning**: modeled win probability "
                f"{result['probability']:.0%} — the Random Forest's numbers "
                f"disagree with this setup's semantic read. Advisory only; "
                f"the decision stays yours (paper only).")
    except Exception as e:
        print(f"  (skeptic audit skipped: {e})")
        return ""


def _format_proposal_alert(p: dict, action_note: str = None) -> str:
    """The rich Discord markdown for a fully constructed proposal, sent
    the moment the terminal pauses for the y/n decision — so the phone
    knows the system is waiting on a human. `action_note` overrides the
    default action line (headless mode explains itself differently).

    When the proposal carries `memory_context` (the Phase 6C graph lookup),
    a 🧠 Memory block of linked historical patterns rides along in the
    rationale — advisory context only, never a rule change (decision #33)."""
    s = p["spread"]
    vix_text = f"{p['vix']:.2f}" if p["vix"] is not None else "n/a"
    legs_block = "\n".join(
        f"{leg['side']:4} {leg['option_type']} {leg['strike']:g} "
        f"@ Rs.{leg['premium']:,.2f}"
        for leg in s["legs"])
    net = s["net_credit"] if s["net_credit"] is not None else s["net_debit"]
    kind = "Net Credit" if s["net_credit"] is not None else "Net Debit"
    lots = p["lots"]
    action = action_note or ("paused for human-in-the-loop approval in "
                             "the terminal session (paper only).")
    memory = p.get("memory_context")
    memory_block = (f"🧠 **Memory (linked patterns)**:\n```\n{memory}\n```\n"
                    if memory else "")
    skeptic = p.get("skeptic_note")
    skeptic_block = f"{skeptic}\n" if skeptic else ""
    alignment = p.get("alignment_line")
    alignment_block = f"{alignment}\n" if alignment else ""
    book = p.get("book_line")
    book_block = f"{book}\n" if book else ""
    return (
        f"🚨 **PROPOSAL ALERT: {s['strategy'].replace('_', ' ').title()}**\n"
        f"**Market Regime**: {p['view'].title()} view on {p['ticker']} | "
        f"India VIX {vix_text} | expiry {s['expiry']}\n"
        f"**Legs** ({lots} lot(s) x {s['lot_size']}):\n"
        f"```\n{legs_block}\n```\n"
        f"**Economics**: {kind} Rs.{net:,.2f}/share | "
        f"Max Loss Rs.{s['max_loss'] * lots:,.0f} | "
        f"Max Profit Rs.{s['max_profit'] * lots:,.0f} | "
        f"SPAN Margin Rs.{s['margin']['total_margin'] * lots:,.0f}\n"
        f"{book_block}"
        f"{alignment_block}"
        f"{memory_block}"
        f"{skeptic_block}"
        f"⏸️ **Action Required**: {action}"
    )


def _notify_discord(text: str) -> bool:
    """Fire-and-forget Discord push from this sync CLI. Fail-safe: an
    unconfigured webhook or any error just prints a note — the terminal
    prompt is never blocked or crashed by Discord being unreachable."""
    import asyncio
    from src import notifier
    try:
        return asyncio.run(notifier.send_discord_message(text))
    except Exception as e:
        print(f"  (discord notify failed: {e})")
        return False


AUTO_APPROVE_ENV_KEY = "PAPER_AUTO_APPROVE"
AUTO_APPROVE_WHY = ("(auto-approved: PAPER_AUTO_APPROVE learning mode — "
                    "paper journal only; no broker exists anywhere in this "
                    "system, decision #11's no-execution rule untouched)")


def paper_auto_approve_enabled() -> bool:
    """True ONLY when PAPER_AUTO_APPROVE is explicitly truthy in the
    environment/.env. Default is OFF: the human Approve/Reject gate
    (decision #11's human-in-the-loop, reaffirmed by the Discord-ingestion
    spec §3) stays exactly as it is unless the user deliberately flips
    this switch to maximize learning data. Checked per call so a .env
    change takes effect without a restart.

    Physical isolation from real capital is structural, not conditional:
    the ONLY thing an approval ever does in this codebase is journal a
    paper decision and lock simulated margin — there is no broker client,
    no order endpoint, no real-capital code path for this to reach
    (dhan_client is data-only by hard rule)."""
    return (os.environ.get(AUTO_APPROVE_ENV_KEY, "")
            .strip().lower() in ("1", "true", "yes"))


def run_headless(underlying: str = "NIFTY 50", state: dict = None) -> dict:
    """The market loop's entry point: build the proposal, fire the rich
    Discord alert, journal the entry as PENDING_APPROVAL, and return
    IMMEDIATELY — no input(), no terminal pause, ever.

    `state` (optional) is a dict of build_proposal keyword overrides
    (analysis/vix/expiry/chain/book/prices) — the injection seam the
    Phase 7 simulator and the market loop's fetch_market_state() use.

    Pending entries are tracked hypothetically like rejected ones (user's
    call): if nobody ever decides, the tracker still scores what the
    setup would have done.

    PAPER_AUTO_APPROVE mode: when the env switch is on, the freshly
    journaled pending entry is immediately decided through decide_pending
    — the SAME code path a human tap takes, so the margin gate, the
    journal rewrite, the Discord confirmation and the "opened" broadcast
    all behave identically; only the finger on the button changes. The
    entry's `why` carries the auto-approval marker for the audit trail.

    Returns {"proposed": bool, "reason": str, "entry": dict-or-None,
    "auto_approved": bool}."""
    state = dict(state or {})
    vol_overrides = state.pop("vol_overrides", {})
    bp_extras = {k: vol_overrides[k]
                 for k in ("risk_pct", "short_strike_otm_pct")
                 if k in vol_overrides}
    result = build_proposal(underlying, **state, **bp_extras)
    if result["proposal"] is None:
        return {"proposed": False, "reason": result["reason"], "entry": None}
    p = result["proposal"]
    # Decision #68: one open position per underlying+direction. Sits
    # BEFORE enrichment, the evidence stamp and the margin gate — a
    # blocked duplicate costs nothing downstream and never locks margin.
    # Sandbox books (simulator / tests / what-ifs) are their own worlds —
    # exempt, same rule as the margin gate below. Fail-OPEN by hard rule.
    if "book" not in state:
        from src import exposure_gate
        allowed, exp_reason = exposure_gate.gate_entry(
            p, notify_fn=_notify_discord)
        if not allowed:
            return {"proposed": False, "reason": exp_reason, "entry": None}
    # Book context (annotate-only, #63 stage 1): what the real book already
    # holds and why, so the newcomer is judged in context. Sandbox books are
    # exempt (their journal isn't their book); fail-open — a broken book
    # read never touches the proposal.
    if "book" not in state:
        try:
            from src.book_context import book_line
            p["book_line"] = book_line(p.get("ticker"), result.get("view"))
        except Exception:
            p["book_line"] = None
    # Phase 6C: enrich the Discord rationale with linked historical patterns
    # from the knowledge graph (fail-safe: "" when the graph is empty).
    p["memory_context"] = _memory_context_for(_memory_seeds(p))
    # Phase 11: the Random Forest Skeptic's numerical audit — right before
    # the alert is formatted ("" until a trained model exists).
    p["skeptic_note"] = _skeptic_note_for(p)
    entry = to_journal_entry(
        p, "pending_approval",
        "(headless proposal — auto-generated by the market loop, awaiting "
        "human decision)")
    # Phase 2 (holy-grail plan §5.1): stamp what EVERY layer said at this
    # exact moment onto the entry — the substrate per-layer reliability is
    # learned from. Simulator-injected runs stamp too (their state carries
    # the as-of analysis/vix; local artifacts read as-is). Fail-open: a
    # stamp failure never blocks the proposal.
    from src.confluence.evidence import alignment_line, capture_for_entry
    snapshot = capture_for_entry(entry, underlying,
                                 analysis=state.get("analysis"),
                                 vix=state.get("vix"))
    # Phase 3 (§6.2): the descriptive alignment line rides the alert card
    # exactly like memory_context/skeptic_note — facts vs the proposal's
    # direction, "evidence not gate", nothing scored, nothing blocked.
    if snapshot is not None:
        try:
            p["alignment_line"] = alignment_line(snapshot, p.get("view"))
        except Exception:
            p["alignment_line"] = None
    # Phase 2 (§5.3): the decision receipt — the WHY-context that isn't
    # otherwise journaled, frozen at proposal time so a human (or a future
    # session) can reconstruct this firing without re-deriving anything.
    # Additive key, fail-open like the stamp.
    try:
        entry["receipt"] = {
            "underlying": underlying,
            "vix": state.get("vix"),
            "analysis": {k: (state.get("analysis") or {}).get(k)
                         for k in ("uptrend", "fresh_cross", "rsi", "price")},
            "vol_overrides": dict(vol_overrides) if vol_overrides else {},
            "book": "sandbox" if "book" in state else "real",
            "memory_context": p.get("memory_context") or "",
            "skeptic_note": p.get("skeptic_note") or "",
        }
    except Exception:
        pass
    # Phase 6G: the capital layer's strict entry guard — lock the SPAN
    # margin against the Rs.10L account pool, or reject SILENTLY (no
    # journal line, no Discord alert; the manager logs the exhaustion/halt
    # event). Guards only the REAL paper book: a caller-injected `book`
    # (simulator, tests, what-if runs) is its own capital world, so the
    # real account never gates it — and never gets touched by it.
    if "book" not in state:
        from src import portfolio_manager as pm
        allowed, gate_reason = pm.gate_headless_entry(
            entry["short_id"], pm.required_margin_for(p))
        if not allowed:
            return {"proposed": False, "reason": gate_reason, "entry": None}
    journal.log(entry)
    # Auto-approve NEVER applies to an injected book: decide_pending's
    # margin gate only knows the REAL Rs.10L account, so auto-approving a
    # sandboxed (simulator / what-if) proposal would lock real margin from
    # a run that was promised its own capital world (see the gate comment
    # above). Sandbox callers decide their own entries.
    auto_mode = paper_auto_approve_enabled() and "book" not in state
    # Phase 3 (§6.6) engagement tripwire: full autonomy is a supervision
    # contract, not abandonment. No human action for N trading days while
    # auto-approve is ON -> NEW auto-approvals pause (the entry stays
    # pending, which decision #31 already tracks hypothetically) and one
    # "unsupervised" card fires per pause episode. Any human decision
    # re-arms instantly (decide_pending touches the pulse). Fail-open: a
    # tripwire error never changes behavior.
    if auto_mode:
        try:
            from src import human_pulse
            if human_pulse.auto_approve_tripped():
                auto_mode = False
                if human_pulse.should_alert_once():
                    _notify_discord(human_pulse.unsupervised_card())
        except Exception:
            pass
    if auto_mode:
        action_note = ("auto-proposed by the market loop — PAPER_AUTO_APPROVE "
                       f"is ON, so trade id `{entry['short_id']}` is being "
                       "journaled as APPROVED automatically, margin gate "
                       "permitting (paper only; the plan tracker manages "
                       "the exit).")
    else:
        action_note = ("auto-proposed by the market loop and journaled as "
                       f"PENDING_APPROVAL (trade id `{entry['short_id']}`) — "
                       "type `/pending` here for Approve/Reject buttons, or "
                       "run `python3 -m src.options_proposer "
                       "--review-pending` in a terminal (paper only).")
    _notify_discord(_format_proposal_alert(p, action_note=action_note))
    if auto_mode:
        verdict = decide_pending(entry["short_id"], approve=True,
                                 why=AUTO_APPROVE_WHY, human=False)
        if verdict["status"] == "approved":
            return {"proposed": True, "reason": "ok (auto-approved)",
                    "entry": verdict["entry"], "auto_approved": True}
        # margin_blocked etc. — the entry stays pending for a human; report
        # honestly instead of pretending the auto-approval happened.
        return {"proposed": True,
                "reason": f"proposed; auto-approval declined "
                          f"({verdict.get('reason', verdict['status'])})",
                "entry": entry, "auto_approved": False}
    return {"proposed": True, "reason": "ok", "entry": entry,
            "auto_approved": False}


def run_session(underlying: str = "NIFTY 50") -> None:
    print(f"Options proposer — {underlying} (paper only)\n")
    result = build_proposal(underlying)
    if result["proposal"] is None:
        print(f"No proposal: {result['reason']}")
        return
    p = result["proposal"]
    # Phase 6C: attach knowledge-graph memory context for the rationale.
    p["memory_context"] = _memory_context_for(_memory_seeds(p))
    # Phase 11: numerical skeptic audit ("" while the model is untrained).
    p["skeptic_note"] = _skeptic_note_for(p)
    for line in _describe(p):
        print(line)
    # Surface the proposal to Discord BEFORE pausing for the decision, so
    # the phone gets the full picture while the terminal waits. Fail-safe:
    # an unreachable Discord never stops the session.
    _notify_discord(_format_proposal_alert(p))
    answer = input("\nTake this spread on paper? [y/N] ").strip().lower()
    decision = "approved" if answer == "y" else "rejected"
    why = input("Why? (one line) ").strip() or "(no reason given)"
    _journal_entry = to_journal_entry(p, decision, why)
    journal.log(_journal_entry)
    # Short follow-up with the outcome (the alert above already carried
    # the full detail); the resolution side is pushed by the API loop
    # when the tracker closes the basket.
    marker = "✅" if decision == "approved" else "❌"
    _notify_discord(f"{marker} **Decision on {p['ticker']} "
                    f"{p['spread']['strategy'].replace('_', ' ')}: "
                    f"{decision.upper()}**\nWhy: {why}")
    if decision == "approved":
        print("\nJournaled as approved — the plan tracker manages the exit "
              "from here (65% profit take / pre-expiry rule). Cash settles "
              "net at the exit.")
        try:
            from src.notifier import fire_broadcast
            s = p["spread"]
            fire_broadcast({
                "event": "opened",
                "ticker": p["ticker"],
                "date": _journal_entry["date"],
                "strategy": s.get("strategy"),
                "short_id": _journal_entry.get("short_id"),
                "lots": p["lots"],
                "lot_size": s.get("lot_size", 75),
                "max_loss": float(s.get("max_loss", 0)) * p["lots"],
                "max_profit": float(s.get("max_profit", 0)) * p["lots"],
                "expiry": s.get("expiry", ""),
                "signal": p.get("signal", ""),
            })
        except Exception as _bcast_err:
            print(f"  (broadcast alert skipped: {_bcast_err})")
    else:
        print("\nJournaled as skipped — the tracker will score the skip.")


def _describe_pending(entry: dict) -> list:
    """Terminal display for one stored pending entry — built entirely from
    the journaled spread payload, no market data fetched."""
    s = entry["spread"]
    net = s["net_credit"] if s.get("net_credit") is not None else s.get("net_debit")
    kind = "credit" if s.get("net_credit") is not None else "debit"
    lots = s.get("lots", 1)
    lines = [
        f"{s['strategy'].replace('_', ' ').title()} on {entry['ticker']} "
        f"(proposed {entry['date']}, expiry {s['expiry']})",
        f"  signal at proposal: {entry.get('signal')}",
        f"  {lots} lot(s) x {s['lot_size']}",
    ]
    for leg in s["legs"]:
        lines.append(f"  {leg['side']:4} {leg['option_type']} {leg['strike']:g} "
                     f"@ Rs.{leg['premium']:,.2f}")
    lines += [
        f"  net {kind} Rs.{net:,.2f}/share  |  "
        f"max loss Rs.{s['max_loss'] * lots:,.0f}  |  "
        f"max profit Rs.{s['max_profit'] * lots:,.0f}",
        "  exits: auto at 65% of max profit, or 2 days before expiry (atomic basket)",
    ]
    return lines


def decide_pending(trade_id: str, approve: bool, why: str = "",
                   human: bool = True) -> dict:
    """The two-way Discord bridge's headless twin of review_pending():
    decide ONE stored pending_approval entry, located by its journal
    short_id, with exactly the CLI's semantics —

      approve=True  -> decision "approved" ON PAPER (the plan tracker takes
                       over; NO broker call anywhere, decision #11), and
      approve=False -> decision "rejected" (this codebase's canonical skip).

    Entries the tracker already resolved hypothetically (outcome set) are
    left as-is — no approving with hindsight (decision #31). Fires the same
    fail-safe Discord confirmation the interactive review does.

    Returns {"status": "approved"|"rejected"|"not_found"|"already_resolved",
             "entry": dict-or-None}."""
    if human:
        # Phase 3 (§6.6): any human decision — button, CLI, either verdict
        # — is the pulse that keeps full autonomy armed. Recorded before
        # the lookup so even a not_found tap counts as presence.
        try:
            from src import human_pulse
            human_pulse.touch("decide_pending")
        except Exception:
            pass
    entries = journal.read_all()
    target = None
    for e in entries:
        if (e.get("decision") == "pending_approval"
                and e.get("short_id") == trade_id):
            target = e
            break
    if target is None:
        return {"status": "not_found", "entry": None}
    if target.get("outcome"):
        return {"status": "already_resolved", "entry": target}

    if approve:
        # Phase 6J: approval is the moment a trade is ACCEPTED, so the
        # capital layer must grant its margin first (idempotent no-op when
        # the headless gate already locked it at proposal time). A
        # margin-blocked approval leaves the entry pending — nothing is
        # journaled, broadcast, or settled.
        spread = target.get("spread") or {}
        per_lot = (spread.get("margin") or {}).get("total_margin")
        if per_lot is not None:
            from src import portfolio_manager as pm
            # Same reservation math as the headless gate (incl. the
            # entry-time VIX-stress factor) — the entry's receipt carries
            # the VIX it was born under; None degrades to factor 1.0.
            required = pm.required_margin_for(
                {"spread": spread,
                 "vix": (target.get("receipt") or {}).get("vix")})
            allowed, gate_reason = pm.gate_headless_entry(trade_id, required)
            if not allowed:
                return {"status": "margin_blocked", "entry": target,
                        "reason": gate_reason}

    decision = "approved" if approve else "rejected"
    target["decision"] = decision
    target["why"] = (why or "").strip() or "(no reason given)"
    journal.rewrite_all(entries)
    if not approve:
        # Phase 6G: a human rejection frees the entry's margin lock right
        # away (zero P&L — the trade never happened). Safe no-op if the
        # entry predates the capital layer.
        from src import portfolio_manager as pm
        pm.release_entry(trade_id, 0.0)
    marker = "✅" if approve else "❌"
    strategy = (target.get("spread") or {}).get("strategy", "proposal")
    _notify_discord(f"{marker} **Pending decision on {target['ticker']} "
                    f"{strategy.replace('_', ' ')}: {decision.upper()}**\n"
                    f"Why: {target['why']}")
    if approve:
        try:
            from src.notifier import fire_broadcast
            spread = target.get("spread") or {}
            lots = int(spread.get("lots", 1))
            fire_broadcast({
                "event": "opened",
                "ticker": target.get("ticker", "?"),
                "date": date.today().isoformat(),
                "strategy": spread.get("strategy"),
                "short_id": target.get("short_id"),
                "lots": lots,
                "lot_size": int(spread.get("lot_size", 75)),
                "max_loss": float(spread.get("max_loss", 0)) * lots,
                "max_profit": float(spread.get("max_profit", 0)) * lots,
                "expiry": spread.get("expiry", "?"),
                "signal": target.get("signal", ""),
            })
        except Exception as _bcast_err:
            print(f"  (broadcast alert skipped: {_bcast_err})")
    return {"status": decision, "entry": target}


def review_pending() -> int:
    """Close the market-loop's loop: read the journal for
    decision == "pending_approval" entries (no market data fetched — the
    stored spread payload is the whole proposal) and decide each one:

      y -> decision becomes "approved" ON PAPER: the plan tracker now
           treats it as a held position and net-settles cash at the
           atomic basket exit. NO broker call is made anywhere —
           dhan_client is data-only by hard project rule (decision #11 /
           Phase 13 gate); "execute" in this system means paper.
      n -> decision becomes "rejected" (this codebase's term for a skip,
           so the scorecard/review flows keep seeing it) + your why.

    Entries that already resolved hypothetically (outcome set before you
    decided) are left as-is and reported. Returns how many entries were
    decided."""
    entries = journal.read_all()
    pending = [(i, e) for i, e in enumerate(entries)
               if e.get("decision") == "pending_approval"]
    if not pending:
        print("No pending proposals found.")
        return 0

    decided = 0
    for i, entry in pending:
        print()
        if entry.get("outcome"):
            print(f"(already resolved) {entry['ticker']} "
                  f"{entry['spread']['strategy']} from {entry['date']} — "
                  f"verdict: {entry['outcome'].get('verdict')}. Left as-is.")
            continue
        for line in _describe_pending(entry):
            print(line)
        answer = input("\nTake this spread on paper? [y/N] ").strip().lower()
        decision = "approved" if answer == "y" else "rejected"
        why = input("Why? (one line) ").strip() or "(no reason given)"
        entry["decision"] = decision
        entry["why"] = why
        decided += 1
        marker = "✅" if decision == "approved" else "❌"
        _notify_discord(f"{marker} **Pending decision on {entry['ticker']} "
                        f"{entry['spread']['strategy'].replace('_', ' ')}: "
                        f"{decision.upper()}**\nWhy: {why}")
        if decision == "approved":
            print("  approved on paper — the plan tracker manages the exit "
                  "from here.")
        else:
            print("  skipped — the tracker will score the skip.")

    if decided:
        journal.rewrite_all(entries)
        print(f"\n{decided} pending proposal(s) decided and journaled.")
    return decided


if __name__ == "__main__":
    import sys
    if "--review-pending" in sys.argv:
        review_pending()
    else:
        run_session(sys.argv[1] if len(sys.argv) > 1 else "NIFTY 50")

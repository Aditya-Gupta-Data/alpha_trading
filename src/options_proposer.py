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
from datetime import date, datetime, timedelta, timezone

from src import journal
from src import portfolio as pf
from src.portfolio_manager import LIVE_ALREADY_OPEN
from src.position_sizing import fractional_lots
from src.config import ACCOUNT_RISK_PER_TRADE_PCT
from src.dhan_client import get_expiry_list, get_india_vix, get_option_chain
from src.strategy import StrategyConstructor, reward_risk_gate
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

# THE HOLDING WINDOW (audit F17, 2026-10-06). A fresh entry must have at
# least this many calendar days between its entry floor and the day its
# forced pre-expiry exit fires — the gap the index pair has always had
# (MIN_DAYS_TO_EXPIRY 7 vs plan_tracker.PRE_EXPIRY_EXIT_DAYS 2). The stock
# pair below once had none (floor 7 == forced exit 7): a stock option
# entered exactly 7 days out was closed by the live arm's first tick and by
# the primary's next close — a guaranteed two-crossing round trip booked as
# a strategy outcome. tests/test_entry_floor_and_approval_window.py holds
# every underlying to floor - forced exit >= this buffer.
ENTRY_HOLD_BUFFER_DAYS = 5


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
#            EQUITY_MIN_DAYS_TO_EXPIRY (12 = the forced exit's 7 + the
#            ENTRY_HOLD_BUFFER_DAYS 5, audit F17 — it was 7, the same
#            number as the forced exit, so a day-7 entry was closed on
#            arrival). Index entries keep the existing MIN_DAYS_TO_EXPIRY.
#   EXIT   : an OPEN equity-option position is forced out BEFORE expiry
#            week — EQUITY_FORCED_EXIT_DAYS (7) — rather than the index
#            book's PRE_EXPIRY_EXIT_DAYS (2). We leave before the delivery
#            margin period starts, not during it. This number guards the
#            physical-settlement risk and is NOT the one F17 moved.
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

# Exit: force an open stock-option position out this many days before
# expiry — i.e. before expiry week and its delivery-margin escalation.
EQUITY_FORCED_EXIT_DAYS = 7
# Entry: no new stock-option position within this many days of expiry —
# the forced exit plus the holding window, so a fresh entry always has
# ENTRY_HOLD_BUFFER_DAYS to work before that exit can touch it (F17).
EQUITY_MIN_DAYS_TO_EXPIRY = EQUITY_FORCED_EXIT_DAYS + ENTRY_HOLD_BUFFER_DAYS


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
                       f"expiry week (forced out at {EQUITY_FORCED_EXIT_DAYS}d, "
                       f"so an entry needs {ENTRY_HOLD_BUFFER_DAYS}d more "
                       "to hold — audit F17)")
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


def regime_read(analysis: dict, vix: float = None) -> dict:
    """The regime inputs behind `market_view`, on the record (G3 audit,
    2026-09-23): the graded trend (spot's % distance to the fast/slow SMAs
    → `classify_trend`), the RSI, whether the flat band pinned it to a
    range, the VIX, and the view + structure that followed. Rides on every
    proposal AND every refusal so the archetype mix can be read back from
    the journal / proposal ledger instead of guessed."""
    from src.trade_planner import classify_trend
    from src.strategy_router import structure_for_view
    fast_pct = analysis.get("sma_fast_distance_pct")
    slow_pct = analysis.get("sma_slow_distance_pct")
    view = market_view(analysis)
    graded = fast_pct is not None and slow_pct is not None
    flat = bool(graded and abs(float(slow_pct)) < FLAT_BAND_PCT
                and abs(float(fast_pct)) < FLAT_BAND_PCT)
    return {"view": view,
            "structure": structure_for_view(view, vix, BUTTERFLY_MIN_VIX),
            "grade": classify_trend(fast_pct, slow_pct) if graded else "legacy",
            "sma_fast_pct": fast_pct, "sma_slow_pct": slow_pct,
            "flat_band_pct": FLAT_BAND_PCT, "flat": flat,
            "rsi": analysis.get("rsi"), "fresh_cross": bool(analysis.get("fresh_cross")),
            "vix": vix}


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
                   account_equity: float = None,
                   account_cash: float = None,
                   short_strike_otm_pct: float = None,
                   advisory: dict = None, today: date = None,
                   horizon: str = None, macro_score: float = None) -> dict:
    """The full pipeline, every input injectable for offline tests.

    `risk_pct` overrides ACCOUNT_RISK_PER_TRADE_PCT (e.g. vol_bridge may
    scale it down 30 % under an Expansion regime).  `short_strike_otm_pct`
    overrides SHORT_STRIKE_OTM_PCT for the iron condor's put short strike
    (vol_bridge widen_wings mode widens it to buffer tail risk).  Both fall
    back to their module constants when None.

    Returns {"proposal": dict-or-None, "reason": str, "view": str-or-None,
    "vix": float-or-None} — `reason` always explains a None proposal."""
    _risk_pct = risk_pct if risk_pct is not None else ACCOUNT_RISK_PER_TRADE_PCT
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

    # Realized-vol rank, DIAGNOSTIC only (the #109 gate on it was withdrawn
    # by #110 — nothing here refuses a trade). Rides on every proposal.
    vol_rank = None
    try:
        from src.config import VOL_RANK_LOOKBACK, VOL_RANK_WINDOW
        from src.vol_rank import hv_rank
        vol_rank = hv_rank(analysis.get("closes"), VOL_RANK_WINDOW, VOL_RANK_LOOKBACK)
    except Exception as exc:
        vol_rank = {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    horizon = horizon or horizon_for(analysis, macro_score=macro_score)
    if expiry is None:
        # `today` reaches the expiry choice as well as the gate below, so an
        # injected clock judges both against the same day (None = today).
        expiry = pick_expiry(get_expiry_list(underlying), today=today,
                             underlying=underlying, horizon=horizon)
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
        # audit F11: premiums that invert the structure are refused by name
        return {"proposal": None, "view": view, "vix": vix,
                "reason": (f"structure refused: {sc.last_refusal}" if sc.last_refusal else
                           "structure failed to build (regime gate or incoherent strikes)")}

    for leg in spread["legs"]:
        leg["fill_basis"] = fill_bases.get(
            (leg["strike"], leg["option_type"].upper(), leg["side"].upper()),
            "ltp")

    # REWARD-TO-RISK GUARDRAIL (decision #98, 2026-09-16): the built
    # structure's max_profit / max_loss must clear the floor for its family
    # (1.5 directional, 0.35 range-bound) BEFORE sizing, margin or any
    # card. The refused structure rides on `rejected_spread` for the
    # ghost tracker, exactly like a sizing refusal.
    rr_ok, rr, rr_floor, rr_why = reward_risk_gate(spread)
    if not rr_ok:
        return {"proposal": None, "view": view, "vix": vix,
                "rejected_spread": spread, "expiry": expiry,
                "reward_risk": rr, "reward_risk_floor": rr_floor,
                "reason": rr_why}

    if book is None:
        book = pf.load()
    if prices is None:
        prices = {}
    # V1.1 (decision #106): fixed-fractional risk on the PRIMARY account's
    # own equity — one door, `position_sizing.fractional_lots`; the shadow
    # accounts run the same formula on THEIR equity in
    # portfolio_manager.evaluate_shadow_accounts. `account_equity` is the
    # injectable seam; live it is the paper account's equity.
    equity = account_equity if account_equity is not None else _primary_equity(book, prices)
    cash = account_cash if account_cash is not None else _primary_available_cash(book)
    # margin wall on the primary's own liquid cash and the VIX-stressed ask (Chunk 3 Z2/Z4)
    sizing = fractional_lots(equity, spread["max_loss"], _risk_pct,
                             margin_per_lot=spread["margin"]["total_margin"] * pf.span_stress_factor(vix),
                             available_cash=cash)
    lots = sizing["lots"]
    if lots <= 0:
        # `rejected_spread` (2026-08-07) is ADDITIVE OBSERVABILITY: the
        # structure the engine built and then refused. Nothing reads it on
        # the trading path — it exists so `ghost_tracker` can mark a
        # refused trade to market instead of guessing what it was. Without
        # it a rejection is just a sentence, and "what would it have done?"
        # is unanswerable.
        return {"proposal": None, "view": view, "vix": vix,
                "rejected_spread": spread, "expiry": expiry,
                "reason": f"sizing refused: {sizing['reason']}"}
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
                "rejected_spread": spread, "expiry": expiry,
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
        # decision #106: how this ticket was sized, on the record.
        "sizing": dict(sizing, account="PAPER_10L", lots_final=lots),
        # G3 audit (2026-09-23): the regime inputs that chose this archetype.
        "regime_read": regime_read(analysis, vix),
        # realized-vol rank on the record (diagnostic; no gate — #110).
        "vol_rank": vol_rank,
    }
    return {"proposal": proposal, "view": view, "vix": vix, "reason": "ok",
            "horizon": horizon}


def _primary_available_cash(book: dict) -> float:
    """The primary's liquid cash (equity minus locks); the legacy book's cash only when the ledger is unreadable."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            from src import brain_map, portfolio_manager as pm
            conn = brain_map.connect()
            try:
                return float(pm.available_cash(conn))
            finally:
                conn.close()
        except Exception:
            pass
    return float(book.get("cash") or 0.0)


def _primary_equity(book: dict, prices: dict) -> float:
    """The primary paper account's total equity for sizing (decision #106).
    Live: `portfolio_manager` (starting capital + realized P&L). Under
    pytest, or if the ledger cannot be read, the legacy paper book's
    total value — a sizing input is never guessed at zero."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            from src import brain_map, portfolio_manager as pm
            conn = brain_map.connect()
            try:
                return float(pm.equity(conn))
            finally:
                conn.close()
        except Exception:
            pass
    return float(pf.total_value(book, prices or {}))


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
    if proposal.get("sizing") is not None:
        entry["sizing"] = proposal["sizing"]
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


def _rejected_facts(underlying: str, result: dict, proposal: dict = None) -> dict:
    """The refused structure, flattened for the proposal ledger.

    A refusal used to be a sentence and nothing else, so "what would that
    trade have done?" had no answer — there were no strikes to price. This
    carries the legs the engine actually built. It is OBSERVABILITY ONLY:
    nothing on the trading path reads it, and it never raises, because a
    telemetry helper that can break a refusal path would be strictly worse
    than no telemetry at all.

    `lots` is deliberately absent for a size-refused trade: the engine
    computed zero lots, and the ghost tracker prices ONE lot and says so
    rather than inventing a size the sizer never authorised.

    EXPIRY LIVES ON THE SPREAD, not on the proposal (2026-08-11 fix). A
    built proposal carries it as `proposal["spread"]["expiry"]` — there is
    no top-level `expiry` key — so gate-stage refusals (exposure #68,
    margin) wrote `expiry: None` and their ghosts could never find a
    chain. It cost every NIFTY BANK ghost on 08-10: five rows, all
    `no captured chain for expiry None`. Build-stage refusals were fine
    because `build_proposal` returns its own `expiry`, which is why the
    hole looked underlying-specific rather than stage-specific."""
    try:
        spread = (proposal or {}).get("spread") or result.get("rejected_spread")
        if not spread:
            return {}
        return {
            "strategy": spread.get("strategy"),
            "direction": spread.get("direction"),
            "legs": spread.get("legs"),
            "lot_size": spread.get("lot_size"),
            "max_loss": spread.get("max_loss"),
            "max_profit": spread.get("max_profit"),
            "net_credit": spread.get("net_credit"),
            "net_debit": spread.get("net_debit"),
            "lots": (proposal or {}).get("lots"),
            "expiry": (spread.get("expiry")
                       or (proposal or {}).get("expiry")
                       or result.get("expiry")),
            "spot": (spread.get("entry_spot")
                     or (proposal or {}).get("spot")
                     or result.get("spot")),
            "view": result.get("view"),
        }
    except Exception:
        return {}


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
        # `rejected` rides along for the proposal ledger / ghost tracker
        # (2026-08-07). Additive only — every caller reads `proposed`,
        # `reason` and `entry`, and none of them looks at this.
        return {"proposed": False, "reason": result["reason"], "entry": None,
                "rejected": _rejected_facts(underlying, result)}
    p = result["proposal"]
    # Decision #68: one open position per underlying+direction. Sits
    # BEFORE enrichment, the evidence stamp and the margin gate — a
    # blocked duplicate costs nothing downstream and never locks margin.
    # FIRM-WIDE since 10-05 (Architect ruling 2, audit F09): with no
    # entries/conn injected the gate reads the journal AND brain_map.db
    # (mode=ro), so a PAPER_2L_LIVE position or an orphaned shadow lock
    # that outlived its primary still fills the slot. This is the gate's
    # only caller (market_loop reaches it through run_headless).
    # Sandbox books (simulator / tests / what-ifs) are their own worlds —
    # exempt, same rule as the margin gate below. Fail-OPEN by hard rule.
    if "book" not in state:
        from src import exposure_gate
        allowed, exp_reason = exposure_gate.gate_entry(
            p, notify_fn=_notify_discord)
        if not allowed:
            return {"proposed": False, "reason": exp_reason, "entry": None,
                    "rejected": _rejected_facts(underlying, result, p)}
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
            return {"proposed": False, "reason": gate_reason, "entry": None,
                    "rejected": _rejected_facts(underlying, result, p)}
        # DUAL PAPER TREASURY (decision #102): the SAME signal, judged again
        # against the Rs.2L stress-test account — its own sizing, its own
        # margin, its own halts. Records only; the primary decision above
        # is already made. Stamped on the row so "approved for 10L, refused
        # for 2L" is readable per trade. Fail-open inside the seam.
        entry["accounts"] = _judge_shadow_accounts(entry["short_id"], p,
                                                   risk_pct=bp_extras.get("risk_pct"))
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
        # margin_blocked, inside_exit_window, exposure_blocked (Fix G) — the
        # entry stays pending for a human; report honestly instead of
        # pretending the auto-approval happened. Tried ONCE per entry: no
        # loop re-offers a pending entry to auto-approve.
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

    Returns {"status": "approved"|"rejected"|"not_found"|"already_resolved"
             |"margin_blocked"|"inside_exit_window"|"exposure_blocked",
             "entry": dict-or-None, "reason": str (the three refusals only),
             "halt": the halt's event name (a margin_blocked an entry halt
             caused — audit F13, L3 review — only)}.

    D1 (decision #122): the whole decision runs under the journal lock on
    the row read FRESH inside it, and writes THAT ROW alone — two taps (or
    a tap racing auto-approve) cannot both approve one entry, a tracker
    resolution cannot be reverted by a stale copy, and a rotation stamp
    this approval writes on the EVICTED trade's row survives. Discord
    notes go out after the lock is released.

    F18 (audit, 2026-10-06): an APPROVAL of an entry already inside its
    forced pre-expiry exit window is refused ("inside_exit_window") for
    every account — nothing journaled, no ticket, no live position, no lock
    re-taken; the entry stays pending and any lock it still holds is
    released by the D7 15:30 sweep. A rejection is never refused. The
    approval's date is `_today()` (IST).

    Fix G (Architect ruling, 2026-10-06): an APPROVAL is refused
    ("exposure_blocked") when another position on the entry's underlying +
    direction is open anywhere in the firm (decision #68, firm-wide since
    #127) — e.g. a second pending entry approved first, or a PAPER_2L_LIVE
    position opened while this one waited. Same shape as the two refusals
    above: nothing journaled, no ticket, no lock touched, left pending. One
    ledger line per entry per day; an AUTO-approval's block is announced on
    Discord once per entry per day (a human's tap gets its door's answer)."""
    today = _today()
    if human:
        # Phase 3 (§6.6): any human decision — button, CLI, either verdict
        # — is the pulse that keeps full autonomy armed. Recorded before
        # the lookup so even a not_found tap counts as presence.
        try:
            from src import human_pulse
            human_pulse.touch("decide_pending")
        except Exception:
            pass
    # F18 + Fix G: an approval the exit-window gate or the #68 re-check is
    # going to refuse needs no quotes — skip both network prefetches. Only
    # an optimisation: both verdicts are made again inside the lock, on the
    # fresh row, and only those decide.
    fetch = approve and not _prefetch_sees_refusal(trade_id, today)
    # The live arm's re-quote is a Dhan chain call: made HERE, before the
    # journal lock, so no other writer waits on the network (#122 panel).
    prefetched = _prefetch_live_requote(trade_id) if fetch else None
    # #123 (Chunk 1 leftover): the rotation arm's marks and the eviction
    # candidate's chain quotes are fetched here too, before the lock; inside
    # it the eviction only re-verifies on these quotes (a candidate that
    # changed meanwhile has none, so it is simply not evicted).
    rotation = _prefetch_rotation(trade_id) if fetch else None
    # lows residual B2 (cross-batch critic): a card fired from inside the lock
    # (a refused live fill's review card, the F13 held-lock halt cards) is
    # queued and sent only once the journal lock is released (#122: no
    # network inside it).
    from src import notifier
    with notifier.deferred_broadcasts():
        with journal.locked():
            verdict = _decide_pending_locked(trade_id, approve, why, live_requote=prefetched,
                                             rotation=rotation, today=today)
    # Fix G: the #68 block's one card, after the lock (D1). Only for an
    # AUTO-approval — its proposal card said "being journaled as APPROVED"
    # and nobody is looking at a reply — and only on the first block of
    # this entry today (`announce`, from the ledger's once-per-entry-per-day
    # memory), so a retried approval never repeats it.
    announce = verdict.pop("announce", False)
    if verdict["status"] == EXPOSURE_BLOCKED and announce and not human:
        try:
            from src import exposure_gate
            _notify_discord(exposure_gate.approval_block_note(verdict["entry"],
                                                              verdict.get("reason")))
        except Exception as _note_err:
            print(f"  (exposure-block note skipped: {_note_err})")
    if verdict["status"] not in ("approved", "rejected"):
        return verdict
    target, decision = verdict["entry"], verdict["status"]
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


INSIDE_EXIT_WINDOW = "inside_exit_window"
# Fix G (Architect ruling 2026-10-06): the #68 slot re-checked at approval
EXPOSURE_BLOCKED = "exposure_blocked"
# Phase 6J: the capital layer could not grant the approval its margin
MARGIN_BLOCKED = "margin_blocked"
# Chunk 4 B1: an approval of an earlier session's proposal would fill at proposal-day prices
STALE_PROPOSAL = "stale_proposal"
# Every status an APPROVAL can be refused with: nothing journaled, the entry
# is still pending, so a reject (or a later approve) still works. The doors
# (api_server's 409, the Discord bot's kept buttons) read THIS set, so a new
# refusal cannot be reported as a decision again (Chunk 2 close-out: a
# margin block used to answer 200 ok:true and retire the buttons).
APPROVAL_REFUSALS = (INSIDE_EXIT_WINDOW, EXPOSURE_BLOCKED, MARGIN_BLOCKED, STALE_PROPOSAL)
_IST = timezone(timedelta(hours=5, minutes=30))


def _stale_refusal(target: dict, today: date) -> str | None:
    """B1: a proposal from an earlier session is refused (its premiums are that day's)."""
    from src.config import STALE_APPROVAL_MAX_DAYS
    if STALE_APPROVAL_MAX_DAYS is None:
        return None
    try:
        age = (today - date.fromisoformat(str(target.get("date")))).days
    except (TypeError, ValueError):
        return None
    if age <= STALE_APPROVAL_MAX_DAYS:
        return None
    return (f"proposed {target.get('date')}, {age}d ago: an approval would fill at proposal-day "
            f"premiums (audit Chunk 4 B1) — reject it, or raise stale_approval_max_days")


def _today() -> date:
    """The approval's date in IST — the exchange's day and the live arm's
    clock (live_pricer._now), whatever the host's zone. The test seam for
    every door into decide_pending (CLI, API bridge, auto-approve)."""
    return datetime.now(_IST).date()


def _exit_window_refusal(entry: dict, today: date) -> str | None:
    """Why `entry` may not be APPROVED on `today` (audit F18), or None.

    An entry already inside its forced pre-expiry exit window — the ONE
    predicate, `plan_tracker.in_forced_exit_window`: 2 calendar days for an
    index, 7 for a stock option, or the last NSE session before expiry — is
    exited by the live arm's next 60-s tick and by the primary's next close.
    Approving it buys two bid/ask crossings and frictions on a trade nobody
    can hold, booked as a strategy outcome. A row stays approvable that
    long because the tracker resolves a pending row only on a completed
    daily bar, served the next morning.

    No spread expiry -> None: not an option spread, no window to be inside
    (the tracker cannot track such a row either). An expiry that cannot be
    read -> refused (fail-closed): the window cannot be judged, and an
    unknown expiry on an option is not something to approve on a guess."""
    expiry = (entry.get("spread") or {}).get("expiry")
    if not expiry:
        return None
    from src import plan_tracker
    ticker = entry.get("ticker")
    try:
        days = (date.fromisoformat(str(expiry)[:10]) - today).days
        inside = plan_tracker.in_forced_exit_window(ticker, expiry, today)
    except (TypeError, ValueError):
        return (f"expiry {expiry!r} is unreadable — the forced-exit window cannot "
                "be judged, so the approval is refused (fail-closed)")
    if not inside:
        return None
    return (f"{days}d to the {expiry} expiry on {today.isoformat()} — already inside "
            f"the forced pre-expiry exit window ({forced_exit_days_for(ticker)}d, or "
            "the last session before expiry), so every account would exit it on its "
            "next tick or close: a guaranteed round trip (audit F18). Left pending; any "
            "margin lock it still holds is released by the 15:30 sweep.")


def _prefetch_sees_refusal(trade_id: str, today: date) -> bool:
    """True when the pending row, read OUTSIDE the lock, is already refused
    by `_exit_window_refusal` (F18) or by the #68 approval re-check (Fix G,
    `exposure_gate.check_approval` — pure and quiet: no ledger line, no
    card) — decide_pending then skips its network prefetches. It never
    decides: if the row changed before the lock (the conflicting position
    exited), the approval proceeds as an un-prefetched one — the live arm
    re-quotes inside, rotation evicts nothing. Never raises: any doubt
    means fetch as before."""
    try:
        e = journal.get_entry(trade_id)
        if not e or e.get("decision") != "pending_approval":
            return False
        if _exit_window_refusal(e, today) is not None:
            return True
        from src import exposure_gate
        return not exposure_gate.check_approval(e, today=today, quiet=True)[0]
    except Exception:
        return False


def _prefetch_live_requote(trade_id: str):
    """The live arm's crossed re-quote for this pending entry, fetched
    OUTSIDE the journal lock; None when the arm is off, the row is not a
    pending spread with every leg quoted, or the fetch raised (the arm then
    re-quotes inside, as before). Never raises."""
    try:
        from src import portfolio_manager as pm
        if not pm.live_account_enabled():
            return None
        e = journal.get_entry(trade_id)
        if (not e or e.get("decision") != "pending_approval"
                or not pm._legs_all_quoted(e.get("spread") or {})):
            return None
        from src.execution import live_pricer
        return live_pricer.requote_entry(e, chain_fn=_LIVE_CHAIN_FN)
    except Exception:
        return None


def _prefetch_rotation(trade_id: str):
    """The rotation arm's network work for approving `trade_id`, done
    OUTSIDE the journal lock: {"marks": {ref: rr_left}, "quotes": {ref:
    leg quotes}} for the account's open trades and the one trade
    evaluate_eviction would evict. None when the arm is off, the row is not
    a pending spread, or anything failed (no eviction then). Never raises."""
    try:
        from src import brain_map, plan_tracker, portfolio_manager as pm
        if not pm.rotation_enabled():
            return None
        e = journal.get_entry(trade_id)
        if not e or e.get("decision") != "pending_approval" or not (e.get("spread") or {}).get("legs"):
            return None
        spread = e["spread"]
        vix = (e.get("receipt") or {}).get("vix")
        conn = brain_map.connect()
        try:
            pm.ensure_accounts_schema(conn)
            marks, quotes = {}, {}
            for account in pm.ROTATION_ACCOUNTS:
                refs = [r[0] for r in conn.execute(
                    "SELECT journal_ref FROM paper_margin_locks WHERE account_id = ? AND "
                    "released_at IS NULL", (account,)).fetchall()]
                if not refs:
                    continue
                m = plan_tracker.rotation_marks(refs)
                marks.update(m)
                need = pm.required_margin_for({"spread": dict(spread, lots=1), "vix": vix})
                v = pm.evaluate_eviction(conn, account, spread, m, need)
                weak = (v.get("weakest") or {}).get("journal_ref")
                if v.get("evict") and weak:
                    row = journal.get_entry(weak)
                    if row:
                        from src.live_bridge import _leg_quotes_for
                        q = _leg_quotes_for(row)
                        if q:
                            quotes[weak] = q
            return {"marks": marks, "quotes": quotes}
        finally:
            if not _PAPER_VENUE_KEEP_CONN:      # same seam as the venue step
                conn.close()
    except Exception as exc:
        print(f"  (rotation prefetch skipped: {exc})")
        return None


def _rotation_fns(rotation):
    """(marks_fn, evict_fn) that use ONLY the prefetched data — no network
    inside the journal lock. No prefetch -> no marks -> no eviction."""
    from src import plan_tracker
    data = rotation or {"marks": {}, "quotes": {}}

    def marks_fn(refs):
        return {r: data["marks"][r] for r in refs if r in data["marks"]}

    def evict_fn(conn, account, ref, lots, max_rr_left, reason, need_rs=None):
        return plan_tracker.evict_for_rotation(
            conn, account, ref, lots, max_rr_left=max_rr_left, reason=reason, need_rs=need_rs,
            quotes_fn=lambda e: data["quotes"].get(e.get("short_id")))
    return marks_fn, evict_fn


def _decide_pending_locked(trade_id: str, approve: bool, why: str,
                           live_requote: dict = None, rotation: dict = None,
                           today: date = None) -> dict:
    """decide_pending's body — caller holds the journal lock."""
    import copy
    target = journal.get_entry(trade_id)
    if target is None or target.get("decision") != "pending_approval":
        return {"status": "not_found", "entry": None}
    if target.get("outcome"):
        return {"status": "already_resolved", "entry": target}

    if approve:
        today = today or _today()
        # F18 (audit): FIRST, before any account is judged or any lock is
        # re-taken — an entry inside its forced-exit window is refused for
        # EVERY account at once, so no ticket is issued, the live arm never
        # re-quotes into a position its next tick closes, and nothing is
        # written and no lock is re-taken: the row stays pending, any lock
        # it still holds is released by the D7 15:30 sweep, and the tracker
        # still scores it hypothetically.
        refusal = _exit_window_refusal(target, today)
        if refusal:
            return {"status": INSIDE_EXIT_WINDOW, "entry": target, "reason": refusal}
        stale = _stale_refusal(target, today)
        if stale:
            return {"status": STALE_PROPOSAL, "entry": target, "reason": stale}

        # Fix G (Architect ruling 2026-10-06): SECOND — the #68 slot,
        # re-checked FIRM-WIDE on this fresh row before margin, the shadow
        # accounts, the venue or the journal are touched. The proposal gate
        # never counts PENDING rows, so two pending entries on one
        # underlying+direction could both be approved, and a PAPER_2L_LIVE
        # position can open while an entry waits; either now blocks it,
        # exactly like a margin block: left pending, nothing journaled, no
        # ticket, no live position, no broadcast — and no lock re-taken
        # (a D7-expired lock stays expired; one still held is released by
        # the 15:30 sweep). The entry's own ref never conflicts with
        # itself. Local reads only (journal + brain_map.db mode=ro) — no
        # network inside the lock. Fail-OPEN like the proposal gate: an
        # unreadable firm view judges on the primary journal alone (note
        # printed), any other failure allows. No sandbox exemption here:
        # decide_pending only knows the REAL book (it locks real margin and
        # fills on the real venue — see run_headless's auto-approve note),
        # so whatever produced the row, approving it opens a real-book
        # position. Rejections never reach this.
        from src import exposure_gate
        allowed, exp_reason, first_today = exposure_gate.gate_approval(target, today=today)
        if not allowed:
            return {"status": EXPOSURE_BLOCKED, "entry": target, "reason": exp_reason,
                    "announce": first_today}

        # Phase 6J: approval is the moment a trade is ACCEPTED, so the
        # capital layer must grant its margin first (idempotent when the
        # headless gate already locked it at proposal time — but that held
        # lock still passes the account's halt list, audit F13: a ruin
        # latch or daily breaker that tripped since the proposal refuses
        # the approval with the halt named; a lock that EXPIRED unapproved
        # at 15:30 — D7 — is taken again on today's cash). A margin-blocked
        # approval leaves the entry pending — nothing is journaled,
        # broadcast, or settled. A HALT's refusal carries the halt's event
        # name (`halt`, F13 L3 review) so the doors can say "the halt is
        # up", not "margin frees up".
        spread = target.get("spread") or {}
        proposed = dict(target.get("accounts") or {})   # the proposal-time verdicts (F14 below)
        per_lot = (spread.get("margin") or {}).get("total_margin")
        if per_lot is not None:
            from src import portfolio_manager as pm
            # Same reservation math as the headless gate (incl. the
            # entry-time VIX-stress factor) — the entry's receipt carries
            # the VIX it was born under; None degrades to factor 1.0.
            required = pm.required_margin_for(
                {"spread": spread,
                 "vix": (target.get("receipt") or {}).get("vix")})
            gate = pm.gate_headless_entry(trade_id, required)
            allowed, gate_reason = gate
            if not allowed:
                blocked = {"status": MARGIN_BLOCKED, "entry": target, "reason": gate_reason}
                if getattr(gate, "halt", None):
                    blocked["halt"] = gate.halt
                return blocked
            # #102: approval is the acceptance moment for the shadow
            # accounts too (idempotent re-request on an active lock, which
            # still passes the account's own halts — F13: a halted shadow is
            # refused, its lock released at zero; a proposal-time refusal is
            # re-judged on today's cash, on the risk budget the proposal
            # was sized on — F12 L3 review). D6
            # (#122): this is the ONLY judgement where the rotation account
            # may evict — the entry is being accepted, not merely proposed.
            marks_fn, evict_fn = _rotation_fns(rotation)
            target["accounts"] = _judge_shadow_accounts(
                trade_id, {"spread": spread, "lots": spread.get("lots"),
                           "vix": (target.get("receipt") or {}).get("vix")},
                risk_pct=_proposal_risk_pct(target),
                allow_rotation=True, marks_fn=marks_fn, evict_fn=evict_fn)
        # Audit F14 (L3 review) + L3 residual (a): a shadow account (the
        # live arm, PAPER_2L, PAPER_2L_ROT) switched OFF since the proposal
        # takes nothing now — refused by name on the row, its proposal-time
        # lock released at zero here (not held until the primary exits, and
        # never settled at the primary's scaled P&L); a proposal-time
        # verdict that was not an approval stays on the row as proposed.
        _switched_off_refusals(target, proposed)

    decision = "approved" if approve else "rejected"
    target["decision"] = decision
    target["decided_at"] = datetime.now(_IST).replace(tzinfo=None).isoformat(timespec="seconds")
    target["why"] = (why or "").strip() or "(no reason given)"
    if approve:
        # Phase M2 (decision #101): an approved ENTRY becomes an Order
        # Ticket the PAPER venue fills leg by leg — the legacy "the journal
        # line IS the fill" model is replaced for new entries when the
        # flag is on. Runs BEFORE the write so the venue's fill prices
        # land on the row the tracker will read. Fail-open: a venue error
        # leaves the legacy fill in place and says so; nothing here touches
        # margin or the exit path.
        target["execution"] = _execute_paper_entry(target, live_requote=live_requote)
    final = copy.deepcopy(target)

    def _replace(e):
        e.clear()
        e.update(final)
    journal.update_entry(trade_id, _replace)
    if not approve:
        # Phase 6G: a human rejection frees the entry's margin lock right
        # away (zero P&L — the trade never happened). Safe no-op if the
        # entry predates the capital layer.
        from src import portfolio_manager as pm
        pm.release_entry(trade_id, 0.0)
    return {"status": decision, "entry": final}


def _proposal_risk_pct(entry: dict):
    """The risk budget (% of equity) the proposal was SIZED on, or None (the
    account default, ACCOUNT_RISK_PER_TRADE_PCT). run_headless sizes on
    vol_bridge's override (`state['vol_overrides']['risk_pct']` — 0.7 x the
    base under Expansion) and freezes it on the row as
    `receipt.vol_overrides.risk_pct`. Audit F12 (L3 review): every
    approval-time sizing — the shadow accounts' re-judgement and the live
    arm's re-size on its crossed re-quote — runs on THAT budget, never the
    quiet-regime default. Read as stored: a value that is not a number
    fails in the sizer, whose callers refuse by name (never a guessed 2%)."""
    return (((entry or {}).get("receipt") or {}).get("vol_overrides") or {}).get("risk_pct")


def _judge_shadow_accounts(journal_ref: str, proposal: dict, risk_pct=None,
                           allow_rotation: bool = False, marks_fn=None,
                           evict_fn=None) -> dict:
    """The dual-treasury seam (#102): {account_id: {status, lots, margin_rs,
    reason}} from `portfolio_manager.evaluate_shadow_accounts`, {} when the
    switch is off. Never raises — a broken shadow ledger cannot touch the
    primary path. Each refusal is printed so the session log reads it.
    `allow_rotation` is True only for the APPROVAL judgement (D6, #122)."""
    try:
        from src import brain_map, portfolio_manager as pm
        if not pm.shadow_accounts_enabled():
            return {}
        conn = brain_map.connect()
        try:
            verdicts = pm.evaluate_shadow_accounts(journal_ref, proposal, conn=conn,
                                                   risk_pct=risk_pct,
                                                   allow_rotation=allow_rotation,
                                                   marks_fn=marks_fn, evict_fn=evict_fn)
        finally:
            if not _PAPER_VENUE_KEEP_CONN:      # same seam as the venue step
                conn.close()
    except Exception as e:
        print(f"  (shadow accounts unavailable: {e})")
        return {}
    for acct, v in (verdicts or {}).items():
        if v.get("status") == "approved":
            if v.get("reason") == pm.HELD_LOCK_REASON:
                continue    # the proposal-time lock, re-confirmed at approval: printed when taken
            print(f"  [{acct}] {journal_ref}: approved {v['lots']} lot(s), "
                  f"margin Rs.{float(v['margin_rs'] or 0):,.0f}")
        elif v.get("status") == LIVE_ALREADY_OPEN:
            print(f"  [{acct}] {journal_ref}: NO SECOND ENTRY — {v.get('reason')}")
        else:
            print(f"  [{acct}] {journal_ref}: REJECTED — {v.get('reason')}")
    return verdicts or {}


_PAPER_VENUE_KEEP_CONN = False     # test seam: a shared in-memory conn must survive the call
_LIVE_CHAIN_FN = None              # test seam: the live arm's chain source (None = dhan, muzzled under pytest)


# L3 review: the live arm's verdict when a re-approval finds this ref
# already opened (a recorded row, or an entry ticket FILLED awaiting its
# repair) — that position stands on the lock behind it; only a SECOND entry
# ticket is refused, so the verdict must not read 'rejected'. The constant
# is portfolio_manager's (L4 review: its F13 halt path stamps it too),
# imported at the top of this module as LIVE_ALREADY_OPEN.


def _lock_text(rel: dict) -> str:
    """How pm.release_unopened_lock left a refused account's lock, for its event."""
    return ("released at zero" if rel.get("released") else
            "not held" if rel.get("reason") == "no active lock for this ref" else
            f"kept ({rel.get('reason')})")


def _live_refuse(conn, acct: str, entry: dict, why: str, status: str = "rejected") -> None:
    """The live arm (#120) declines this entry at approval: verdict rejected,
    its proposal-time lock released at zero, one named event. Through
    pm.release_unopened_lock: a lock that backs a recorded or FILLED live
    position (L2 residual (b), F15) is kept, and the event says so.
    `status` LIVE_ALREADY_OPEN (L3 review): the ref was already opened —
    the verdict is stamped `already_open`, not 'rejected', with the lots
    and margin of the lock that backs that position. L3 residual (b): the
    same holds whatever the refusal's cause (the arm or the venue switched
    off) — a lock KEPT because it backs a recorded or FILLED position is an
    open position, never a 'rejected' verdict with 0 lots (the dashboard
    and the A/B read the row)."""
    from src import portfolio_manager as pm
    ref = entry.get("short_id")
    rel = None
    try:
        rel = pm.release_unopened_lock(conn, acct, ref)
        pm.paper_log_event(conn, acct, "live_entry_refused", ref, f"{why} — lock {_lock_text(rel)}")
    except Exception as e:
        print(f"  [{acct}] {ref}: refusal bookkeeping failed ({e})")
    if status != LIVE_ALREADY_OPEN and (rel or {}).get("backs_position"):
        status, why = LIVE_ALREADY_OPEN, f"{why}; {rel.get('reason')}"
    if (rel or {}).get("closed_position"):
        # lows residual B2: the arm traded this ref and already CLOSED it —
        # its verdict (stamped closed with its P&L) stands; never relabelled
        # 'already_open' (a holder) nor 'rejected' (never traded)
        print(f"  [{acct}] {ref}: NO SECOND ENTRY — {why}; {rel.get('reason')}")
        return
    if isinstance(entry.get("accounts"), dict):
        if status == LIVE_ALREADY_OPEN:
            verdict = dict(entry["accounts"].get(acct) or {}, status=status, reason=f"no second live entry: {why}")
            try:
                held = pm._active_shadow_lock(conn, acct, ref)
            except Exception:
                held = None                      # unreadable: the verdict keeps what it had
            if held is not None:
                verdict.update(margin_rs=held[0], lots=held[1])
            entry["accounts"][acct] = verdict
        else:
            entry["accounts"][acct] = dict(entry["accounts"].get(acct) or {}, status="rejected", lots=0,
                                           margin_rs=None, ticket_id=None, reason=f"live entry refused: {why}")
    print(f"  [{acct}] {ref}: {'NO SECOND ENTRY' if status == LIVE_ALREADY_OPEN else 'REFUSED'} "
          f"at approval — {why}")


# L3 residual (a): the event of a model-priced shadow account (PAPER_2L /
# PAPER_2L_ROT) refused at approval because its switch went off after the
# proposal (the live arm's own refusals write `live_entry_refused`).
EVENT_SWITCHED_OFF = "entry_refused_switched_off"


def _shadow_refuse(conn, acct: str, entry: dict, proposed: dict, why: str) -> None:
    """L3 residual (a): a model-priced shadow account declines this entry
    at approval. Its proposal-time verdict stays on the row as REFUSED
    (status rejected, 0 lots, no margin, no ticket; the lots it was
    proposed for kept as `proposed_lots`), its lock released at zero
    through pm.release_unopened_lock, one named event."""
    from src import portfolio_manager as pm
    ref = entry.get("short_id")
    entry["accounts"][acct] = dict(proposed or {}, status="rejected", lots=0, margin_rs=None, ticket_id=None,
                                   proposed_lots=(proposed or {}).get("lots"),
                                   reason=f"refused at approval: {why}")
    try:
        rel = pm.release_unopened_lock(conn, acct, ref)
        pm.paper_log_event(conn, acct, EVENT_SWITCHED_OFF, ref, f"{why} — lock {_lock_text(rel)}")
    except Exception as e:
        print(f"  [{acct}] {ref}: refusal bookkeeping failed ({e})")
    print(f"  [{acct}] {ref}: REFUSED at approval — {why}")


def _live_entry_rules(conn, acct: str, entry: dict, rq: dict, lots: int) -> int | None:
    """Audit F12 (policy default, 2026-10-06 — the owner may revise): the
    live arm's crossed re-quote is judged by the desk's OWN entry rules
    before it opens — at approval the arm enters at that re-quote, which can
    sit far from the proposal's prices after an hours-long human gap.
      (a) the #98 R:R floor on the RE-QUOTED structure, through the
          proposer's own gate (strategy.reward_risk_gate, its own floors):
          below it the arm abstains for this entry — named, its lock
          released at zero, a `live_entry_refused` event;
      (b) the #106 risk budget on the CROSSED max loss, through the shadow
          accounts' one sizing door (pm.size_for_account, the account's own
          equity, risk only — its held lock is already out of its liquid
          cash, and fewer lots need less margin): lots re-sized DOWN, never
          above the lots it was approved for; #106's 1-lot floor is kept
          and recorded on the verdict (`requote_sizing`); the lock shrinks
          with the lots (a `live_entry_resized` event). The budget is the
          one the proposal was sized on (`_proposal_risk_pct`: vol_bridge's
          Expansion override, else the default — L3 review).
    The primary and the other shadows are untouched. Returns the lots to
    ticket, or None (refused). A failure abstains (refused, named): this
    runs before ANY ticket is issued, and must never take the primary's
    venue run down with it."""
    try:
        return _live_entry_rules_body(conn, acct, entry, rq, lots)
    except Exception as e:
        _live_refuse(conn, acct, entry, f"the desk's entry rules could not be applied to the re-quote ({e})")
        return None


def _live_entry_rules_body(conn, acct: str, entry: dict, rq: dict, lots: int) -> int | None:
    from src import portfolio_manager as pm
    ref = entry.get("short_id")
    spread = entry["spread"]
    lot = int(spread["lot_size"])
    crossed = {"strategy": spread.get("strategy"),
               "max_loss": round(float(rq["max_loss_ps"]) * lot, 2),
               "max_profit": round(float(rq["max_profit_ps"]) * lot, 2)}
    ok, rr, _floor, why = reward_risk_gate(crossed)
    if not ok:
        _live_refuse(conn, acct, entry, f"the crossed re-quote (net {float(rq['entry_mark_ps']):+.2f}/share) "
                                        f"fails the desk's R:R floor — {why} (audit F12)")
        return None
    sized = pm.size_for_account(conn, acct, dict(crossed, margin={"total_margin": 0}),
                                risk_pct=_proposal_risk_pct(entry))
    new = min(int(lots), int(sized["lots"]))
    if new <= 0:                 # unreachable past the gate (a measurable loss sizes >= 1)
        _live_refuse(conn, acct, entry, f"re-sizing on the crossed max loss refused: {sized['reason']}")
        return None
    record = {"lots_approved": int(lots), "lots": new, "crossed_max_loss_rs": crossed["max_loss"],
              "reward_risk": rr, "by_risk": sized["by_risk"], "risk_capacity_rs": sized["risk_capacity_rs"],
              "risk_at_lots_rs": round(new * crossed["max_loss"], 2),
              "floor_applied": bool(sized["floor_applied"] and new == 1),
              "reason": sized["reason"] if new == sized["lots"] else "kept at the approved lots (below the budget)"}
    verdict = (entry.get("accounts") or {}).get(acct)
    if new < int(lots):
        held = pm._active_shadow_lock(conn, acct, ref)
        if held is not None:
            margin = round(held[0] * new / held[1], 2) if held[1] else held[0]
            if pm.paper_resize_lock(conn, acct, ref, new, margin):
                record["margin_rs"] = margin
                pm.paper_log_event(conn, acct, "live_entry_resized", ref,
                                   f"crossed max loss Rs.{crossed['max_loss']:,.2f}/lot: {lots} -> {new} "
                                   f"lot(s) on the {sized['risk_pct']:g}% budget Rs.{sized['risk_capacity_rs']:,.2f}; "
                                   f"lock Rs.{held[0]:,.2f} -> Rs.{margin:,.2f} (audit F12)")
                if isinstance(verdict, dict):
                    verdict["margin_rs"] = margin
        print(f"  [{acct}] {ref}: re-sized {lots} -> {new} lot(s) on the crossed max loss "
              f"Rs.{crossed['max_loss']:,.2f}/lot (audit F12)")
    if isinstance(verdict, dict):
        verdict["lots"] = new
        verdict["requote_sizing"] = record
    return new


def _live_already_recorded(conn, acct: str, entry: dict) -> bool:
    """L2 residual (b): True (and refused, named, the lock KEPT) when this
    (account, ref) was already opened — a live position row exists (a
    kept-then-repaired position on a still-pending entry being approved
    again), or an entry ticket for it already FILLED and its row is still
    to be repaired from that ticket (F15). The table's key is that pair: a
    second entry ticket would be a second fill with nowhere to be recorded.
    Checked BEFORE any ticket is issued. An opened ref's verdict reads
    LIVE_ALREADY_OPEN (its position stands; L3 review). An unreadable state
    refuses too — 'rejected', named as unreadable, never as 'already
    recorded' (L3 review) — never a second fill on a guess."""
    from src.execution import live_pricer
    ref = entry.get("short_id")
    try:
        state = live_pricer.position_state(conn, acct, ref)
        filled = None if state else live_pricer.filled_entry_ticket(conn, acct, ref)
    except Exception as e:
        _live_refuse(conn, acct, entry, f"this entry's live position state could not be read ({e}) — "
                                        "no entry ticket on a guess")
        return True
    if state is not None:
        why = f"a live position for this entry is already recorded ({state})"
    elif filled:
        why = (f"entry ticket {filled} for this entry already FILLED (its position row is repaired from "
               "that ticket by the next live tick)")
    else:
        return False
    _live_refuse(conn, acct, entry, f"{why} — a ref is opened once; no second entry ticket",
                 status=LIVE_ALREADY_OPEN)
    return True


def _live_requote(conn, acct: str, entry: dict, prefetched: dict = None) -> dict | None:
    """Fresh crossed limits for the live arm, or None (refused + released).
    `prefetched` = decide_pending's re-quote made before the journal lock."""
    from src.execution import live_pricer
    try:
        rq = prefetched if prefetched is not None else live_pricer.requote_entry(
            entry, chain_fn=_LIVE_CHAIN_FN)
    except Exception as e:
        rq = {"ok": False, "reason": f"requote failed ({e})"}
    if not rq.get("ok"):
        _live_refuse(conn, acct, entry, rq.get("reason") or "no live quotes")
        return None
    return rq


def _live_after_fill(conn, acct: str, entry: dict, stid: str, sview: dict, quote_ts: str) -> None:
    """A FILLED live ticket becomes an open live position; anything else is
    refused (ticket cancelled, lock released at zero)."""
    from src import oms
    from src.execution import live_pricer
    try:
        if sview.get("status") == oms.FILLED:
            try:
                opened = live_pricer.open_position(conn, acct, entry, sview, quote_ts=quote_ts)
            except live_pricer.LiveEntryRefused as refused:
                # the door that writes refused a FILLED entry (F11 fills that
                # invert the structure; a row already recorded for the ref):
                # nothing books it on its own — lock kept, named, one card
                detail = live_pricer.note_refused_fill(conn, acct, entry.get("short_id"), stid, str(refused))
                if isinstance(entry.get("accounts"), dict) and acct in entry["accounts"]:
                    entry["accounts"][acct].update(status="refused_after_fill", reason=detail)
                print(f"  [{acct}] {entry.get('short_id')}: {detail}")
                return
            if isinstance(entry.get("accounts"), dict) and acct in entry["accounts"]:
                entry["accounts"][acct].update(entry_quote_ts=quote_ts,
                                               entry_mark_ps=opened["entry_mark_ps"],
                                               max_loss_ps=opened["max_loss_ps"],
                                               max_profit_ps=opened["max_profit_ps"])
            print(f"  [{acct}] {entry.get('short_id')}: live position opened at crossed quotes "
                  f"(d {opened['entry_mark_ps']:+.2f}/share, quoted {quote_ts})")
            return
        try:
            oms.cancel_ticket(conn, stid, "live account: entry not filled")
        except Exception:
            pass
        _live_refuse(conn, acct, entry, f"entry ticket {stid} not filled ({sview.get('status')})")
    except Exception as e:
        # the ticket IS filled: keep the lock, say so; live_pricer.tick's
        # repair pass opens the position from the ticket on its next run
        from src import portfolio_manager as pm
        try:
            pm.paper_log_event(conn, acct, "live_position_unrecorded", entry.get("short_id"),
                               f"entry ticket {stid} filled but the position row failed ({e}) — "
                               "lock kept; repaired from the ticket on the next live tick")
        except Exception:
            pass
        print(f"  [{acct}] {entry.get('short_id')}: position row failed ({e}) — lock kept for repair")


def _live_venue_off(entry: dict, conn=None) -> None:
    """Audit F14 (b): with the paper venue OFF the approval opens no
    ticket, so the live arm — which only ever opens on a real crossed fill —
    takes no position. Its proposal-time lock used to stay held until the
    primary exited (then released at zero); it is released NOW, named, and
    its verdict says why. Fail-open: a failure leaves the old behaviour."""
    try:
        from src import portfolio_manager as pm
        live = [a for a, v in (entry.get("accounts") or {}).items()
                if a in pm.LIVE_ACCOUNTS and (v or {}).get("status") == "approved"]
        _live_refuse_each(entry, live, "the paper venue is off (paper_venue_enabled false) — the live arm "
                                       "opens only on a real crossed fill, so it takes no position (audit F14)",
                          conn)
    except Exception as e:
        print(f"  (live account: venue-off release skipped: {e})")


def _switch_of(acct: str) -> str:
    """The config key that switches `acct` off right now (the 2L
    experiment's own switch first: it turns every shadow arm off)."""
    from src import portfolio_manager as pm
    if not pm.shadow_accounts_enabled():
        return "paper_2l_account_enabled"
    if acct in pm.LIVE_ACCOUNTS:
        return "paper_2l_live_account_enabled"
    if acct in pm.ROTATION_ACCOUNTS:
        return "capital_rotation_enabled"
    return "paper_2l_account_enabled"


def _switched_off_refusals(entry: dict, proposed: dict, conn=None) -> None:
    """A shadow account whose SWITCH went off between the proposal and this
    approval. The approval judgement never sees it then (shadow_account_ids
    leaves it out — with the whole 2L experiment off, _judge_shadow_accounts
    returns {}), so its proposal-time verdict was dropped from the row, it
    got no ticket, and the lock it took at proposal stayed held:
      * the live arm (audit F14, L3 review) — capital of an arm that opens
        nothing, held until the primary exited;
      * PAPER_2L / PAPER_2L_ROT (L3 residual (a), the same class) — worse:
        the primary's exit then settled that lock at the SCALED primary
        P&L, booking a trade the account never ticketed.
    Every account `proposed` (the row's proposal-time verdicts) APPROVED
    and switched off now is refused by name: its verdict stays on the row
    as refused, its lock released at zero through pm.release_unopened_lock
    (a lock backing a recorded or FILLED live position is kept, named —
    `already_open`). A proposal-time verdict that was NOT an approval holds
    no lock: it stays on the row exactly as it was proposed — no event, no
    release (L4 review: the judgement used to drop it with the rest). Runs
    whether or not the entry carries a margin field (the judgement does
    not). Fail-open: a failure leaves the old path, and release_shadow_locks
    still settles a model-priced shadow with no approved verdict at zero
    (L3 residual (a)), so it books no P&L either way."""
    try:
        from src import portfolio_manager as pm
        on = set(pm.shadow_account_ids())
        switched = {a: v for a, v in (proposed or {}).items() if a in pm.PAPER_ACCOUNTS and a not in on}
        off = [a for a, v in switched.items() if (v or {}).get("status") == "approved"]
        if not switched:
            return
        if not isinstance(entry.get("accounts"), dict):
            entry["accounts"] = {}
        for acct, v in switched.items():
            if acct not in off:
                entry["accounts"].setdefault(acct, v)     # unchanged: nothing to refuse or release
        if not off:
            return
        own = conn is None
        if own:
            from src import brain_map
            conn = brain_map.connect()
        try:
            for acct in off:
                switch = _switch_of(acct)
                if acct in pm.LIVE_ACCOUNTS:
                    _live_refuse(conn, acct, entry, f"the live arm is switched off ({switch} false) — no new "
                                                    "live entry opens while it is off, so it takes no position "
                                                    "(audit F14)")
                else:
                    _shadow_refuse(conn, acct, entry, proposed[acct],
                                   f"{acct} is switched off since the proposal ({switch} false) — it takes no "
                                   "position, so it books none of this trade's P&L (L3 residual (a))")
        finally:
            if own and not _PAPER_VENUE_KEEP_CONN:
                conn.close()
    except Exception as e:
        print(f"  (shadow accounts: switch-off refusal skipped: {e})")


def _live_refuse_each(entry: dict, accounts: list, why: str, conn=None) -> None:
    """`_live_refuse` for each of `accounts` on one connection (opened here
    when none is given; the test seam keeps a shared one open). Raises —
    its callers fail open."""
    if not accounts:
        return
    own = conn is None
    if own:
        from src import brain_map
        conn = brain_map.connect()
    try:
        for acct in accounts:
            _live_refuse(conn, acct, entry, why)
    finally:
        if own and not _PAPER_VENUE_KEEP_CONN:
            conn.close()


def _execute_paper_entry(entry: dict, conn=None, venue_mod=None, live_requote: dict = None) -> dict:
    """Issue the ticket and run one venue pass for THIS entry (decision
    #101). Returns a small execution record stamped on the journal row:
    {mode, ticket_id, status, filled_legs, error}. `mode` is
    "legacy_instant" when PAPER_VENUE_ENABLED is off or the venue could
    not run — the entry then behaves exactly as before."""
    from src.config import PAPER_VENUE_ENABLED
    record = {"mode": "legacy_instant", "ticket_id": None, "status": None,
              "filled_legs": 0, "error": None}
    if not PAPER_VENUE_ENABLED:
        _live_venue_off(entry, conn)
        return record
    if not (entry.get("spread") or {}).get("legs"):
        return record
    caller_conn = conn
    try:
        from src import brain_map, oms, strategy_router
        venue = venue_mod
        if venue is None:
            from src.execution import paper_venue as venue
        own = conn is None
        if own:
            conn = brain_map.connect()
        try:
            proposal = {"ticker": entry["ticker"], "short_id": entry.get("short_id"),
                        "signal": entry.get("signal"), "spread": entry["spread"]}
            # ONE ticket per paper account that ACCEPTED this entry (#102):
            # the primary first (its lots = the trade), then every shadow
            # account at its own lot count. Same legs, same limits, same
            # venue, same slippage; the venue journals each fill to the
            # account that owns the ticket.
            tickets = [(oms.PRIMARY_ACCOUNT, None)]
            for acct, verdict in (entry.get("accounts") or {}).items():
                if (verdict or {}).get("status") == "approved" and int(verdict.get("lots") or 0) > 0:
                    tickets.append((acct, int(verdict["lots"])))
            # decision #120: the live-quote arm is RE-QUOTED now, at approval
            # (the proposal's quotes may be hours old); its ticket carries the
            # fresh crossed limits, or it declines and releases its lock.
            from src import portfolio_manager as _pm
            live_quotes = {}
            for acct, _lots in list(tickets):
                if acct in _pm.LIVE_ACCOUNTS:
                    # L2 residual (b): never a second entry ticket on a ref
                    # whose live position is already recorded; then the
                    # re-quote, then the desk's own entry rules on it (F12)
                    rq = (None if _live_already_recorded(conn, acct, entry)
                          else _live_requote(conn, acct, entry, prefetched=live_requote))
                    lots_now = None if rq is None else _live_entry_rules(conn, acct, entry, rq, _lots)
                    if lots_now is None:
                        tickets = [t for t in tickets if t[0] != acct]
                    else:
                        tickets = [(a, lots_now if a == acct else n) for a, n in tickets]
                        live_quotes[acct] = rq
            tids = {}
            for acct, lots in tickets:
                # lows residual B1 (F12): ONE entry basket per account per ref.
                # An approval killed after its tickets were issued left them
                # behind: one that filled anything IS this account's entry
                # (reused — a second basket would double the position); one
                # still working with nothing filled is withdrawn first, or the
                # sweep below would fill it beside the new ticket.
                prior = oms.withdraw_stale_entry(conn, acct, entry.get("short_id"),
                                                 "re-approval: an earlier, unfinished approval's entry ticket")
                if prior["filled"]:
                    tids[acct] = prior["filled"]
                    continue
                prop = (dict(proposal, spread=dict(entry["spread"], legs=live_quotes[acct]["legs"]))
                        if acct in live_quotes else proposal)
                issued = strategy_router.issue(conn, prop, journal_ref=entry.get("short_id"),
                                               source="options_proposer.decide_pending",
                                               account_id=acct, lots=lots)
                tids[acct] = issued["ticket_id"]
            tid = tids[oms.PRIMARY_ACCOUNT]
            # The caller (decide_pending) writes this row itself, under the
            # journal lock, after this returns — the venue's own stamp is
            # off here; the fills are copied onto the legs below instead.
            venue.sweep(conn, stamp=False)
            view = oms.ticket_view(conn, tid) or {}
            record.update(mode="paper_venue", ticket_id=tid, status=view.get("status"),
                          filled_legs=sum(1 for l in view.get("legs", [])
                                          if l.get("state") == oms.FILLED))
            for acct, stid in tids.items():
                if acct == oms.PRIMARY_ACCOUNT:
                    continue
                sview = oms.ticket_view(conn, stid) or {}
                record.setdefault("accounts", {})[acct] = {
                    "ticket_id": stid, "status": sview.get("status"),
                    "lots": sview.get("lots")}
                if isinstance(entry.get("accounts"), dict) and acct in entry["accounts"]:
                    entry["accounts"][acct]["ticket_id"] = stid
                    entry["accounts"][acct]["venue_status"] = sview.get("status")
                if acct in live_quotes:
                    _live_after_fill(conn, acct, entry, stid, sview, live_quotes[acct]["quote_ts"])
            if view.get("status") == oms.FILLED:
                fills = {(str(l["side"]).upper(), str(l.get("option_type") or "").upper(),
                          float(l["strike"])): l for l in view["legs"]}
                for leg in entry["spread"]["legs"]:
                    f = fills.get((str(leg.get("side")).upper(),
                                   str(leg.get("option_type") or "").upper(),
                                   float(leg.get("strike"))))
                    if f and f.get("avg_fill_price") is not None:
                        leg["venue_fill"] = leg.get("premium")
                        leg["premium"] = float(f["avg_fill_price"])
                        leg["fill_basis"] = venue.FILL_BASIS
                entry["spread"]["ticket_id"] = tid
                entry["spread"]["venue"] = venue.VENUE
        finally:
            if own and not _PAPER_VENUE_KEEP_CONN:
                conn.close()
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
        _live_venue_error(entry, caller_conn, record["error"])
    return record


def _live_venue_error(entry: dict, conn, error: str) -> None:
    """Lows residual B1 (F14): the venue is ON but its run raised before the
    live arm got a ticket — the same outcome F14(b) removed for a venue
    switched off: an 'approved' LIVE verdict with no ticket and a lock held
    until the primary exits. Each such LIVE account is refused now, named,
    its lock released at zero (KEPT if a recorded position or a FILLED entry
    ticket backs it — `pm.release_unopened_lock`, which also withdraws a
    ticket still working). Fail-open."""
    try:
        from src import portfolio_manager as pm
        live = [a for a, v in (entry.get("accounts") or {}).items()
                if a in pm.LIVE_ACCOUNTS and (v or {}).get("status") == "approved" and not (v or {}).get("ticket_id")]
        _live_refuse_each(entry, live, f"the paper venue failed before the live arm's ticket ({error}) — it "
                                       "takes no position on this approval (audit F14)", conn)
    except Exception as exc:
        print(f"  (live account: venue-error release skipped: {exc})")


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

        # #122: the CLI decides through decide_pending — the ONE decision
        # path (margin gate, shadow accounts, venue, a rejection's release,
        # the journal lock). Before #122 it only flipped the journal field:
        # an approval after a D7 expiry would have run with no margin.
        verdict = decide_pending(journal.row_key(entry), approve=(decision == "approved"),
                                 why=why, human=True)
        if verdict["status"] not in ("approved", "rejected"):
            print(f"  not decided: {verdict['status']}"
                  + (f" ({verdict['reason']})" if verdict.get("reason") else ""))
            continue
        decided += 1
        if decision == "approved":
            print("  approved on paper — the plan tracker manages the exit "
                  "from here.")
        else:
            print("  skipped — the tracker will score the skip.")

    if decided:
        print(f"\n{decided} pending proposal(s) decided and journaled.")
    return decided


if __name__ == "__main__":
    import sys
    if "--review-pending" in sys.argv:
        review_pending()
    else:
        run_session(sys.argv[1] if len(sys.argv) > 1 else "NIFTY 50")

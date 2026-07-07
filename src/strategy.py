"""
Phase 3/4B: turns Phase 2 signals into full trade PLANS.

A plan is only ever a suggestion — nothing executes until the user approves
it in the interactive session (src/trade.py). Decision #11.

Buy signals (only for stocks we don't already hold):
  - fresh Golden Cross (50-day SMA just crossed above 200-day)
  - uptrend + RSI oversold ("uptrend with a dip")

Sell signals (only for stocks we hold):
  - fresh Death Cross (trend just flipped down)
  - downtrend (the uptrend that justified holding is gone)

Phase 4B: a plan carries the full trade thinking, not just action/shares —
entry rule, hard stop-loss, take-profit target, invalidation criteria,
risk:reward, and a plain-English rationale. Position size is risk-based:
we size so that hitting the stop loses at most RISK_PER_TRADE_PCT of the
portfolio (set by risk_level in config.json), further capped by the
per-position budget and the Phase 3 rails (cash, 25%/stock).

`propose_plans()` returns up to two BUY plans per signal — a primary
(enter at market now) and an alternative (limit order on a small pullback,
same risk math from the better price). `propose()` keeps the old contract:
the single primary, executable-now plan, or None — that is what
src/trade.py consumes today. The extra plan fields ride along on the same
dict, so journal entries pick them up without any caller changes.
"""

from src.config import (
    ALT_ENTRY_PULLBACK_PCT,
    DEFAULT_INVESTMENT_SIZE,
    DEFAULT_STOP_LOSS_PCT,
    MAX_CONCURRENT_POSITIONS,
    RISK_LEVEL,
    RISK_PER_TRADE_PCT,
    RSI_OVERSOLD,
    TAKE_PROFIT_RR,
)
from src.portfolio import max_affordable_shares, total_value


def _risk_based_shares(portfolio: dict, prices: dict, entry: float, sl_pct: float) -> int:
    """Whole shares such that the stop-loss costs at most the per-trade risk
    budget, capped by the per-position size and the Phase 3 rails."""
    if entry <= 0 or sl_pct <= 0:
        return 0
    risk_budget = total_value(portfolio, prices) * RISK_PER_TRADE_PCT / 100
    loss_per_share = entry * sl_pct / 100
    by_risk = int(risk_budget // loss_per_share)
    by_position_cap = int(DEFAULT_INVESTMENT_SIZE // entry)
    by_rails = max_affordable_shares(portfolio, entry, prices)
    return max(0, min(by_risk, by_position_cap, by_rails))


def _buy_plan(ticker: str, entry: float, signal: str, portfolio: dict,
              prices: dict, variant: str, entry_rule: str) -> dict | None:
    sl_pct = DEFAULT_STOP_LOSS_PCT
    stop = round(entry * (1 - sl_pct / 100), 2)
    target = round(entry * (1 + sl_pct * TAKE_PROFIT_RR / 100), 2)
    shares = _risk_based_shares(portfolio, prices, entry, sl_pct)
    if shares <= 0:
        return None
    max_loss = round(shares * (entry - stop), 2)
    max_gain = round(shares * (target - entry), 2)
    return {
        # Original proposal keys — trade.py and journal.py rely on these.
        "action": "BUY",
        "ticker": ticker,
        "shares": shares,
        "price": entry,
        "signal": signal,
        # Phase 4B plan fields.
        "variant": variant,
        "entry_rule": entry_rule,
        "stop_loss": {"pct": sl_pct, "price": stop},
        "target": {"price": target, "rr": TAKE_PROFIT_RR},
        "risk_reward": TAKE_PROFIT_RR,
        "max_loss_rs": max_loss,
        "invalidation": (
            f"a daily close below Rs.{stop:,.2f} (the stop), or the 50-day "
            f"average slipping back below the 200-day, kills this idea"
        ),
        "rationale": (
            f"{signal}. Sized at {RISK_LEVEL} risk ({RISK_PER_TRADE_PCT}% of "
            f"the portfolio): if the stop at Rs.{stop:,.2f} hits you lose about "
            f"Rs.{max_loss:,.0f}; if the target at Rs.{target:,.2f} hits you "
            f"make about Rs.{max_gain:,.0f} ({TAKE_PROFIT_RR:.0f}:1 reward-to-risk)."
        ),
    }


def _sell_plan(ticker: str, price: float, signal: str, shares: int) -> dict:
    return {
        "action": "SELL",
        "ticker": ticker,
        "shares": shares,
        "price": price,
        "signal": signal,
        "variant": "primary",
        "entry_rule": f"Sell all {shares} shares at market (~Rs.{price:,.2f})",
        "stop_loss": None,
        "target": None,
        "risk_reward": None,
        "max_loss_rs": None,
        "invalidation": (
            "the 50-day average climbing back above the 200-day would mean "
            "the uptrend resumed and this exit was early"
        ),
        "rationale": (
            f"{signal}. This is an exit, not a bet: the condition that "
            f"justified holding is gone, so the plan is simply to step aside."
        ),
    }


def _buy_signal(analysis: dict) -> str | None:
    rsi = analysis["rsi"]
    if analysis["fresh_cross"] and analysis["uptrend"]:
        return "fresh Golden Cross — trend just turned up"
    if analysis["uptrend"] and rsi is not None and rsi <= RSI_OVERSOLD:
        return f"uptrend with a dip (RSI {rsi:.0f}) — buying the pullback"
    return None


def propose_plans(analysis: dict, portfolio: dict, prices: dict) -> list:
    """All candidate plans for one analyze() result: [] if no signal,
    [primary] for sells, [primary, alternative] for buys when both size."""
    ticker = analysis["ticker"]
    price = analysis["price"]

    if ticker not in portfolio["holdings"]:
        signal = _buy_signal(analysis)
        if signal is None:
            return []
        if len(portfolio["holdings"]) >= MAX_CONCURRENT_POSITIONS:
            return []  # risk lever: no new positions past the cap
        plans = []
        primary = _buy_plan(
            ticker, price, signal, portfolio, prices,
            variant="primary",
            entry_rule=f"Buy at market (~Rs.{price:,.2f})",
        )
        if primary:
            plans.append(primary)
            alt_entry = round(price * (1 - ALT_ENTRY_PULLBACK_PCT / 100), 2)
            alternative = _buy_plan(
                ticker, alt_entry, signal, portfolio, prices,
                variant="alternative",
                entry_rule=(
                    f"Only if it dips: limit order at Rs.{alt_entry:,.2f} "
                    f"({ALT_ENTRY_PULLBACK_PCT:g}% below today) — same setup "
                    f"from a better price, but it may never fill"
                ),
            )
            if alternative:
                plans.append(alternative)
        return plans

    # We hold it — should we get out?
    if analysis["fresh_cross"] and not analysis["uptrend"]:
        signal = "fresh Death Cross — trend just turned down"
    elif not analysis["uptrend"]:
        signal = "downtrend — the reason for holding is gone"
    else:
        return []
    shares = portfolio["holdings"][ticker]["shares"]
    return [_sell_plan(ticker, price, signal, shares)]


def propose(analysis: dict, portfolio: dict, prices: dict):
    """Original Phase 3 contract: the single executable-now plan, or None.
    src/trade.py consumes this; the alternative plan is surfaced when
    trade.py learns to display it (next 4B step)."""
    plans = propose_plans(analysis, portfolio, prices)
    return plans[0] if plans else None


# ======================================================================
# Phase 5 — options: defined-risk multi-leg spread construction
# ======================================================================
# Appended below the Phase 3/4 equity logic, which is untouched (strict
# Phase 5 rule). Everything here is proposal-only paper math — no chain
# fetching, no execution. Locked directives (DECISIONS.md #27):
#   * ZERO naked options — every structure built here is defined-risk;
#   * India VIX regime gate — VIX above VIX_BLOCK_ABOVE strictly blocks
#     range-bound (credit/short-volatility) structures;
#   * sizing by ABSOLUTE MAX LOSS, never stop-distance.

from src.portfolio import calculate_span_margin  # noqa: E402  (Phase 5 section)

# Above this India VIX level, breakout risk makes short-range structures
# (iron condor / iron butterfly) untradeable — strictly blocked. Debit
# spreads are directional defined-risk and stay allowed in any regime.
VIX_BLOCK_ABOVE = 16.0
RANGE_BOUND_STRATEGIES = {"iron_condor", "iron_butterfly"}


class StrategyConstructor:
    """Builds defined-risk multi-leg option structures as plain dicts.

    `vix` is injected (tests pass a number; live callers pass
    dhan_client.get_india_vix()). A None vix means "regime unknown" and
    fails safe: range-bound structures are refused, directional debit
    spreads still build.

    Every constructor returns None when the structure is blocked or
    incoherent, else a spread dict:
      {strategy, direction, legs, lot_size, spread_width,
       net_credit | net_debit (per share), max_loss, max_profit (per lot),
       margin {total_margin, naked_margin, offset_savings}}
    Legs are {"side", "option_type", "strike", "premium"} — the exact
    shape portfolio.calculate_span_margin() consumes."""

    def __init__(self, vix: float = None, lot_size: int = 75):
        self.vix = vix
        self.lot_size = int(lot_size)

    # ---------------------------------------------------------- regime

    def validate_regime(self, strategy: str) -> tuple:
        """(allowed, reason). Range-bound strategies are STRICTLY blocked
        when VIX > VIX_BLOCK_ABOVE — or when VIX is unknown (fail safe)."""
        if strategy not in RANGE_BOUND_STRATEGIES:
            return True, "directional defined-risk — allowed in any VIX regime"
        if self.vix is None:
            return False, "India VIX unavailable — range-bound strategies refused (fail safe)"
        if self.vix > VIX_BLOCK_ABOVE:
            return False, (f"India VIX {self.vix:.2f} > {VIX_BLOCK_ABOVE:g} — "
                           f"breakout risk too high for range-bound structures")
        return True, f"India VIX {self.vix:.2f} <= {VIX_BLOCK_ABOVE:g} — calm regime"

    # ----------------------------------------------------- constructors

    def _package(self, strategy: str, direction: str, legs: list) -> dict:
        """Common defined-risk math for any leg basket: net premium, width,
        absolute max loss/profit per lot (the sizing quantity), and the
        SPAN margin simulation with hedge offsets."""
        lot = self.lot_size
        credit = sum(l["premium"] for l in legs if l["side"] == "SELL")
        debit = sum(l["premium"] for l in legs if l["side"] == "BUY")
        net = credit - debit  # >0 credit structure, <0 debit structure

        # Worst-case width: for verticals it's the strike gap; for a condor/
        # butterfly the loss side is the wider wing (only one side can lose).
        widths = []
        for opt_type in ("CE", "PE"):
            strikes = [l["strike"] for l in legs if l["option_type"] == opt_type]
            if len(strikes) == 2:
                widths.append(abs(strikes[0] - strikes[1]))
        width = max(widths) if widths else 0.0

        if net >= 0:  # credit: max loss = (width - net credit) x lot
            max_loss = (width - net) * lot
            max_profit = net * lot
        else:         # debit: max loss = premium paid; profit capped by width
            max_loss = -net * lot
            max_profit = (width + net) * lot

        return {
            "strategy": strategy,
            "direction": direction,
            "legs": legs,
            "lot_size": lot,
            "spread_width": round(width, 2),
            "net_credit": round(net, 2) if net >= 0 else None,
            "net_debit": round(-net, 2) if net < 0 else None,
            "max_loss": round(max_loss, 2),
            "max_profit": round(max_profit, 2),
            "margin": calculate_span_margin(legs, lot),
        }

    def construct_bull_call_spread(self, buy_strike: float, sell_strike: float,
                                   buy_premium: float, sell_premium: float) -> dict:
        """Debit vertical for a BULLISH trend: buy the lower call, sell the
        higher call. Directional — allowed in any VIX regime."""
        if sell_strike <= buy_strike:
            return None
        legs = [
            {"side": "BUY",  "option_type": "CE", "strike": buy_strike,  "premium": buy_premium},
            {"side": "SELL", "option_type": "CE", "strike": sell_strike, "premium": sell_premium},
        ]
        return self._package("bull_call_spread", "bullish", legs)

    def construct_bear_put_spread(self, buy_strike: float, sell_strike: float,
                                  buy_premium: float, sell_premium: float) -> dict:
        """Debit vertical for a BEARISH trend: buy the higher put, sell the
        lower put. Directional — allowed in any VIX regime."""
        if sell_strike >= buy_strike:
            return None
        legs = [
            {"side": "BUY",  "option_type": "PE", "strike": buy_strike,  "premium": buy_premium},
            {"side": "SELL", "option_type": "PE", "strike": sell_strike, "premium": sell_premium},
        ]
        return self._package("bear_put_spread", "bearish", legs)

    def construct_iron_condor(self, lower_put: float, upper_call: float,
                              wing_width: float,
                              put_sell_premium: float, put_buy_premium: float,
                              call_sell_premium: float, call_buy_premium: float) -> dict:
        """Credit range-bound 4-leg: sell the put at `lower_put` and the call
        at `upper_call`, buy protective wings `wing_width` further out on
        each side. STRICTLY blocked when VIX > VIX_BLOCK_ABOVE."""
        allowed, reason = self.validate_regime("iron_condor")
        if not allowed:
            print(f"Strategy: iron condor BLOCKED — {reason}")
            return None
        if not (lower_put < upper_call) or wing_width <= 0:
            return None
        legs = [
            {"side": "SELL", "option_type": "PE", "strike": lower_put,               "premium": put_sell_premium},
            {"side": "BUY",  "option_type": "PE", "strike": lower_put - wing_width,  "premium": put_buy_premium},
            {"side": "SELL", "option_type": "CE", "strike": upper_call,              "premium": call_sell_premium},
            {"side": "BUY",  "option_type": "CE", "strike": upper_call + wing_width, "premium": call_buy_premium},
        ]
        return self._package("iron_condor", "neutral", legs)

    def construct_iron_butterfly(self, atm_strike: float, wing_width: float,
                                 atm_call_premium: float, atm_put_premium: float,
                                 wing_call_premium: float, wing_put_premium: float) -> dict:
        """Credit range-bound 4-leg: sell the ATM call AND put (the body),
        buy protective wings `wing_width` out on each side. STRICTLY
        blocked when VIX > VIX_BLOCK_ABOVE."""
        allowed, reason = self.validate_regime("iron_butterfly")
        if not allowed:
            print(f"Strategy: iron butterfly BLOCKED — {reason}")
            return None
        if wing_width <= 0:
            return None
        legs = [
            {"side": "SELL", "option_type": "CE", "strike": atm_strike,              "premium": atm_call_premium},
            {"side": "SELL", "option_type": "PE", "strike": atm_strike,              "premium": atm_put_premium},
            {"side": "BUY",  "option_type": "CE", "strike": atm_strike + wing_width, "premium": wing_call_premium},
            {"side": "BUY",  "option_type": "PE", "strike": atm_strike - wing_width, "premium": wing_put_premium},
        ]
        return self._package("iron_butterfly", "neutral", legs)

    # ----------------------------------------------------------- sizing

    def size_lots(self, spread: dict, portfolio: dict, prices: dict,
                  risk_pct: float = None) -> int:
        """Lots such that the spread's ABSOLUTE MAX LOSS fits the per-trade
        risk budget (`risk_pct` of portfolio value; defaults to the equity
        RISK_PER_TRADE_PCT — the proposer passes the dedicated
        OPTIONS_RISK_PER_TRADE_PCT) — the options override of equity
        stop-distance sizing. Also capped so the SPAN margin (with
        offsets) never exceeds available cash: margin is what actually
        gets blocked, NOT the naked-equivalent capital."""
        if not spread or spread["max_loss"] <= 0:
            return 0
        if risk_pct is None:
            risk_pct = RISK_PER_TRADE_PCT
        risk_budget = total_value(portfolio, prices) * risk_pct / 100
        by_risk = int(risk_budget // spread["max_loss"])
        margin_per_lot = spread["margin"]["total_margin"]
        by_margin = (int(portfolio["cash"] // margin_per_lot)
                     if margin_per_lot > 0 else by_risk)
        return max(0, min(by_risk, by_margin))

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

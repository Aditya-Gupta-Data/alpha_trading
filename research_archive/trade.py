"""
Alpha Trading — Phase 3: Paper trading (fake money, real prices)
================================================================

The engine looks at your watchlist, proposes trades, and asks you about each
one. You answer y/n and say WHY in one line. Approved trades execute against
the fake portfolio; everything (including rejections and your why) goes into
the journal so `python -m src.review` can score us both later.

Run it from the project folder with:

    python -m src.trade

No broker is connected anywhere in this project — it cannot touch real money.
"""

from src import portfolio as pf
from src import journal
from src.config import DEFAULT_STOP_LOSS_PCT, DEFAULT_INVESTMENT_SIZE
from src.plan_tracker import run_tracker
from src.strategy import propose_plans
from src.suggest import load_tickers
from src.suggestions import analyze, describe
from src.notifier import send_digest


def gather_analyses():
    """Analyze every watchlist ticker plus anything we hold that fell off
    the watchlist (we still need sell signals for those)."""
    book = pf.load()
    tickers = load_tickers()
    for held in book["holdings"]:
        if held not in tickers:
            tickers.append(held)

    analyses, prices = [], {}
    for ticker in tickers:
        result = analyze(ticker)
        if result is None:
            print(f"  skip  {ticker}: not enough price history yet")
            continue
        analyses.append(result)
        prices[ticker] = result["price"]
    return book, analyses, prices


def show_portfolio(book: dict, prices: dict) -> list:
    lines = [f"Cash: Rs.{book['cash']:,.2f}"]
    for ticker, pos in book["holdings"].items():
        now = prices.get(ticker, pos["avg_price"])
        pnl = (now - pos["avg_price"]) / pos["avg_price"] * 100
        lines.append(
            f"  {ticker}: {pos['shares']} shares @ Rs.{pos['avg_price']:,.2f} "
            f"(now Rs.{now:,.2f}, {pnl:+.1f}%)"
        )
    lines.append(f"Total value: Rs.{pf.total_value(book, prices):,.2f} "
                 f"(started with Rs.{pf.STARTING_CASH:,.0f})")
    return lines


def plain_english_summary(proposal: dict, decision: str, why: str) -> str:
    verb = "bought" if proposal["action"] == "BUY" else "sold"
    cost = proposal["shares"] * proposal["price"]
    if decision == "approved":
        headline = (
            f"You {verb} {proposal['shares']} shares of {proposal['ticker']} "
            f"at Rs.{proposal['price']:,.2f} (Rs.{cost:,.2f} total)."
        )
    else:
        action_word = "buying" if proposal["action"] == "BUY" else "selling"
        headline = f"You chose NOT to go ahead with {action_word} {proposal['ticker']}."
    plan_line = ""
    if decision == "approved" and proposal.get("stop_loss"):
        plan_line = (
            f"\n   The plan: get out if it drops to Rs.{proposal['stop_loss']['price']:,.2f}, "
            f"take profit around Rs.{proposal['target']['price']:,.2f}."
        )
    return (
        f"{headline}{plan_line}\n"
        f"   Why the engine suggested it: {proposal['signal']}\n"
        f"   Your reason: {why}"
    )


def show_plan(prop: dict, alternative: dict | None) -> None:
    """Print the full 4B plan for one proposal (terminal = technical view)."""
    cost = prop["shares"] * prop["price"]
    print(f"PROPOSAL: {prop['action']} {prop['shares']} x {prop['ticker']} "
          f"@ Rs.{prop['price']:,.2f}  (Rs.{cost:,.2f})")
    print(f"  engine's reason: {prop['signal']}")
    print(f"  the plan: {prop['entry_rule']}")
    if prop.get("stop_loss"):
        print(f"            stop-loss Rs.{prop['stop_loss']['price']:,.2f} "
              f"(-{prop['stop_loss']['pct']:g}%)  |  "
              f"target Rs.{prop['target']['price']:,.2f} "
              f"({prop['risk_reward']:g}:1)  |  "
              f"max loss ~Rs.{prop['max_loss_rs']:,.0f}")
    print(f"  wrong if: {prop['invalidation']}")
    if alternative:
        print(f"  alternative (shown for context; conditional entries become "
              f"trackable in 4C): {alternative['entry_rule']}")


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_number(prompt: str, default: float) -> float:
    """Prompt for a number; Enter (or an unreadable value) uses `default`."""
    raw = ask(prompt)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"    (couldn't read '{raw}' as a number — using {default})")
        return default


def ask_tags(prompt: str) -> list:
    """Prompt for comma-separated chart-pattern tags; returns a clean list."""
    raw = ask(prompt)
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def run_session():
    print("Paper trading session — fake money, real prices.\n")
    # First, settle any open plans: stops/targets that hit since the last
    # session execute now (4C), so today's proposals see the real book.
    run_tracker()
    print()
    book, analyses, prices = gather_analyses()

    print("Today's reads:")
    for a in analyses:
        print(f"  {describe(a)}")
    print()

    plan_sets = [plans for a in analyses if (plans := propose_plans(a, book, prices))]
    session_lines = []

    if not plan_sets:
        print("No trade proposals today — nothing matched the buy/sell signals.")
    for plans in plan_sets:
        prop = plans[0]  # primary is what executes; alternative is context
        show_plan(prop, plans[1] if len(plans) > 1 else None)

        answer = ask("  approve? (y/n): ").lower()
        decision = "approved" if answer in ("y", "yes") else "rejected"
        why = ask("  your why (one line): ") or "(no reason given)"
        tags = ask_tags("  chart patterns you see (comma-separated, Enter to skip): ")

        # Risk levers: only asked for approved BUYs (a SELL is a full exit —
        # there is nothing to size and no stop to place). The answers now
        # DRIVE execution: your size sets the share count, your stop-loss %
        # reshapes the plan's stop/target that get journaled (and, from 4C,
        # tracked). Enter keeps the engine's risk-based plan as proposed.
        sl_pct = size = None
        if decision == "approved" and prop["action"] == "BUY":
            sl_pct = ask_number(
                f"  stop-loss % (Enter for {DEFAULT_STOP_LOSS_PCT}): ",
                DEFAULT_STOP_LOSS_PCT,
            )
            size = ask_number(
                f"  position size in Rs. (Enter for {DEFAULT_INVESTMENT_SIZE}): ",
                DEFAULT_INVESTMENT_SIZE,
            )
            if sl_pct != prop["stop_loss"]["pct"]:
                stop = round(prop["price"] * (1 - sl_pct / 100), 2)
                target = round(
                    prop["price"] * (1 + sl_pct * prop["risk_reward"] / 100), 2
                )
                prop["stop_loss"] = {"pct": sl_pct, "price": stop}
                prop["target"] = {"price": target, "rr": prop["risk_reward"]}
                print(f"    (stop moved to Rs.{stop:,.2f}, target to "
                      f"Rs.{target:,.2f} to keep {prop['risk_reward']:g}:1)")
            shares = min(
                int(size // prop["price"]),
                pf.max_affordable_shares(book, prop["price"], prices),
            )
            if shares <= 0:
                print(f"    (Rs.{size:,.0f} buys 0 shares at "
                      f"Rs.{prop['price']:,.2f} — keeping the proposed "
                      f"{prop['shares']})")
            elif shares != prop["shares"]:
                print(f"    (sized to your Rs.{size:,.0f}: {shares} shares "
                      f"instead of {prop['shares']})")
                prop["shares"] = shares
            prop["max_loss_rs"] = round(
                prop["shares"] * prop["price"] * prop["stop_loss"]["pct"] / 100, 2
            )

        if decision == "approved":
            if prop["action"] == "BUY":
                pf.buy(book, prop["ticker"], prop["shares"], prop["price"])
            else:
                pf.sell(book, prop["ticker"], prop["price"])
            pf.save(book)
            print(f"  done — {prop['action']} {prop['shares']} x "
                  f"{prop['ticker']} executed on paper.\n")
        else:
            print("  skipped — logged your reasoning.\n")

        journal.log(
            journal.new_entry(
                prop, decision, why, sl_pct=sl_pct, size=size, pattern_tags=tags
            )
        )
        session_lines.append(plain_english_summary(prop, decision, why))

    print("Portfolio now:")
    portfolio_lines = show_portfolio(book, prices)
    for line in portfolio_lines:
        print(f"  {line}")

    if session_lines:
        send_digest("Paper Trading Session", session_lines + [""] + portfolio_lines)

    print("\nDone. Run `python3 -m src.review` after a week to see the scorecard.")


if __name__ == "__main__":
    run_session()

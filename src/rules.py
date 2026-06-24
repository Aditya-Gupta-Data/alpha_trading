"""
Rule definitions and the engine that checks them.

HOW TO ADD A NEW KIND OF ALERT LATER:
  1. Write one small function below (it takes a `quote` dict and a `value`,
     and returns True if the alert should fire).
  2. Add it to the CONDITIONS dictionary.
  3. Add a friendly description line in `describe()`.
That's it — nothing else in the project needs to change.
"""


def price_above(quote, value):
    """Alert when the current price is above `value`."""
    return quote["current_price"] > value


def price_below(quote, value):
    """Alert when the current price is below `value`."""
    return quote["current_price"] < value


def percent_up(quote, value):
    """Alert when the stock is up by `value` percent or more today."""
    return quote["percent_change"] >= value


def percent_down(quote, value):
    """Alert when the stock is down by `value` percent or more today."""
    return quote["percent_change"] <= -value


# Maps the name you type in watchlist.yaml -> the function that checks it.
CONDITIONS = {
    "price_above": price_above,
    "price_below": price_below,
    "percent_up": percent_up,
    "percent_down": percent_down,
}


def check_rule(quote, condition, value):
    """Return True if this rule is triggered for this quote."""
    if condition not in CONDITIONS:
        raise ValueError(
            f"Unknown condition '{condition}'. Available: {', '.join(CONDITIONS)}"
        )
    return CONDITIONS[condition](quote, value)


def describe(quote, condition, value):
    """Build a human-readable alert line."""
    phrases = {
        "price_above": f"is above {value}",
        "price_below": f"is below {value}",
        "percent_up": f"is up {value}% or more today",
        "percent_down": f"is down {value}% or more today",
    }
    phrase = phrases.get(condition, condition)
    return (
        f"{quote['ticker']} {phrase} "
        f"(now Rs.{quote['current_price']}, {quote['percent_change']:+.2f}% today)"
    )

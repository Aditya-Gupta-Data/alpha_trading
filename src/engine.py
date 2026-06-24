"""
The shared 'run a check' function.

Both the command-line tool and the web app call evaluate_watchlist(). It loads
your watchlist, fetches prices, checks each rule, and returns the results as plain
data (a list of dictionaries) — so a screen can display it or the CLI can print it.
"""

from pathlib import Path

import yaml

from src.data_fetcher import get_quote
from src.rules import check_rule, describe

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"


def load_watchlist():
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
    return config.get("watchlist", []) if config else []


def evaluate_watchlist():
    """Check every rule against the latest prices and return structured results."""
    results = []
    for item in load_watchlist():
        ticker = item["ticker"]
        condition = item["condition"]
        value = item["value"]

        quote = get_quote(ticker)
        if quote is None:
            results.append({
                "ticker": ticker,
                "condition": condition,
                "value": value,
                "ok": False,
                "triggered": False,
                "price": None,
                "percent_change": None,
                "message": f"Couldn't fetch {ticker} — check the symbol or your connection",
            })
            continue

        triggered = check_rule(quote, condition, value)
        results.append({
            "ticker": ticker,
            "condition": condition,
            "value": value,
            "ok": True,
            "triggered": triggered,
            "price": quote["current_price"],
            "percent_change": quote["percent_change"],
            "message": describe(quote, condition, value) if triggered else None,
        })

    return results

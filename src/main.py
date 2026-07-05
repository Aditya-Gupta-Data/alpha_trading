"""
Alpha Trading — Phase 1: Alerting
=================================

Loads your watchlist, checks each rule against the latest prices, and sends an
alert for anything that triggers.

Run it from the project folder with:

    python -m src.main
"""

from pathlib import Path

import yaml

from src.data_fetcher import get_quote
from src.rules import check_rule, describe
from src.notifier import send_digest

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"


def load_watchlist():
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
    return config.get("watchlist", []) if config else []


def run_once():
    watchlist = load_watchlist()
    if not watchlist:
        print("Your watchlist is empty. Add some stocks in config/watchlist.yaml")
        return

    print(f"Checking {len(watchlist)} rule(s)...\n")
    triggered_lines = []

    for item in watchlist:
        ticker = item["ticker"]
        condition = item["condition"]
        value = item["value"]

        quote = get_quote(ticker)
        if quote is None:
            continue

        if check_rule(quote, condition, value):
            message = describe(quote, condition, value)
            print(f"  [ALERT] {message}")
            triggered_lines.append(message)
        else:
            print(
                f"  ok   {ticker}: no alert  "
                f"(now Rs.{quote['current_price']}, {quote['percent_change']:+.2f}%)"
            )

    if triggered_lines:
        send_digest("Your Stock Alerts for Today", triggered_lines)

    print(f"\nDone. {len(triggered_lines)} alert(s) fired.")


if __name__ == "__main__":
    run_once()

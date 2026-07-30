"""
Alpha Trading -- Phase 2: Suggestions
======================================

Looks at each stock in your watchlist and gives a plain-English read on
trend + momentum. Advisory only -- it never places a trade, it just tells
you what it sees so you can decide.

Run it from the project folder with:

    python -m src.suggest
"""

from pathlib import Path

import yaml

from src.suggestions import analyze, describe, bucket, plain_english_line
from src.notifier import send_digest

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"


def load_tickers():
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
    watchlist = config.get("watchlist", []) if config else []

    seen = set()
    tickers = []
    for item in watchlist:
        ticker = item["ticker"]
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def run_once():
    tickers = load_tickers()
    if not tickers:
        print("Your watchlist is empty. Add some stocks in config/watchlist.yaml")
        return

    print(f"Analyzing {len(tickers)} stock(s)...\n")
    results = []
    skipped = []

    for ticker in tickers:
        result = analyze(ticker)
        if result is None:
            skipped.append(ticker)
            print(f"  skip  {ticker}: no usable price history this pass")
            continue
        print(f"  {describe(result)}")
        results.append(result)

    # Second-chance pass (2026-07-30): the 08:00 run loses its FIRST few
    # tickers to a transient Dhan-side DH-905 window that has always passed
    # by the time the other ~80 names have gone through — so retrying the
    # skips once at the END of the run recovers them deterministically.
    # Same doctrine as intraday_tracker's in-sweep retry. A name that fails
    # both passes stays skipped and says so; nothing is fabricated.
    recovered = 0
    for ticker in skipped:
        result = analyze(ticker)
        if result is None:
            print(f"  still-skipped  {ticker}: no usable price history after retry")
            continue
        print(f"  recovered  {describe(result)}")
        results.append(result)
        recovered += 1

    if results:
        send_digest("Today's Stock Suggestions", build_email_body(results))

    print(f"\nDone. ({recovered} recovered on retry)" if recovered else "\nDone.")


def build_email_body(results: list) -> list:
    """Groups results into plain-English buckets for a non-technical reader."""
    groups = {"opportunity": [], "caution": [], "steady": []}
    for result in results:
        groups[bucket(result)].append(plain_english_line(result))

    lines = []
    if groups["opportunity"]:
        lines.append("WORTH A LOOK:")
        lines += [f"  - {line}" for line in groups["opportunity"]]
        lines.append("")
    if groups["caution"]:
        lines.append("KEEP AN EYE ON:")
        lines += [f"  - {line}" for line in groups["caution"]]
        lines.append("")
    if groups["steady"]:
        lines.append("NOTHING TO DO (steady, no change):")
        lines += [f"  - {line}" for line in groups["steady"]]

    return lines


if __name__ == "__main__":
    run_once()

"""
Alpha Trading — Phase 3: the scorecard
=======================================

Looks back at journal entries that are at least REVIEW_AFTER_DAYS old and
scores them against what the price actually did since:

  approved BUY   -> win if the stock rose, loss if it fell
  approved SELL  -> win if it fell after we sold (we dodged it), loss if it kept rising
  rejected BUY   -> "good skip" if it fell, "missed gain" if it rose
  rejected SELL  -> "good hold" if it rose, "should have sold" if it fell

Each verdict is shown next to the engine's signal AND your logged why, so
over time you can see which signals — and which of your own instincts —
actually hold up. Run:

    python -m src.review
"""

from datetime import date, timedelta

from src import journal
from src.data_fetcher import get_quote
from src.notifier import send_digest

REVIEW_AFTER_DAYS = 7
MOVE_THRESHOLD = 2.0  # a move under ±2% counts as "flat", not a win or loss


def verdict_for(entry: dict, pct: float) -> str:
    action, decision = entry["action"], entry["decision"]
    if abs(pct) < MOVE_THRESHOLD:
        return "flat (too early to call)"
    rose = pct > 0
    if decision == "approved":
        if action == "BUY":
            return "WIN — it rose after you bought" if rose else "LOSS — it fell after you bought"
        return "WIN — it fell after you sold" if not rose else "LOSS — it kept rising after you sold"
    # rejected proposals — scoring the skip itself
    if action == "BUY":
        return "GOOD SKIP — it fell anyway" if not rose else "MISSED GAIN — it rose without you"
    return "GOOD HOLD — it kept rising" if rose else "SHOULD HAVE SOLD — it fell while you held"


def run_review():
    entries = journal.read_all()
    if not entries:
        print("Journal is empty — run `python3 -m src.trade` first.")
        return

    cutoff = (date.today() - timedelta(days=REVIEW_AFTER_DAYS)).isoformat()
    lines, checked = [], 0

    for entry in entries:
        if entry["outcome"] is not None or entry["date"] > cutoff:
            continue
        if entry.get("plan") and entry["plan"].get("stop_loss"):
            # 4B plans with a stop/target are resolved against their own
            # rules by src/plan_tracker.py, not by this 7-day price check.
            continue
        quote = get_quote(entry["ticker"])
        if quote is None:
            continue
        now = quote["current_price"]
        pct = (now - entry["price"]) / entry["price"] * 100
        verdict = verdict_for(entry, pct)
        entry["outcome"] = {
            "checked": date.today().isoformat(),
            "price": now,
            "pct": round(pct, 2),
            "verdict": verdict,
        }
        checked += 1
        verb = "bought" if entry["action"] == "BUY" else "sold"
        did = "You" if entry["decision"] == "approved" else "You considered, but skipped,"
        lines.append(
            f"{did} {verb} {entry['ticker']} on {entry['date']} at Rs.{entry['price']:,.2f} "
            f"— it's now Rs.{now:,.2f} ({pct:+.1f}%).\n"
            f"   Verdict: {verdict}\n"
            f"   Engine's reason at the time: {entry['signal']}\n"
            f"   Your reason at the time: {entry['why']}"
        )

    if checked:
        journal.rewrite_all(entries)

    # Running totals across everything ever scored.
    scored = [e for e in entries if e["outcome"]]
    wins = sum(1 for e in scored if e["outcome"]["verdict"].startswith(("WIN", "GOOD")))
    losses = sum(1 for e in scored if e["outcome"]["verdict"].startswith(("LOSS", "MISSED", "SHOULD")))
    summary = (f"Scorecard so far: {wins} good calls, {losses} bad calls, "
               f"{len(scored) - wins - losses} flat, across {len(scored)} scored decisions.")

    if not lines:
        print(f"Nothing new to score yet (entries must be {REVIEW_AFTER_DAYS}+ days old).")
        print(summary)
        return

    print(f"Scored {checked} decision(s):\n")
    for line in lines:
        print(line + "\n")
    print(summary)
    send_digest("Paper Trading Scorecard", lines + ["", summary])


if __name__ == "__main__":
    run_review()

"""
Alpha Trading -- Phase 4F: the learning-loop tuner
===================================================

Reads every plan outcome the Phase 4C tracker has resolved (stop hit /
target hit / time stop, each carrying an r_multiple) and asks a narrow
question: of the two BUY signals strategy.py can fire -- a fresh Golden
Cross, or an uptrend dip (RSI oversold) -- which one has actually been
paying off? It writes a small weights file, data/brain_weights.json, that
src/forecast.py reads to lean a bit more or less on those same two
checklist drivers (the fresh-cross and RSI-oversold *bullish* signals
specifically -- see forecast.py, which keeps the bearish/Death-Cross and
overbought readings untuned since there's no journaled BUY archetype to
learn those from).

This stays a narrow, transparent nudge, not a black box: a driver's
weight only moves once it has at least TUNER_MIN_SAMPLES resolved trades
behind it (config.json), and the adjustment is a simple, capped linear
rule on the average R-multiple (see _weight_for) -- nothing hidden, and
capped to TUNER_WEIGHT_BOUNDS so it can never swamp the base checklist.

Pattern tags (the free-text chart-pattern labels the user picks in
trade.py, e.g. "Breakout") are broken out in the printed report and in
brain_weights.json's pattern_tag_report, for the user's own insight --
but are NOT fed into any weight: they're free-form text, not one of
forecast.py's checklist drivers, so there's nothing for the tuner to
adjust with them yet.

Run it from the project folder with:

    python3 -m src.tuner
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src import journal
from src.config import TUNER_MIN_SAMPLES, TUNER_WEIGHT_BOUNDS, TUNER_WEIGHT_SENSITIVITY

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "data" / "brain_weights.json"

# Maps strategy.py's plain-English `signal` text to the forecast.py driver
# it corresponds to -- these are the only two BUY archetypes strategy.py
# fires today (see strategy._buy_signal), and they line up 1:1 with
# forecast.py's bullish cross/RSI drivers.
_ARCHETYPES = {
    "fresh_cross": lambda signal: "Cross" in signal,
    "rsi_oversold": lambda signal: "RSI" in signal,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolved_buy_outcomes(entries: list) -> list:
    """Journal entries that are BUYs, carry a 4B plan, and have been
    resolved by the 4C plan tracker (r_multiple present). Sells never
    carry a stop-loss plan (plan_tracker only tracks stop-carrying plans),
    so this naturally excludes them."""
    out = []
    for entry in entries:
        if entry["action"] != "BUY" or not entry.get("plan"):
            continue
        outcome = entry.get("outcome")
        if not outcome or outcome.get("r_multiple") is None:
            continue
        out.append(entry)
    return out


def _archetype_for(entry: dict) -> str:
    signal = entry.get("signal", "")
    for name, matches in _ARCHETYPES.items():
        if matches(signal):
            return name
    return "other"


def _pattern_tag_breakdown(entries: list) -> dict:
    """Informational only -- count + avg R per user-chosen pattern tag, not
    fed into brain_weights.json's weights (see module docstring)."""
    breakdown = {}
    for entry in entries:
        r = entry["outcome"]["r_multiple"]
        for tag in entry.get("pattern_tags") or []:
            bucket = breakdown.setdefault(tag, {"count": 0, "total_r": 0.0})
            bucket["count"] += 1
            bucket["total_r"] += r
    return {
        tag: {"count": b["count"], "avg_r_multiple": round(b["total_r"] / b["count"], 2)}
        for tag, b in breakdown.items()
    }


def _weight_for(avg_r_multiple: float) -> float:
    """Simple, transparent, capped linear rule: a positive average R nudges
    the weight above 1.0 (trust this driver more), a negative average R
    nudges it below 1.0 -- capped to TUNER_WEIGHT_BOUNDS so no single
    archetype can swamp forecast.py's checklist."""
    lo, hi = TUNER_WEIGHT_BOUNDS
    weight = 1.0 + avg_r_multiple * TUNER_WEIGHT_SENSITIVITY
    return round(max(lo, min(hi, weight)), 2)


def build_weights(entries: list = None) -> dict:
    if entries is None:
        entries = journal.read_all()
    resolved = _resolved_buy_outcomes(entries)

    by_archetype = {}
    for entry in resolved:
        archetype = _archetype_for(entry)
        bucket = by_archetype.setdefault(archetype, {"count": 0, "total_r": 0.0})
        bucket["count"] += 1
        bucket["total_r"] += entry["outcome"]["r_multiple"]

    weights, sample_counts = {}, {}
    for archetype, bucket in by_archetype.items():
        sample_counts[archetype] = bucket["count"]
        if bucket["count"] >= TUNER_MIN_SAMPLES:
            weights[archetype] = _weight_for(bucket["total_r"] / bucket["count"])
        else:
            weights[archetype] = 1.0  # not enough evidence yet -- stay neutral

    return {
        "generated": _now(),
        "min_samples": TUNER_MIN_SAMPLES,
        "resolved_trade_count": len(resolved),
        "sample_counts": sample_counts,
        "weights": weights,
        "pattern_tag_report": _pattern_tag_breakdown(resolved),
    }


def write_weights(data: dict) -> None:
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(data, f, indent=2)


def run() -> dict:
    entries = journal.read_all()
    data = build_weights(entries)
    write_weights(data)

    print(f"Tuner: {data['resolved_trade_count']} resolved BUY plan(s) to learn from.")
    if not data["weights"]:
        print("  Nothing resolved yet -- brain_weights.json written with no adjustments.")
    for archetype, weight in data["weights"].items():
        count = data["sample_counts"][archetype]
        status = "tuned" if count >= data["min_samples"] else f"needs {data['min_samples']}, has {count}"
        print(f"  {archetype}: weight {weight:.2f} ({status})")
    if data["pattern_tag_report"]:
        print("  Pattern tags (informational only, not yet fed into weights):")
        for tag, stats in data["pattern_tag_report"].items():
            print(f"    {tag}: {stats['count']} trade(s), avg {stats['avg_r_multiple']:+.2f}R")
    return data


if __name__ == "__main__":
    run()

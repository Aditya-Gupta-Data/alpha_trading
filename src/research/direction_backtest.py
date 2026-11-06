# MANUAL OFFLINE TOOL — direction backtest of the options desk's view (branch feature/direction-backtest). No cron.
"""
Did the desk's direction call — `options_proposer.market_view` on the SMA 50/200 + RSI read (the same
`simulator.analysis_from_closes` the live `suggestions.analyze` makes) — get the next move right?

    python -m src.research.direction_backtest --start 2024-01-01 --end 2025-12-31 [--universe live|broad]

For every session T per underlying: the view from closes up to T only; entry at T+1's CLOSE (index files carry
closes only, so every name is entered the same way, one session late, no look-ahead); exit at the close h sessions
later; one call per underlying until it exits (non-overlapping, like the live cool-down). Bullish = +1, bearish = -1;
neutral (range) views are counted, not graded. Per class: hit rate, Wilson LB, mean signed return, the same calls'
plain-long return (drift baseline), and a sign-permutation placebo (shuffle the views across the same calls; p =
share of shuffles whose mean signed return ≥ the real one). Option P&L is NOT modelled — direction only.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

from src.validation import stat_gates

ROOT = Path(__file__).resolve().parent.parent.parent
MACRO_DIR = ROOT / "data" / "lake" / "macro"
INDEX_FILES = {"NIFTY 50": "NIFTY.csv", "NIFTY BANK": "NIFTY_BANK.csv", "NIFTY FIN SERVICE": "NIFTY_FIN_SERVICE.csv"}
HORIZONS = (5, 10, 20)
DRAWS = 2000
SIGN = {"bullish": 1, "bearish": -1}


def load_index_closes(name: str, macro_dir=None) -> list:
    p = Path(macro_dir or MACRO_DIR) / INDEX_FILES[name]
    with p.open() as fh:
        return [(r["date"][:10], float(r["value"])) for r in csv.DictReader(fh) if r.get("value")]


def load_stock_closes(symbols, days: int = 1500) -> dict:
    from src.ingestion import bhavcopy_clerk
    bars = bhavcopy_clerk.bars_for_many(symbols, days=days)
    return {s: [(b["session"], float(b["close"])) for b in rows if b.get("close")] for s, rows in bars.items()}


def calls_for(name: str, series: list, start: str, end: str, horizon: int) -> tuple[list, int]:
    """(graded calls, neutral count) for one underlying at one horizon."""
    from src.options_proposer import market_view
    from src.simulator import analysis_from_closes
    out, neutral, i = [], 0, 0
    closes = [c for _, c in series]
    while i < len(series):
        day = series[i][0]
        if not (start <= day <= end):
            i += 1
            continue
        a = analysis_from_closes(name, closes[:i + 1])
        if a is None:
            i += 1
            continue
        view = market_view(a)
        if view not in SIGN:
            neutral += 1
            i += 1
            continue
        if i + 1 + horizon >= len(series):
            break
        c0, c1 = closes[i + 1], closes[i + 1 + horizon]
        ret = (c1 - c0) / c0 * 100.0
        out.append({"underlying": name, "day": day, "view": view, "sign": SIGN[view], "return_pct": ret,
                    "signed_pct": SIGN[view] * ret})
        i += 1 + horizon
    return out, neutral


def stats(calls: list, draws: int = DRAWS, seed: int = 0) -> dict:
    n = len(calls)
    if not n:
        return {"n": 0}
    wins = sum(1 for c in calls if c["signed_pct"] > 0)
    mean = sum(c["signed_pct"] for c in calls) / n
    signs, rets = [c["sign"] for c in calls], [c["return_pct"] for c in calls]
    rng, at_least = random.Random(seed), 0
    for _ in range(draws):
        rng.shuffle(signs)
        if sum(s * r for s, r in zip(signs, rets)) / n >= mean:
            at_least += 1
    return {"n": n, "hit_rate": wins / n, "wilson_lb": stat_gates.wilson_lower_bound(wins, n),
            "mean_signed_pct": mean, "long_only_mean_pct": sum(rets) / n,
            "bullish": sum(1 for c in calls if c["sign"] > 0), "bearish": sum(1 for c in calls if c["sign"] < 0),
            "placebo_p": (1 + at_least) / (1 + draws)}


def run(start: str, end: str, universe: str = "live", horizons=HORIZONS, series_by_name: dict = None) -> dict:
    if series_by_name is None:
        from src.options_proposer import EQUITY_OPTION_UNDERLYINGS
        if universe == "live":
            stocks = sorted(s.removesuffix(".NS") for s in EQUITY_OPTION_UNDERLYINGS)
        else:
            sec = json.loads((ROOT / "config" / "sector_universe.json").read_text())["sectors"]
            stocks = sorted({t.removesuffix(".NS") for m in sec.values() for t in m.get("constituents") or []})
        series_by_name = {n: load_index_closes(n) for n in INDEX_FILES}
        series_by_name.update(load_stock_closes(stocks))
    report = {"start": start, "end": end, "universe": universe, "label": "BACKTEST — direction only, no option P&L",
              "horizons": {}}
    for h in horizons:
        all_calls, neutral = [], 0
        for name, series in sorted(series_by_name.items()):
            c, nn = calls_for(name, series, start, end, h)
            all_calls += c
            neutral += nn
        idx = [c for c in all_calls if c["underlying"] in INDEX_FILES]
        groups = {"ALL": all_calls, "INDEX": idx, "STOCKS": [c for c in all_calls if c["underlying"] not in INDEX_FILES],
                  "BULLISH": [c for c in all_calls if c["sign"] > 0], "BEARISH": [c for c in all_calls if c["sign"] < 0]}
        for y in sorted({c["day"][:4] for c in all_calls}):
            groups[f"YEAR {y}"] = [c for c in all_calls if c["day"][:4] == y]
        report["horizons"][h] = {"neutral_skipped": neutral, "groups": {g: stats(c) for g, c in groups.items()}}
    return report


def summary_lines(report: dict) -> list:
    out = [f"{report['label']} · {report['start']}..{report['end']} · universe={report['universe']}"]
    for h, block in report["horizons"].items():
        out.append(f"\n{h}-session hold (neutral views skipped: {block['neutral_skipped']})")
        for g, s in block["groups"].items():
            if not s.get("n"):
                continue
            out.append(f"  {g:10s} n={s['n']:<4d} hit={s['hit_rate']:.0%} wilsonLB={s['wilson_lb']:.2f} "
                       f"mean={s['mean_signed_pct']:+.2f}% long-only={s['long_only_mean_pct']:+.2f}% "
                       f"bull/bear={s['bullish']}/{s['bearish']} placebo_p={s['placebo_p']:.3f}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="direction backtest of options_proposer.market_view")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--universe", choices=["live", "broad"], default="live")
    ap.add_argument("--json", help="also write the full report here")
    a = ap.parse_args(argv)
    rep = run(a.start, a.end, a.universe)
    print("\n".join(summary_lines(rep)))
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

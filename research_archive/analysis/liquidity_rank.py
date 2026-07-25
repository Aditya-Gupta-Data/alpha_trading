"""
src/analysis/liquidity_rank.py — the objective universe ordering (Dept 8)
=========================================================================

Ranks every NSE-listed equity by TRADING LIQUIDITY — average daily traded
value (TURNOVER_LACS) across the bhavcopy history in data/lake/bhavcopy/.
Liquidity is a factual market-data attribute (how much money changes hands
in the name), NOT a return/valuation prediction — so it is a safe, honest
way to order the scan universe: read the most-traded names first and walk
down the ladder.

Why turnover, not volume: turnover (value) is price-normalised, so a
penny-stock printing huge share counts doesn't outrank a genuinely liquid
large-cap. Secondary tie-breaks: number of trades, then delivery value.

Reads the raw NSE full-bhavcopy CSVs the bhavcopy_clerk drops
(one per trading day). EQ/BE series only. NULL-honest: NSE's ' -' and
blanks are skipped, never counted as zero (a non-trading day for a symbol
must not drag its average down).

CLI:
    python3 -m src.analysis.liquidity_rank [--top N] [--from-rank K] [--json OUT]
"""
import csv
import glob
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BHAV_DIR = ROOT / "data" / "lake" / "bhavcopy"

# columns of interest in sec_bhavdata_full (header has leading spaces per NSE)
_WANT_SERIES = {"EQ", "BE"}


def _num(v):
    """NSE null-honest float: ' -', '', None -> None; else float or None."""
    if v is None:
        return None
    v = v.strip()
    if v in ("-", "", "nan"):
        return None
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def load_bhavcopy(bhav_dir: Path = BHAV_DIR) -> dict:
    """Aggregate per-symbol liquidity stats across every bhavcopy day.

    Returns {symbol: {days, turnover_sum, trades_sum, deliv_sum, last_close}}.
    Averages are computed only over days the symbol actually traded, so a
    recent IPO isn't penalised for days it didn't exist."""
    agg = defaultdict(lambda: {"days": 0, "turnover_sum": 0.0,
                               "trades_sum": 0.0, "deliv_sum": 0.0,
                               "last_close": None})
    files = sorted(glob.glob(str(bhav_dir / "*.csv")))
    for fp in files:
        with open(fp, newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                continue
            cols = {name.strip(): i for i, name in enumerate(header)}
            i_sym = cols.get("SYMBOL")
            i_ser = cols.get("SERIES")
            i_to = cols.get("TURNOVER_LACS")
            i_tr = cols.get("NO_OF_TRADES")
            i_dv = cols.get("DELIV_QTY")
            i_cl = cols.get("CLOSE_PRICE")
            if i_sym is None or i_ser is None or i_to is None:
                continue
            for row in reader:
                if len(row) <= max(filter(None, [i_sym, i_ser, i_to, i_tr, i_dv, i_cl])):
                    continue
                if row[i_ser].strip() not in _WANT_SERIES:
                    continue
                to = _num(row[i_to])
                if to is None:            # symbol didn't trade / no value -> skip the day
                    continue
                s = row[i_sym].strip()
                a = agg[s]
                a["days"] += 1
                a["turnover_sum"] += to
                a["trades_sum"] += _num(row[i_tr]) or 0.0
                a["deliv_sum"] += _num(row[i_dv]) or 0.0
                cl = _num(row[i_cl]) if i_cl is not None else None
                if cl is not None:
                    a["last_close"] = cl
    return dict(agg)


def rank_universe(bhav_dir: Path = BHAV_DIR, min_days: int = 5) -> list:
    """Ordered [{rank, symbol, avg_turnover_lacs, avg_trades, days, last_close}, ...]
    descending by average daily traded value. Symbols trading fewer than
    `min_days` are excluded (too thin to rank honestly)."""
    agg = load_bhavcopy(bhav_dir)
    rows = []
    for s, a in agg.items():
        if a["days"] < min_days:
            continue
        rows.append({
            "symbol": s,
            "avg_turnover_lacs": round(a["turnover_sum"] / a["days"], 2),
            "avg_trades": round(a["trades_sum"] / a["days"], 1),
            "avg_deliv_qty": round(a["deliv_sum"] / a["days"], 0),
            "days": a["days"],
            "last_close": a["last_close"],
        })
    rows.sort(key=lambda r: (-r["avg_turnover_lacs"], -r["avg_trades"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40, help="print the top N")
    ap.add_argument("--from-rank", type=int, default=1)
    ap.add_argument("--json", type=str, default=None, help="write full ranking JSON here")
    args = ap.parse_args()
    ranking = rank_universe()
    if args.json:
        Path(args.json).write_text(json.dumps(ranking, indent=1))
    lo = args.from_rank
    hi = lo + args.top - 1
    print(f"{'RANK':>4}  {'SYMBOL':<14}{'AVG TURNOVER (Cr/day)':>22}{'AVG TRADES':>12}{'DAYS':>6}")
    for r in ranking:
        if lo <= r["rank"] <= hi:
            print(f"{r['rank']:>4}  {r['symbol']:<14}"
                  f"{r['avg_turnover_lacs']/100:>22,.1f}{r['avg_trades']:>12,.0f}{r['days']:>6}")
    print(f"\nranked {len(ranking)} liquid EQ/BE symbols across the bhavcopy history")

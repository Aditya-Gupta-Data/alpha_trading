#!/usr/bin/env python3
# MANUAL OFFLINE TOOL
"""
scripts/market_vs_skill_study.py — MANUAL OFFLINE TOOL (read-only)
==================================================================

The question (owner, 2026-11-02, after the direction-bias study showed 53
bearish spreads at 69.8% / +Rs.2,54,607 and 4 bullish at 0%): is the
bearish edge SKILL, or did we simply bet on a falling market while it fell?

The test, per settled directional spread (bear put / bull call, plus the
credit verticals):
  * DIRECTION CALL — did the underlying move the way the trade bet, from
    entry to exit? (entry = the spread's own `entry_spot` when stamped,
    else the lake close on the entry day; exit = the lake close on the
    exit day, or the last close before it.)
  * BASE RATE — over the SAME underlying, the SAME holding length and the
    SAME calendar window (first entry .. last exit), how often would a
    bet in the same direction, started on ANY trading day, have been
    right? That is what "always bet down on this index" scored, with no
    timing at all.
  If our hit rate ~ the base rate, the money came from the market's
  direction. If it is clearly higher, there is timing skill on top.

READ-ONLY. Reads `data/journal.jsonl` and the price lakes the dashboard's
benchmark already reads on the VM (`data/lake/macro/<KEY>.csv` for
indices, `data/lake/bhavcopy/<date>.csv` for stocks). No network, no
market call, no write except stdout or the one `--out` file you name.

Honest limits, printed with the result:
  * An underlying with no lake series (e.g. NIFTY MID SELECT is not in the
    indices map) is skipped BY NAME, never approximated by another index.
  * Trades overlap in time, so they are not independent; the z-score is a
    rough guide, not a p-value. n < 20 in any cell is flagged (#86).
  * The direction call ignores HOW FAR the market moved; a debit spread can
    be right on direction and still lose to time decay. Both are shown.

Usage (on the VM, from ~/alpha_trading):
    venv/bin/python scripts/market_vs_skill_study.py
    venv/bin/python scripts/market_vs_skill_study.py --out /tmp/skill.md
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MACRO_DIR = ROOT / "data" / "lake" / "macro"
BHAV_DIR = ROOT / "data" / "lake" / "bhavcopy"
MIN_N = 20
BET = {"bear_put_spread": "down", "bear_call_spread": "down",
       "bull_call_spread": "up", "bull_put_spread": "up"}
# journal ticker -> macro-lake key (src/ingestion/indices_lake.INDEX_MAP)
INDEX_KEYS = {"NIFTY 50": "NIFTY", "NIFTY": "NIFTY", "NIFTY BANK": "NIFTY_BANK",
              "BANKNIFTY": "NIFTY_BANK", "NIFTY FIN SERVICE": "NIFTY_FIN_SERVICE",
              "FINNIFTY": "NIFTY_FIN_SERVICE", "NIFTY IT": "NIFTY_IT",
              "NIFTY AUTO": "NIFTY_AUTO", "NIFTY PHARMA": "NIFTY_PHARMA",
              "NIFTY FMCG": "NIFTY_FMCG", "NIFTY METAL": "NIFTY_METAL",
              "NIFTY ENERGY": "NIFTY_ENERGY", "NIFTY REALTY": "NIFTY_REALTY",
              "NIFTY PSU BANK": "NIFTY_PSU_BANK", "NIFTY MEDIA": "NIFTY_MEDIA",
              "NIFTY INFRA": "NIFTY_INFRA"}


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ inputs
def load_trades(journal_path) -> tuple[list, dict]:
    """Settled, approved, non-hypothetical vertical spreads:
    [{ref, ticker, bet, entry_date, exit_date, entry_spot, pnl}] + skips."""
    trades, skipped = [], {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1
    try:
        lines = Path(journal_path).read_text().splitlines()
    except OSError as exc:
        return [], {f"journal unreadable: {exc}": 1}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        s, o = e.get("spread") or {}, e.get("outcome")
        bet = BET.get(s.get("strategy"))
        if not bet or e.get("decision") != "approved" or not o or o.get("hypothetical"):
            continue
        pnl = _f(o.get("pnl_rs"))
        exit_day = str(o.get("exit_date") or o.get("settled_at") or o.get("checked") or "")[:10]
        if pnl is None or len(exit_day) != 10 or not e.get("date"):
            skip("no pnl / exit date on the outcome")
            continue
        trades.append({"ref": e.get("short_id"), "ticker": e.get("ticker"), "bet": bet,
                       "entry_date": str(e["date"])[:10], "exit_date": exit_day,
                       "entry_spot": _f(s.get("entry_spot")), "pnl": pnl})
    return trades, skipped


def series_key(ticker: str) -> tuple[str, str]:
    """('macro', KEY) for an index, ('bhav', SYMBOL) for a stock."""
    t = str(ticker or "").strip()
    if t.upper() in INDEX_KEYS:
        return "macro", INDEX_KEYS[t.upper()]
    if t.upper().startswith("NIFTY"):
        return "macro", t.upper().replace(" ", "_")
    return "bhav", t.upper().removesuffix(".NS")


def _read_macro(path: Path) -> list:
    out = []
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                d, v = (row.get("date") or "").strip(), _f((row.get("value") or "").strip() or None)
                if len(d) == 10 and v:
                    out.append((d, v))
    except OSError:
        return []
    return sorted(out)


def _read_bhav(bhav_dir: Path, symbols: set, start: str) -> dict:
    """{symbol: [(date, close)]} for the wanted EQ symbols, one pass over the day files."""
    out = {s: [] for s in symbols}
    if not symbols:
        return out
    try:
        names = sorted(p for p in os.listdir(bhav_dir) if p.endswith(".csv") and p[:-4] >= start)
    except OSError:
        return out
    for name in names:
        try:
            with open(bhav_dir / name, newline="") as f:
                reader = csv.reader(f, skipinitialspace=True)
                header = [h.strip().upper() for h in next(reader)]
                si, se, ci = header.index("SYMBOL"), header.index("SERIES"), header.index("CLOSE_PRICE")
                for cols in reader:
                    if len(cols) > ci and cols[se].strip() == "EQ" and cols[si].strip() in symbols:
                        v = _f(cols[ci])
                        if v:
                            out[cols[si].strip()].append((name[:-4], v))
        except (OSError, ValueError, StopIteration):
            continue
    return out


def load_series(trades: list, macro_dir=MACRO_DIR, bhav_dir=BHAV_DIR) -> dict:
    """{ticker: [(date, close)] sorted} for every traded underlying with a
    lake series; an underlying with none is simply absent."""
    if not trades:
        return {}
    start = (date.fromisoformat(min(t["entry_date"] for t in trades)) - timedelta(days=120)).isoformat()
    keys = {t["ticker"]: series_key(t["ticker"]) for t in trades}
    out = {}
    for ticker, (kind, key) in keys.items():
        if kind == "macro":
            s = [x for x in _read_macro(Path(macro_dir) / f"{key}.csv") if x[0] >= start]
            if s:
                out[ticker] = s
    bhav = _read_bhav(Path(bhav_dir), {k for kind, k in keys.values() if kind == "bhav"}, start)
    for ticker, (kind, key) in keys.items():
        if kind == "bhav" and bhav.get(key):
            out[ticker] = sorted(bhav[key])
    return out


# ------------------------------------------------------------------ the test
def close_on_or_before(series: list, day: str):
    i = bisect.bisect_right([d for d, _ in series], day) - 1
    return series[i][1] if i >= 0 else None


def right_way(bet: str, move: float) -> bool:
    return move < 0 if bet == "down" else move > 0


def base_rate(series: list, bet: str, hold_days: int, window: tuple) -> float | None:
    """Share of start days in `window` on which a `bet` held `hold_days`
    calendar days was right. None when no start day fits."""
    hits = n = 0
    lo, hi = window
    dates = [d for d, _ in series]
    for d, c0 in series:
        if d < lo or d > hi:
            continue
        end = (date.fromisoformat(d) + timedelta(days=max(1, hold_days))).isoformat()
        if end > hi:
            break
        j = bisect.bisect_left(dates, end)
        if j >= len(series):
            break
        c1 = series[j][1]
        n += 1
        hits += right_way(bet, c1 / c0 - 1)
    return (hits / n) if n else None


def evaluate(trades: list, series: dict) -> tuple[list, dict]:
    """Per-trade rows with move %, right_way, base rate; plus skip counts."""
    rows, skipped = [], {}
    if not trades:
        return rows, skipped
    window = (min(t["entry_date"] for t in trades), max(t["exit_date"] for t in trades))
    for t in trades:
        s = series.get(t["ticker"])
        if not s:
            why = f"no lake series for {t['ticker']}"
            skipped[why] = skipped.get(why, 0) + 1
            continue
        entry = t["entry_spot"] or close_on_or_before(s, t["entry_date"])
        exit_ = close_on_or_before(s, t["exit_date"])
        if not entry or not exit_:
            why = f"no close around the trade dates for {t['ticker']}"
            skipped[why] = skipped.get(why, 0) + 1
            continue
        move = exit_ / entry - 1
        hold = (date.fromisoformat(t["exit_date"]) - date.fromisoformat(t["entry_date"])).days
        rows.append(dict(t, move_pct=round(move * 100, 2), right=right_way(t["bet"], move),
                         hold_days=hold, base=base_rate(s, t["bet"], hold, window)))
    return rows, skipped


def summary(rows: list) -> dict:
    """Hit rate vs the summed base rate, a rough z, and P&L split by whether
    the market moved our way."""
    with_base = [r for r in rows if r["base"] is not None]
    hits = sum(r["right"] for r in with_base)
    exp = sum(r["base"] for r in with_base)
    var = sum(r["base"] * (1 - r["base"]) for r in with_base)
    agreed = [r for r in rows if r["right"]]
    against = [r for r in rows if not r["right"]]

    def side(rs):
        return {"n": len(rs), "wins": sum(1 for r in rs if r["pnl"] >= 0),
                "pnl": round(sum(r["pnl"] for r in rs), 2)}
    return {"n": len(with_base), "hits": hits, "expected": round(exp, 2),
            "hit_rate": round(100.0 * hits / len(with_base), 1) if with_base else None,
            "base_rate": round(100.0 * exp / len(with_base), 1) if with_base else None,
            "z": round((hits - exp) / math.sqrt(var), 2) if var > 0 else None,
            "market_our_way": side(agreed), "market_against": side(against)}


# ------------------------------------------------------------------ output
def _rs(x) -> str:
    return f"{'+' if x >= 0 else '-'}Rs.{abs(x):,.0f}"


def verdict(s: dict) -> str:
    if not s["n"]:
        return "Nothing to judge — no trade had both a lake series and a base rate."
    if s["n"] < MIN_N:
        return (f"Too few trades ({s['n']}) to call it either way; read the table, decide nothing.")
    gap = s["hit_rate"] - s["base_rate"]
    if s["z"] is not None and s["z"] >= 2 and gap >= 10:
        return (f"Timing skill on top of the market: right {s['hit_rate']}% of the time vs "
                f"{s['base_rate']}% for a random same-direction bet (z {s['z']}).")
    if abs(gap) < 10 or (s["z"] is not None and abs(s["z"]) < 2):
        return (f"Mostly the market: right {s['hit_rate']}% of the time vs {s['base_rate']}% for a "
                "random same-direction bet over the same weeks. The profit followed the market's "
                "direction; it will not survive a change of direction by itself.")
    return (f"Worse than a random same-direction bet: {s['hit_rate']}% vs {s['base_rate']}% "
            f"(z {s['z']}).")


def render(rows: list, skipped: dict, notes: list) -> str:
    out = ["# Market vs skill study", "", "Read-only; changes no rule.", ""]
    for label, bet in (("All directional spreads", None), ("Bearish bets", "down"), ("Bullish bets", "up")):
        rs = [r for r in rows if bet is None or r["bet"] == bet]
        if not rs:
            continue
        s = summary(rs)
        flag = " ⚠️ n<20" if s["n"] < MIN_N else ""
        out += [f"## {label} — {len(rs)} trades{flag}", "",
                "| Measure | Value |", "|---|---|",
                f"| Market moved the way we bet | {s['hits']} of {s['n']} ({s['hit_rate']}%) |",
                f"| A random same-direction bet, same weeks and holding length | {s['base_rate']}% |",
                f"| Difference (rough z, trades overlap) | {s['z']} |", "",
                "| | Trades | Won | P&L |", "|---|---|---|---|",
                f"| Market moved our way | {s['market_our_way']['n']} | {s['market_our_way']['wins']} | "
                f"{_rs(s['market_our_way']['pnl'])} |",
                f"| Market moved against us | {s['market_against']['n']} | {s['market_against']['wins']} | "
                f"{_rs(s['market_against']['pnl'])} |", "",
                f"**Verdict:** {verdict(s)}", ""]
    if rows:
        out += ["## Per underlying", "", "| Underlying | Trades | Right way | Avg base rate | P&L |",
                "|---|---|---|---|---|"]
        for tk in sorted({r["ticker"] for r in rows}):
            rs = [r for r in rows if r["ticker"] == tk]
            b = [r["base"] for r in rs if r["base"] is not None]
            out.append(f"| {tk} | {len(rs)} | {sum(r['right'] for r in rs)} | "
                       f"{(100.0 * sum(b) / len(b)):.0f}% | {_rs(sum(r['pnl'] for r in rs))} |"
                       if b else f"| {tk} | {len(rs)} | {sum(r['right'] for r in rs)} | — | "
                                 f"{_rs(sum(r['pnl'] for r in rs))} |")
        out.append("")
    if skipped or notes:
        out += ["## Not read (named, never guessed)", ""]
        out += [f"- {k}: {v}" for k, v in sorted(skipped.items())] + [f"- {n}" for n in notes] + [""]
    out += ["## How to read this", "",
            "- 'Random same-direction bet' = start a bet in the same direction on the same underlying "
            "on ANY trading day in the same window and hold it the same number of days. That is the "
            "score of 'always bet down' with no timing.",
            "- If our hit rate is about the same, the profit came from the market falling. That is "
            "real money but not an edge: it reverses when the market turns.",
            "- A debit spread can be right on direction and still lose to time decay, so the "
            "'moved our way' row can contain losers."]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--journal", default=str(ROOT / "data" / "journal.jsonl"))
    ap.add_argument("--macro-dir", default=str(MACRO_DIR))
    ap.add_argument("--bhav-dir", default=str(BHAV_DIR))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    trades, skipped = load_trades(a.journal)
    notes = [] if trades else [f"no settled directional spreads read from {a.journal}"]
    series = load_series(trades, a.macro_dir, a.bhav_dir)
    rows, sk2 = evaluate(trades, series)
    for k, v in sk2.items():
        skipped[k] = skipped.get(k, 0) + v
    text = render(rows, skipped, notes)
    if a.out:
        Path(a.out).write_text(text)
        print(f"written: {a.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

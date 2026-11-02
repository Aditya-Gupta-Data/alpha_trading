#!/usr/bin/env python3
# MANUAL OFFLINE TOOL
"""
scripts/ratchet_near_miss_study.py — MANUAL OFFLINE TOOL (read-only)
====================================================================

The question (owner, 2026-11-02): the profit ratchet (decision #110) arms
only at 40% of max profit (`RATCHET_LADDER`, first rung [40, 0]). On the
11-02 Live Book the two TCS bear puts peaked at 37.7% and 39.2%, never
armed, and sat near max loss. HOW OFTEN does a directional spread climb
into the 20-39% band and still settle as a loss — and what would an
earlier breakeven rung have saved, and what could it have cost?

READ-ONLY. Opens `data/journal.jsonl` for reading and, optionally, the
database read-only (`file:...?mode=ro`). Writes nothing but stdout (or
the one `--out` file you name). Changes no rule: the ladder is a
Department 5 decision; this tool only puts numbers in front of it.

WHAT IT CAN AND CANNOT SAY — read before acting on its output:

  * Each settled row carries only its PEAK capture and its FINAL result,
    not the path between them. A trade that peaked at 30%, dipped below
    breakeven, then recovered to a win would have been CUT by an earlier
    breakeven rung. The tool cannot see that dip, so it reports a RANGE:
      saved (best case)  = every loser in the band exits at breakeven
      cost  (worst case) = every winner in the band was also cut at breakeven
    The truth is somewhere between `saved - cost` and `saved`.
  * The primary's (journal) peaks are MODEL captures (closes + intraday
    model rungs); its final P&L is mostly crossed real quotes. The live
    arm (`paper_live_positions`, `--db`) is priced on crossed quotes at
    both ends — the more honest sample, and the smaller one.
  * A breakeven exit is booked as `-frictions_rs` (exit costs still paid).
  * Small samples: any band under 20 trades is flagged. The project's own
    rule (#86) reads nothing below n = 20.

Usage (on the VM, from ~/alpha_trading):
    venv/bin/python scripts/ratchet_near_miss_study.py
    venv/bin/python scripts/ratchet_near_miss_study.py --db data/brain_map.db
    venv/bin/python scripts/ratchet_near_miss_study.py --thresholds 15,20,25,30,35 --out /tmp/ratchet.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRECTIONAL = ("bear_put_spread", "bull_call_spread")
CURRENT_ARM_PCT = 40.0          # config.RATCHET_LADDER first rung, read live in main()
BANDS = ((None, 0.0, "never positive"), (0.0, 10.0, "0-10%"), (10.0, 20.0, "10-20%"),
         (20.0, 30.0, "20-30%"), (30.0, 40.0, "30-40%"), (40.0, None, "40%+ (armed today)"))
MIN_N = 20


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def load_journal_rows(path) -> tuple[list, dict]:
    """Settled, real (approved, non-hypothetical) directional spreads from the
    journal: [{source, ref, ticker, strategy, entered, peak, final_pct, pnl,
    frictions}] plus skip counts by named reason."""
    rows, skipped = [], {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as exc:
        return [], {f"journal unreadable: {exc}": 1}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            skip("unparseable line")
            continue
        s, o = e.get("spread") or {}, e.get("outcome")
        if not s or s.get("strategy") not in DIRECTIONAL:
            continue
        if e.get("decision") != "approved":
            continue
        if o is None:
            skip("still open")
            continue
        if o.get("hypothetical"):
            skip("hypothetical outcome")
            continue
        peak = _f((e.get("ratchet") or {}).get("peak_capture_pct"))
        pnl = _f(o.get("pnl_rs"))
        if peak is None:
            skip("no ratchet peak on the row")
            continue
        if pnl is None:
            skip("no pnl_rs on the outcome")
            continue
        rows.append({"source": "PAPER_10L (journal)", "ref": e.get("short_id"),
                     "ticker": e.get("ticker"), "strategy": s.get("strategy"),
                     "entered": e.get("date"), "resolution": o.get("resolution"),
                     "peak": peak, "final_pct": _f(o.get("pct")), "pnl": pnl,
                     "frictions": _f(o.get("frictions_rs")) or 0.0})
    return rows, skipped


def load_live_rows(db_path) -> tuple[list, dict]:
    """Closed directional rows of the crossed-quote books (`paper_live_positions`),
    read-only. Their peak AND exit are real bid/ask."""
    try:
        conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return [], {f"database unreadable: {exc}": 1}
    conn.row_factory = sqlite3.Row
    try:
        got = conn.execute(
            "SELECT account_id, journal_ref, ticker, strategy, opened_at, resolution, "
            "ratchet_peak_pct, last_capture_pct, pnl_net, frictions_rs FROM paper_live_positions "
            "WHERE state = 'closed'").fetchall()
    except sqlite3.Error as exc:
        return [], {f"paper_live_positions unreadable: {exc}": 1}
    finally:
        conn.close()
    rows, skipped = [], {}
    for r in got:
        if r["strategy"] not in DIRECTIONAL:
            continue
        peak, pnl = _f(r["ratchet_peak_pct"]), _f(r["pnl_net"])
        if peak is None or pnl is None:
            why = "no ratchet peak" if peak is None else "no pnl_net"
            skipped[why] = skipped.get(why, 0) + 1
            continue
        rows.append({"source": f"{r['account_id']} (crossed quotes)", "ref": r["journal_ref"],
                     "ticker": r["ticker"], "strategy": r["strategy"],
                     "entered": str(r["opened_at"] or "")[:10], "resolution": r["resolution"],
                     "peak": peak, "final_pct": _f(r["last_capture_pct"]), "pnl": pnl,
                     "frictions": _f(r["frictions_rs"]) or 0.0})
    return rows, skipped


def band_of(peak: float) -> str:
    if peak < 0:
        return BANDS[0][2]
    for lo, hi, label in BANDS[1:]:
        if peak >= lo and (hi is None or peak < hi):
            return label
    return BANDS[-1][2]


def bands_table(rows: list) -> list:
    """[{band, n, losers, winners, pnl}] in band order — how each peak band ended."""
    out = []
    for _lo, _hi, label in BANDS:
        rs = [r for r in rows if band_of(r["peak"]) == label]
        out.append({"band": label, "n": len(rs),
                    "losers": sum(1 for r in rs if r["pnl"] < 0),
                    "winners": sum(1 for r in rs if r["pnl"] >= 0),
                    "pnl": round(sum(r["pnl"] for r in rs), 2)})
    return out


def what_if(rows: list, arm_at: float, current_arm: float = CURRENT_ARM_PCT) -> dict:
    """A breakeven rung at `arm_at` (below today's first rung). Only rows whose
    peak sits in [arm_at, current_arm) change. Returns the honest range:
    saved_best (losers exit at breakeven), cost_worst (winners also cut)."""
    band = [r for r in rows if arm_at <= r["peak"] < current_arm]
    losers = [r for r in band if r["pnl"] < 0]
    winners = [r for r in band if r["pnl"] >= 0]
    # a breakeven exit nets -frictions; vs the actual pnl
    saved = sum((-r["frictions"]) - r["pnl"] for r in losers)
    cost = sum(r["pnl"] + r["frictions"] for r in winners)
    return {"arm_at": arm_at, "affected": len(band), "losers": len(losers),
            "winners": len(winners), "saved_best": round(saved, 2),
            "cost_worst": round(cost, 2), "net_low": round(saved - cost, 2),
            "net_high": round(saved, 2), "thin": len(band) < MIN_N}


def _rs(x) -> str:
    return f"{'+' if x >= 0 else '-'}Rs.{abs(x):,.0f}"


def render(groups: dict, thresholds: list, skipped: dict, current_arm: float) -> str:
    out = ["# Ratchet near-miss study", "",
           f"Today's ratchet arms at **{current_arm:g}%** of max profit (lock breakeven). "
           "Read-only; changes no rule.", ""]
    for name, rows in groups.items():
        out += [f"## {name} — {len(rows)} settled directional spreads", ""]
        if not rows:
            out += ["Nothing to read.", ""]
            continue
        out += ["### How each peak band ended", "",
                "| Peak capture | Trades | Ended as loss | Ended as win | Total P&L |",
                "|---|---|---|---|---|"]
        for b in bands_table(rows):
            flag = " ⚠️ n<20" if 0 < b["n"] < MIN_N else ""
            out.append(f"| {b['band']} | {b['n']}{flag} | {b['losers']} | {b['winners']} | {_rs(b['pnl'])} |")
        out += ["", "### What an earlier breakeven rung would have done", "",
                "| Arm at | Trades affected | Losers rescued | Winners at risk | "
                "Saved (best case) | Cost (worst case) | Net range |", "|---|---|---|---|---|---|---|"]
        for t in thresholds:
            if t >= current_arm:
                continue
            w = what_if(rows, t, current_arm)
            flag = " ⚠️ n<20" if w["thin"] else ""
            out.append(f"| {t:g}% | {w['affected']}{flag} | {w['losers']} | {w['winners']} | "
                       f"{_rs(w['saved_best'])} | {_rs(-w['cost_worst'])} | "
                       f"{_rs(w['net_low'])} to {_rs(w['net_high'])} |")
        near = sorted((r for r in rows if 20.0 <= r["peak"] < current_arm and r["pnl"] < 0),
                      key=lambda r: r["pnl"])
        if near:
            out += ["", f"### The near-misses: peaked 20-{current_arm:g}%, settled at a loss", "",
                    "| Ref | Ticker | Strategy | Entered | Peak | Exit | P&L |", "|---|---|---|---|---|---|---|"]
            for r in near:
                out.append(f"| {r['ref']} | {r['ticker']} | {r['strategy']} | {r['entered']} | "
                           f"{r['peak']:.1f}% | {r['resolution']} | {_rs(r['pnl'])} |")
        out.append("")
    if skipped:
        out += ["## Rows not read (named, never guessed)", ""]
        out += [f"- {k}: {v}" for k, v in sorted(skipped.items())]
        out.append("")
    out += ["## How to read this", "",
            "- **Net range** is the honest answer: the path after the peak is not recorded, so a "
            "winner that dipped below breakeven after its peak would also have been cut. "
            "The truth lies between the two numbers.",
            "- If even the LOW end is clearly positive over 20+ trades, an earlier rung is worth "
            "a Department 5 decision. If the range straddles zero, the data does not support a change.",
            "- Do not tune the rung to these trades and then judge it on the same trades — "
            "that is a backtest, not evidence. Any change is forward-tested from its own date."]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--journal", default=str(ROOT / "data" / "journal.jsonl"))
    ap.add_argument("--db", default=None, help="brain_map.db (read-only) to add the crossed-quote books")
    ap.add_argument("--thresholds", default="20,25,30,35")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    try:
        from src.config import RATCHET_LADDER
        current_arm = float(sorted(RATCHET_LADDER)[0][0])
    except Exception:
        current_arm = CURRENT_ARM_PCT
    thresholds = [float(x) for x in a.thresholds.split(",") if x.strip()]
    rows, skipped = load_journal_rows(a.journal)
    groups = {"PAPER_10L (journal; model peaks)": rows}
    if a.db:
        live, sk2 = load_live_rows(a.db)
        skipped.update({f"db: {k}": v for k, v in sk2.items()})
        for r in live:
            groups.setdefault(r["source"], []).append(r)
    text = render(groups, thresholds, skipped, current_arm)
    if a.out:
        Path(a.out).write_text(text)
        print(f"written: {a.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())

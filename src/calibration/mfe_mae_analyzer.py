"""
src/calibration/mfe_mae_analyzer.py — MFE/MAE expectancy surface (§3.1)
=======================================================================

Maximum Favorable / Adverse Excursion analysis over CLOSED historical
trades: for every resolved trade, how far did the underlying actually run
in the trade's favor before reversing (MFE) and against it (MAE) during
the holding window? The resulting distribution is the empirical input the
Apex Target (§3.2, docs/self_evolving_brain_map.md) replaces fixed
R-multiple exits with — a standard systematic-trading research technique.

Data sources (both supported, pick with --source):
  * journal  — data/journal.jsonl entries with a filled `outcome`
               (equity plans AND options spreads; a spread's excursion is
               measured on its UNDERLYING from `entry_spot`, signed by the
               spread's direction).
  * simulated — the Phase 7 simulator's `simulated_trades` table in
               brain_map.db. NOTE: that table has no `status` column —
               every row IS a resolved trade by construction (resolution /
               exit_date / result are NOT NULL), so "closed" = all rows.
               The DB is opened in SQLite READ-ONLY mode (mode=ro URI):
               this script physically cannot write or lock it for others.

Market data: bars come through the hardened SafeDhanClient (Phase 1
scratchpad build) — one get_ohlc_since call per ticker (earliest window
start), sliced per trade, with auth failures (DH-906 etc.) surfaced in
the summary instead of masquerading as missing history.

Directional semantics:
  * long  (BUY equity, bull_* spreads):  MFE = highest high − ref price,
                                         MAE = ref price − lowest low
  * short (bear_* spreads):              mirrored
  * neutral (iron_condor / unknown):     SKIPPED — a range structure's
    "favorable" is the absence of movement; a signed excursion is
    undefined for it. Skips are counted and reported, never silent.
  Excursions are clamped at 0 (a trade that never moved favorably has
  MFE 0, not a negative number) and reported in % of the reference price.

Advisory contract (§3.2 / §4.4): the suggested take-profit / stop-loss
levels this prints are CONTEXT for the human and for a future proposal
card — never a silent override of the engine's fixed rules. Below
--min-samples resolved trades in a group, the script says "insufficient
data" instead of forcing out a number (§4.2's abstention rule).

Run from the project folder (read-only; --report writes ONE new json):

    python3 -m src.calibration.mfe_mae_analyzer
    python3 -m src.calibration.mfe_mae_analyzer --source simulated
    python3 -m src.calibration.mfe_mae_analyzer --report data/calibration_report.json
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent.parent
JOURNAL_PATH = ROOT / "data" / "journal.jsonl"
DB_PATH = ROOT / "data" / "brain_map.db"

IST = timezone(timedelta(hours=5, minutes=30))

MIN_SAMPLES_DEFAULT = 20   # below this, abstain (§4.2) — no forced numbers

LONG, SHORT = 1, -1


# ------------------------------------------------------------- direction

def direction_for_strategy(strategy: str) -> int | None:
    """+1 long / -1 short from a spread strategy name; None = neutral or
    unknown (excursion undefined — skip, never guess)."""
    s = (strategy or "").lower()
    if s.startswith("bull_"):
        return LONG
    if s.startswith("bear_"):
        return SHORT
    return None


# ---------------------------------------------------------------- loaders

def _trade_record(trade_id, source, ticker, strategy, direction, ref_price,
                  opened_on, exit_date, pnl, hypothetical=False) -> dict:
    return {"trade_id": trade_id, "source": source, "ticker": ticker,
            "strategy": strategy, "direction": direction,
            "ref_price": ref_price, "opened_on": opened_on,
            "exit_date": exit_date, "pnl": pnl, "hypothetical": hypothetical}


def load_journal_trades(entries: list = None,
                        include_hypothetical: bool = True) -> list:
    """Every resolved journal trade (outcome filled), normalized. Spread
    excursions are measured on the underlying from entry_spot; equity
    trades on the ticker itself from the entry price. Rejected entries'
    hypothetical resolutions are real observations of the setup and are
    included by default (they can be excluded with --approved-only)."""
    if entries is None:
        from src import journal
        entries = journal.read_all()
    trades = []
    for e in entries:
        outcome = e.get("outcome")
        if not outcome or not outcome.get("exit_date"):
            continue
        hypothetical = bool(outcome.get("hypothetical"))
        if hypothetical and not include_hypothetical:
            continue
        spread = e.get("spread")
        if spread:
            trades.append(_trade_record(
                e.get("short_id"), "journal", e.get("ticker"),
                spread.get("strategy"),
                direction_for_strategy(spread.get("strategy")),
                spread.get("entry_spot"),
                e.get("date"), outcome["exit_date"],
                outcome.get("pnl_rs"), hypothetical))
        elif e.get("action") == "BUY" and (e.get("plan") or {}).get("stop_loss"):
            trades.append(_trade_record(
                e.get("short_id"), "journal", e.get("ticker"),
                (e.get("plan") or {}).get("variant") or "equity_swing",
                LONG, e.get("price"),
                e.get("date"), outcome["exit_date"],
                outcome.get("pnl_rs"), hypothetical))
        # SELL/exit lines and pre-4B entries carry no tracked window — skip.
    return trades


def load_simulated_trades(db_path: Path = None) -> list:
    """All rows of simulated_trades (each one is a resolved trade — the
    table has no open/closed state). Opened with the SQLite read-only URI:
    writes are impossible and no reserved lock is ever taken."""
    db_path = Path(db_path) if db_path is not None else DB_PATH
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT journal_ref, underlying, strategy, proposed_on, "
                "exit_date, pnl_net FROM simulated_trades").fetchall()
        except sqlite3.OperationalError:
            return []   # table absent (fresh DB) — nothing to analyze
        return [_trade_record(
            r["journal_ref"], "simulated", r["underlying"], r["strategy"],
            direction_for_strategy(r["strategy"]),
            None,   # the simulator stores no entry spot — first-bar fallback
            r["proposed_on"], r["exit_date"], r["pnl_net"])
            for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------- excursions

def trim_window(bars: list, start_iso: str, end_iso: str) -> list:
    """The bars inside [start_iso, end_iso] inclusive — the holding window."""
    return [b for b in bars if start_iso <= b["date"] <= end_iso]


def compute_excursions(bars: list, ref_price: float, direction: int) -> dict | None:
    """MFE/MAE over a holding window's bars, relative to ref_price and
    signed by direction. None when the math is undefined (no bars, no
    usable reference, neutral direction)."""
    if not bars or direction not in (LONG, SHORT):
        return None
    if not ref_price or ref_price <= 0:
        return None
    highest = max(float(b["high"]) for b in bars)
    lowest = min(float(b["low"]) for b in bars)
    if direction == LONG:
        mfe_abs = max(0.0, highest - ref_price)
        mae_abs = max(0.0, ref_price - lowest)
    else:
        mfe_abs = max(0.0, ref_price - lowest)
        mae_abs = max(0.0, highest - ref_price)
    return {
        "mfe_abs": round(mfe_abs, 2),
        "mae_abs": round(mae_abs, 2),
        "mfe_pct": round(mfe_abs / ref_price * 100, 4),
        "mae_pct": round(mae_abs / ref_price * 100, 4),
        "bars": len(bars),
    }


# --------------------------------------------------------------- pipeline

def analyze(trades: list, fetch_bars_fn=None) -> dict:
    """Cross-reference every trade with its holding-window bars and attach
    excursions. One bar fetch per ticker (from that ticker's earliest
    entry), sliced per trade — Dhan's rate limit is respected by design.

    Returns {"records": [trade+excursion dicts], "skipped": {reason: n},
             "auth_failures": [...]} — skips are counted per reason, never
    silent (a data gap must not be mistaken for a quiet market)."""
    client = None
    if fetch_bars_fn is None:
        from src.dhan_guard import SafeDhanClient
        client = SafeDhanClient()
        fetch_bars_fn = client.get_ohlc_since

    skipped = {"neutral_strategy": 0, "no_reference_price": 0,
               "no_bars_in_window": 0, "bad_dates": 0}
    directional = [t for t in trades if t["direction"] in (LONG, SHORT)]
    skipped["neutral_strategy"] = len(trades) - len(directional)

    # one fetch per ticker, from its earliest holding-window start
    earliest: dict = {}
    for t in directional:
        if t["opened_on"] and t["exit_date"]:
            prev = earliest.get(t["ticker"])
            earliest[t["ticker"]] = min(prev, t["opened_on"]) if prev else t["opened_on"]
    bars_by_ticker = {tk: (fetch_bars_fn(tk, start) or [])
                      for tk, start in earliest.items()}

    records = []
    for t in directional:
        if not t["opened_on"] or not t["exit_date"]:
            skipped["bad_dates"] += 1
            continue
        window = trim_window(bars_by_ticker.get(t["ticker"], []),
                             t["opened_on"], t["exit_date"])
        if not window:
            skipped["no_bars_in_window"] += 1
            continue
        ref = t["ref_price"] or float(window[0]["open"])  # first-bar fallback
        exc = compute_excursions(window, ref, t["direction"])
        if exc is None:
            skipped["no_reference_price"] += 1
            continue
        records.append(dict(t, ref_price=round(float(ref), 2), **exc))

    result = {"records": records,
              "skipped": {k: v for k, v in skipped.items() if v}}
    if client is not None:
        result["auth_failures"] = client.auth_failures()
    return result


# ------------------------------------------------------------- statistics

def percentile(values: list, p: float) -> float | None:
    """Linear-interpolated percentile (p in [0, 100]); None on empty."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100) * (len(ordered) - 1)
    low = int(rank)
    frac = rank - low
    if low + 1 >= len(ordered):
        return float(ordered[-1])
    return float(ordered[low] + (ordered[low + 1] - ordered[low]) * frac)


def _group_stats(records: list) -> dict:
    mfe = [r["mfe_pct"] for r in records]
    mae = [r["mae_pct"] for r in records]
    return {
        "count": len(records),
        "median_mfe_pct": round(median(mfe), 4) if mfe else None,
        "median_mae_pct": round(median(mae), 4) if mae else None,
        "p75_mfe_pct": round(percentile(mfe, 75), 4) if mfe else None,
        "p75_mae_pct": round(percentile(mae, 75), 4) if mae else None,
    }


def summarize(records: list, min_samples: int = MIN_SAMPLES_DEFAULT) -> dict:
    """The statistical summary + the Apex suggestion (advisory only).

    Take-profit: the median MFE of WINNERS — the favorable distance half
    the winning trades actually reached; a target beyond it goes
    unfilled more often than not.
    Stop-loss: the 75th-percentile MAE of WINNERS — three quarters of
    eventual winners never went more adverse than this, so a stop just
    beyond it cuts losers while rarely shaking out a winner.

    Below `min_samples` records overall (or zero winners) the apex block
    abstains with a reason instead of emitting numbers (§4.2)."""
    winners = [r for r in records if (r["pnl"] or 0) > 0]
    losers = [r for r in records if (r["pnl"] or 0) < 0]
    by_strategy = {}
    for r in records:
        by_strategy.setdefault(r["strategy"] or "unknown", []).append(r)

    summary = {
        "all": _group_stats(records),
        "winners": _group_stats(winners),
        "losers": _group_stats(losers),
        "by_strategy": {name: _group_stats(rs)
                        for name, rs in sorted(by_strategy.items())},
    }

    if len(records) < min_samples or not winners:
        summary["apex"] = {
            "status": "insufficient_data",
            "reason": (f"{len(records)} usable trades ({len(winners)} "
                       f"winners) < the {min_samples}-trade floor — refusing "
                       "to suggest exits from noise (spec §4.2)"),
        }
        return summary

    tp = median([r["mfe_pct"] for r in winners])
    sl = percentile([r["mae_pct"] for r in winners], 75)
    summary["apex"] = {
        "status": "ok",
        "suggested_take_profit_pct": round(tp, 2),
        "suggested_stop_loss_pct": round(sl, 2),
        "reward_risk_ratio": round(tp / sl, 2) if sl and sl > 0 else None,
        "basis": f"{len(winners)} winners / {len(records)} trades",
        "note": ("ADVISORY ONLY (spec §3.2/§4.4): context for the human "
                 "and future proposal cards — never a silent override of "
                 "the engine's fixed exit rules."),
    }
    return summary


# ---------------------------------------------------------------- output

def render_summary(summary: dict, skipped: dict, source: str) -> str:
    def stat_line(name, g):
        if not g["count"]:
            return f"  {name:<22} (no trades)"
        return (f"  {name:<22} n={g['count']:<4} "
                f"MFE median {g['median_mfe_pct']:.2f}% / p75 {g['p75_mfe_pct']:.2f}%   "
                f"MAE median {g['median_mae_pct']:.2f}% / p75 {g['p75_mae_pct']:.2f}%")

    lines = [f"MFE/MAE Expectancy Surface — source: {source}", ""]
    lines.append(stat_line("all trades", summary["all"]))
    lines.append(stat_line("winners", summary["winners"]))
    lines.append(stat_line("losers", summary["losers"]))
    if summary["by_strategy"]:
        lines.append("")
        lines.append("  per strategy:")
        for name, g in summary["by_strategy"].items():
            lines.append(stat_line(f"  {name}", g))
    lines.append("")
    apex = summary["apex"]
    if apex["status"] == "ok":
        lines.append(f"  Apex suggestion: take-profit at "
                     f"+{apex['suggested_take_profit_pct']}% underlying move, "
                     f"stop at -{apex['suggested_stop_loss_pct']}% "
                     f"(reward:risk {apex['reward_risk_ratio']}; "
                     f"{apex['basis']})")
        lines.append(f"  {apex['note']}")
    else:
        lines.append(f"  Apex suggestion: ABSTAINED — {apex['reason']}")
    if skipped:
        lines.append("")
        lines.append("  skipped: " + ", ".join(f"{k}={v}"
                                               for k, v in skipped.items()))
    return "\n".join(lines)


def run(source: str = "journal", db_path: Path = None,
        include_hypothetical: bool = True,
        min_samples: int = MIN_SAMPLES_DEFAULT,
        report_path: Path = None, fetch_bars_fn=None,
        journal_entries: list = None) -> dict:
    """The full pipeline; every input is injectable for offline tests.
    Returns the report dict (also printed, and written iff report_path)."""
    trades = []
    if source in ("journal", "both"):
        trades += load_journal_trades(entries=journal_entries,
                                      include_hypothetical=include_hypothetical)
    if source in ("simulated", "both"):
        trades += load_simulated_trades(db_path=db_path)

    analysis = analyze(trades, fetch_bars_fn=fetch_bars_fn)
    summary = summarize(analysis["records"], min_samples=min_samples)

    report = {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": source,
        "trades_loaded": len(trades),
        "trades_analyzed": len(analysis["records"]),
        "skipped": analysis["skipped"],
        "summary": summary,
    }
    if analysis.get("auth_failures"):
        report["auth_failures"] = analysis["auth_failures"]

    print(render_summary(summary, analysis["skipped"], source))
    if analysis.get("auth_failures"):
        print(f"\n  ⚠ {len(analysis['auth_failures'])} auth failure(s) "
              "during bar fetches (DH-9xx) — the distributions above may "
              "be missing tickers; fix the token before trusting them.")
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=2))
        print(f"\n  report written: {report_path}")
    return report


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        description="MFE/MAE expectancy analysis of closed trades "
                    "(read-only; see docs/self_evolving_brain_map.md §3)")
    parser.add_argument("--source", choices=("journal", "simulated", "both"),
                        default="journal")
    parser.add_argument("--db", type=Path, default=None,
                        help=f"brain_map.db path (default {DB_PATH})")
    parser.add_argument("--approved-only", action="store_true",
                        help="exclude hypothetical (rejected-entry) outcomes")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES_DEFAULT)
    parser.add_argument("--report", type=Path, default=None,
                        help="write calibration_report.json here (e.g. "
                             "data/calibration_report.json)")
    args = parser.parse_args(argv)
    run(source=args.source, db_path=args.db,
        include_hypothetical=not args.approved_only,
        min_samples=args.min_samples, report_path=args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

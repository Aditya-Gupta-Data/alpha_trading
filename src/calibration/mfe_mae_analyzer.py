# MANUAL OFFLINE TOOL — not on any cron/systemd path; keep out of dead-code sweeps (Phase-1 audit 2026-07-25)
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


def optimize_expectancy_target(
    records: list,
    min_samples: int = MIN_SAMPLES_DEFAULT,
    friction_pct: float = 0.15,
) -> dict:
    """Net-expectancy Apex Target surface optimizer (`docs/self_evolving_brain_map.md` §3.1–3.2):
    searches candidate `(take_profit_pct, stop_loss_pct)` thresholds across the
    empirical MFE/MAE surface to maximize net expectancy per trade after
    `friction_pct` round-trip friction:
      - If `mae_pct >= S` and `mfe_pct < T` -> stopped out at `-S - friction_pct`.
      - If `mfe_pct >= T` and `mae_pct < S` -> target filled at `+T - friction_pct`.
      - If both `mfe_pct >= T` and `mae_pct >= S` -> resolved by realized `pnl` sign
        (`+T - friction_pct` if winner else `-S - friction_pct`).
      - If neither reached -> realized excursion proxy (`mfe_pct * 0.5` if winner
        else `-mae_pct * 0.5`) minus `friction_pct`.

    Abstains below `min_samples` (`20`) or when zero winners exist (§4.2).
    Advisory only — never overrides live exit rules."""
    winners = [r for r in records if (r.get("pnl") or 0) > 0]
    if len(records) < min_samples or not winners:
        return {
            "status": "insufficient_data",
            "reason": (
                f"{len(records)} usable trades ({len(winners)} winners) < the "
                f"{min_samples}-trade floor — refusing to optimize expectancy "
                "surface on noise (spec §4.2)"
            ),
        }

    all_mfe = [float(r["mfe_pct"]) for r in records]
    win_mfe = [float(r["mfe_pct"]) for r in winners]
    win_mae = [float(r["mae_pct"]) for r in winners]

    tp_grid = sorted({
        round(percentile(win_mfe, q) or 0.0, 4)
        for q in (25, 35, 50, 60, 70, 75, 85)
    } | {
        round(percentile(all_mfe, q) or 0.0, 4)
        for q in (40, 50, 65, 75, 85)
    })
    tp_grid = [t for t in tp_grid if t > 0.0]

    sl_grid = sorted({
        round(percentile(win_mae, q) or 0.0, 4)
        for q in (60, 75, 85, 90)
    })
    sl_grid = [s for s in sl_grid if s > 0.0] or [round(max(win_mae) or 1.0, 4)]

    best = None
    n = len(records)
    for tp in tp_grid:
        for sl in sl_grid:
            payoffs = []
            hits = 0
            for r in records:
                mfe_i = float(r["mfe_pct"])
                mae_i = float(r["mae_pct"])
                is_win = (r.get("pnl") or 0) > 0
                if mfe_i >= tp and mae_i < sl:
                    payoffs.append(tp - friction_pct)
                    hits += 1
                elif mae_i >= sl and mfe_i < tp:
                    payoffs.append(-sl - friction_pct)
                elif mfe_i >= tp and mae_i >= sl:
                    if is_win:
                        payoffs.append(tp - friction_pct)
                        hits += 1
                    else:
                        payoffs.append(-sl - friction_pct)
                else:
                    drift = (0.5 * mfe_i) if is_win else (-0.5 * mae_i)
                    payoffs.append(drift - friction_pct)
            net_exp = sum(payoffs) / n
            cand = (net_exp, tp, sl, hits / n)
            if best is None or cand[0] > best[0]:
                best = cand

    net_exp, best_tp, best_sl, hit_rate = best
    return {
        "status": "ok",
        "optimal_take_profit_pct": round(best_tp, 2),
        "optimal_stop_loss_pct": round(best_sl, 2),
        "net_expectancy_pct": round(net_exp, 4),
        "hit_rate_at_target": round(hit_rate, 4),
        "reward_risk_ratio": round(best_tp / best_sl, 2) if best_sl > 0 else None,
        "friction_pct": friction_pct,
        "basis": f"{len(winners)} winners / {n} trades",
        "note": "ADVISORY ONLY (spec §3.2/§4.4): net-expectancy surface optimum.",
    }


def summarize_by_cluster(
    records: list,
    min_samples: int = MIN_SAMPLES_DEFAULT,
    friction_pct: float = 0.15,
) -> dict:
    """Cluster-conditioned MFE/MAE and Apex summary per strategy
    (`docs/self_evolving_brain_map.md` §3.1). Abstains per cluster whenever
    that cluster has fewer than `min_samples` trades."""
    by_strat: dict[str, list] = {}
    for r in records:
        by_strat.setdefault(r.get("strategy") or "unknown", []).append(r)
    out = {}
    for name, rs in sorted(by_strat.items()):
        out[name] = {
            "stats": _group_stats(rs),
            "expectancy_apex": optimize_expectancy_target(
                rs, min_samples=min_samples, friction_pct=friction_pct
            ),
        }
    return out


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
        "expectancy_apex": optimize_expectancy_target(
            records, min_samples=min_samples
        ),
        "by_strategy_apex": summarize_by_cluster(
            records, min_samples=min_samples
        ),
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


def dry_run_open_book(
    open_entries: list,
    summary: dict,
    court_verdict: dict | None = None,
    static_profit_take_fraction: float = 0.65,
) -> dict:
    """Read-only open-book dry-run for MFE/MAE Expectancy Apex exits
    (`docs/ARCHITECTURE_ROADMAP.md` §5 line 274):
    compares each open position's current static exit rule against the
    cluster-conditioned (or global) Expectancy Apex suggestion without mutating
    `open_entries`, `journal.jsonl`, or `brain_map.db`.

    Enforces the Court verdict gate: `deploy_ready` is True ONLY when a
    Department-5 Court verdict (`proven_on_paper=True` or `promote=True`) is
    supplied; `live_override` is permanently `False` (advisory-only contract)."""
    by_strat = (summary or {}).get("by_strategy_apex") or {}
    global_apex = (summary or {}).get("expectancy_apex") or {}
    positions = []
    skipped_closed = 0

    for entry in open_entries or []:
        if entry.get("outcome") is not None:
            skipped_closed += 1
            continue
        decision = str(entry.get("decision") or "").lower()
        if decision in ("rejected", "voided"):
            skipped_closed += 1
            continue

        spread = entry.get("spread") or {}
        plan = entry.get("plan") or {}
        if spread:
            strat = spread.get("strategy") or "unknown"
            direction = direction_for_strategy(strat)
            ref_spot = float(spread.get("entry_spot") or entry.get("price") or 0.0)
            static_rule = {
                "kind": "options_spread",
                "profit_take_fraction": static_profit_take_fraction,
                "mid_trade_stop": None,
            }
        else:
            strat = plan.get("variant") or "equity_swing"
            direction = LONG if str(entry.get("action") or "").upper() == "BUY" else None
            ref_spot = float(entry.get("price") or 0.0)
            sl_price = plan.get("stop_loss")
            tp_price = plan.get("target")
            static_sl_pct = (
                round((ref_spot - float(sl_price)) / ref_spot * 100.0, 2)
                if ref_spot > 0 and sl_price
                else None
            )
            static_tp_pct = (
                round((float(tp_price) - ref_spot) / ref_spot * 100.0, 2)
                if ref_spot > 0 and tp_price
                else None
            )
            static_rule = {
                "kind": "equity_swing",
                "static_take_profit_pct": static_tp_pct,
                "static_stop_loss_pct": static_sl_pct,
            }

        cluster_apex = (by_strat.get(strat) or {}).get("expectancy_apex") or {}
        chosen_apex = (
            cluster_apex
            if cluster_apex.get("status") == "ok"
            else (global_apex if global_apex.get("status") == "ok" else {"status": "insufficient_data"})
        )
        apex_source = (
            f"cluster:{strat}"
            if cluster_apex.get("status") == "ok"
            else ("global" if global_apex.get("status") == "ok" else "abstained")
        )

        cand_tp_price = None
        cand_sl_price = None
        if chosen_apex.get("status") == "ok" and ref_spot > 0 and direction in (LONG, SHORT):
            tp_pct = float(chosen_apex["optimal_take_profit_pct"]) / 100.0
            sl_pct = float(chosen_apex["optimal_stop_loss_pct"]) / 100.0
            if direction == LONG:
                cand_tp_price = round(ref_spot * (1.0 + tp_pct), 2)
                cand_sl_price = round(ref_spot * (1.0 - sl_pct), 2)
            else:
                cand_tp_price = round(ref_spot * (1.0 - tp_pct), 2)
                cand_sl_price = round(ref_spot * (1.0 + sl_pct), 2)

        positions.append({
            "trade_id": entry.get("short_id") or entry.get("id"),
            "ticker": entry.get("ticker"),
            "strategy": strat,
            "direction": direction,
            "ref_spot": round(ref_spot, 2) if ref_spot > 0 else None,
            "static_exit": static_rule,
            "apex_source": apex_source,
            "apex_candidate": {
                "status": chosen_apex.get("status", "insufficient_data"),
                "optimal_take_profit_pct": chosen_apex.get("optimal_take_profit_pct"),
                "optimal_stop_loss_pct": chosen_apex.get("optimal_stop_loss_pct"),
                "net_expectancy_pct": chosen_apex.get("net_expectancy_pct"),
                "implied_tp_spot": cand_tp_price,
                "implied_sl_spot": cand_sl_price,
            },
        })

    court_passed = bool(
        court_verdict
        and (
            court_verdict.get("proven_on_paper") is True
            or court_verdict.get("promote") is True
        )
    )
    return {
        "mode": "open_book_dry_run",
        "n_open_evaluated": len(positions),
        "skipped_closed_or_rejected": skipped_closed,
        "court_approved": court_passed,
        "deploy_ready": court_passed,
        "live_override": False,
        "status": (
            "COURT_APPROVED_ADVISORY"
            if court_passed
            else "DRY_RUN_ONLY_AWAITING_COURT"
        ),
        "positions": positions,
    }


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

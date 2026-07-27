"""
src/validation/h4_comparator.py — the H4 Simulator Experiment (design doc:
docs/h4_simulator_experiment_design.md)
============================================================================

Tests H4 (`docs/hypotheses.md` §H4): does adding to a price-CONFIRMED
continuation and staged-trimming an adverse move beat the current
one-and-done gate (#68), on a risk-adjusted basis? This module never
answers that question by assertion — it runs both policies through the
Phase 7 Time-Travel Simulator's own machinery (`src/simulator.py`,
`src/plan_tracker.py`) over the same signals and lets Sharpe/Sortino/
max-drawdown decide.

STATUS: experiment harness. Nothing here registers a pattern
(`validation/registry.py`) or touches live sizing — per the owner's own
H4 note, that only happens if a run clears the bar in §2 of the design
doc, and only as a separate, later decision.

MECHANISM (owner-approved 2026-07-27, replaces the doc's placeholder):
  * CONTINUATION (eligible to add) requires BOTH, never signal repetition
    alone:
      1. mark improvement — the stack's most recently opened spread is
         priced ahead of its own entry mark today (pt._spread_mark vs
         pt._spread_entry_mark), and
      2. a fresh N-day extreme in the position's direction (new N-day low
         for a bearish view, new N-day high for a bullish view) — this is
         the parameter under test, `lookback_days` in {3, 5, 10} (a
         micro-swing / weekly / bi-weekly grid, owner's framing).
    Signal repetition with neither confirmation does nothing — the day is
    skipped exactly as the one-and-done baseline already does. This is
    the direct guard against reproducing the #68 pileup (nine near-
    identical spreads fired because the checklist stayed "bearish", not
    because price actually kept confirming it).
  * ADVERSE is staged, not binary (owner-specified 2026-07-27):
      - loss >= H4_TRIM_PCT_OF_MAX_LOSS (default 25%) of the position's
        own max_loss -> trim: close H4_TRIM_FRACTION (default 50%) of its
        CURRENT remaining lots, once per position.
      - loss >= H4_FULL_EXIT_PCT_OF_MAX_LOSS (default 35%) -> close all
        remaining lots of that position immediately.
    Both checked before that day's normal profit_take/pre_expiry_exit
    trigger, on every open position independently (not just the newest).
  * STACK CAP: `H4_MAX_STACK` (default 3) concurrent spreads per
    underlying+direction. Reached -> further continuation signals are
    refused and counted (`stats["stack_capped_by_count"]`).
  * VEGA/DELTA CEILING (design doc §2, guardrail #71): NOT implemented in
    this first cut. `portfolio_greeks.aggregate()` prices legs from a
    real Dhan chain's nested `greeks.{delta,theta,gamma,vega}` — the
    simulator's `build_synthetic_chain()` never populates that field (it
    only has last_price), so there is no honest Greeks number to gate on
    here without fabricating one. Documented, not silently skipped: the
    stack cap above is the only concentration guardrail this run has.
    A real Greeks ceiling needs the synthetic chain to grow modeled
    Greeks first — a separate, later change, not folded in here.

ISOLATION (design doc §3):
  * Additive columns on the EXISTING `simulated_trades` table only:
    `policy` ('baseline'|'pyramid'), `stack_id`, `lookback_days`,
    `lots_closed`, `exit_reason`. No new table — the sim/real separation
    (#49/#65) is unchanged; `src/performance.py` still reads only
    `journal.read_all()` and never sees these rows.
  * Every row's `journal_ref` is namespaced "sim:h4:<hash>" (never
    "sim:<hash>") — cannot collide with `src/simulator.py`'s own
    production refs, and a stray join against `simulated_trades` can
    filter h4 rows out by prefix alone even before checking `policy`.
  * `brain_map.record_resolved_entry()` is called ONLY when a position's
    lots reach zero (a true close) — a partial trim writes its own
    `simulated_trades` row (so it counts in the P&L series) but is
    deliberately NOT pushed into the causal-link writer, which expects
    one journal-shaped resolution per position, not a stream of partials.

Run: `python3 -m src.validation.h4_comparator --start 2025-01-01 --end 2025-06-30`
"""

import argparse
import hashlib
import sys
from datetime import date

from src import brain_map
from src import plan_tracker as pt
from src.config import MOVING_AVERAGE_SLOW
from src.options_proposer import build_proposal, market_view
from src.performance import sharpe, sortino, max_drawdown
from src.simulator import (
    STRIKE_STEPS, SIM_BOOK_CASH, analysis_from_closes, next_expiry,
    build_synthetic_chain, _entry_for,
)
from src.simulator import ensure_schema as _ensure_sim_schema
from src.simulator import _fetch_bars, _fetch_vix_series

DEFAULTS = {
    "lookback_grid": [3, 5, 10],
    "max_stack": 3,
    "trim_pct_of_max_loss": 25.0,
    "trim_fraction": 0.5,
    "full_exit_pct_of_max_loss": 35.0,
    "drawdown_tolerance_pct": 10.0,
}

POLICIES = ("baseline", "pyramid")

# Below this many bars, analysis_from_closes() returns None for EVERY day
# (it needs MOVING_AVERAGE_SLOW+1 closes), so the run is guaranteed to
# produce zero trades regardless of policy — an un-runnable experiment,
# not a thin one.
MIN_WARMUP_BARS = MOVING_AVERAGE_SLOW + 1


class H4DataError(RuntimeError):
    """The experiment cannot run because its market data is missing or too
    short. Raised INSTEAD of walking zero bars to a clean-looking
    'insufficient_data' report — a failed fetch and a genuinely thin
    sample must never be indistinguishable in the output (2026-07-27:
    an expired token produced a DH-901, `_fetch_bars` fail-open-returned
    [], and the run printed a tidy n=0 report that read like a result)."""


def _validate_bars(underlying: str, bars, self_fetched: bool) -> None:
    """Abort loudly on unusable history. `self_fetched` distinguishes a
    live-fetch failure (almost always auth/network — name the likely
    cause) from injected bars that are simply too short."""
    source = ("the Dhan fetch returned no usable history" if self_fetched
              else "the injected bar series is empty")
    if not bars:
        raise H4DataError(
            f"{underlying}: {source}. Nothing was simulated and NO verdict "
            "is possible.\n"
            "  If a DH-901 / Invalid_Authentication error is printed above, "
            "the Dhan access token in .env is expired — regenerate it and "
            "re-run.\n"
            "  (A zero-bar run is a DATA FAILURE, not an experimental "
            "result — refusing to emit a report.)")
    if len(bars) < MIN_WARMUP_BARS:
        raise H4DataError(
            f"{underlying}: only {len(bars)} bar(s) available, but "
            f"{MIN_WARMUP_BARS} are needed before the first signal can "
            f"fire (the {MOVING_AVERAGE_SLOW}-day moving average warmup).\n"
            "  Every day would be skipped and the run would report zero "
            "trades. Start the range earlier.")

_H4_COLUMNS = (
    ("policy", "TEXT"),
    ("stack_id", "TEXT"),
    ("lookback_days", "INTEGER"),
    ("lots_closed", "INTEGER"),
    ("exit_reason", "TEXT"),
)


def ensure_schema(conn) -> None:
    """simulated_trades + h4's own additive columns. Idempotent."""
    _ensure_sim_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(simulated_trades)")}
    for col, sqltype in _H4_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE simulated_trades ADD COLUMN {col} {sqltype}")
    conn.commit()


def h4_ref(underlying: str, day: str, strategy: str, expiry: str,
           policy: str, lookback_days, stack_id: str, seq: int) -> str:
    """Deterministic, namespaced so it can never collide with
    src.simulator's own "sim:<hash>" refs — see module docstring."""
    key = f"{underlying}|{day}|{strategy}|{expiry}|{policy}|{lookback_days}|{stack_id}|{seq}"
    return "sim:h4:" + hashlib.sha1(key.encode()).hexdigest()[:12]


# --------------------------------------------------------------- position

def _open_position(underlying, day, i, bars, analysis, vix, step, policy,
                   lookback_days, stack_id, seq, stats):
    """Build + propose a fresh spread and its scheduled (normal) exit.
    Returns a position dict, or None if no proposal / it never resolves
    inside the available bars."""
    expiry = next_expiry(date.fromisoformat(day))
    dte = (date.fromisoformat(expiry) - date.fromisoformat(day)).days
    chain = build_synthetic_chain(analysis["price"], vix, dte, step)
    result = build_proposal(underlying, analysis=analysis, vix=vix,
                            expiry=expiry, chain=chain,
                            book={"cash": SIM_BOOK_CASH, "holdings": {}},
                            prices={})
    if result["proposal"] is None:
        return None
    p = result["proposal"]
    ref = h4_ref(underlying, day, p["spread"]["strategy"], expiry, policy,
                lookback_days, stack_id, seq)
    entry = _entry_for(p, day, ref)
    schedule = pt._resolve_spread(entry, bars[i:])
    if schedule is None:
        stats["unresolved_at_range_end"] += 1
        return None
    resolution, m_exit, frac_left_exit, exit_day = schedule
    spread = entry["spread"]
    lot = int(spread["lot_size"])
    entry_day = date.fromisoformat(day)
    expiry_date = date.fromisoformat(spread["expiry"])
    total_days = max(1, (expiry_date - entry_day).days)
    return {
        "ref": ref, "entry": entry, "spread": spread,
        "underlying": underlying, "view": p["view"],
        "m_entry": pt._spread_entry_mark(spread),
        "max_loss_ps": float(spread["max_loss"]) / lot if lot else 0.0,
        "max_profit_ps": float(spread["max_profit"]) / lot if lot else 0.0,
        "lots_remaining": int(spread.get("lots", 1)),
        "lots_original": int(spread.get("lots", 1)),
        "entry_day": entry_day, "expiry_date": expiry_date,
        "total_days": total_days,
        "schedule": {"resolution": resolution, "m_exit": m_exit,
                     "frac_left": frac_left_exit, "exit_day": exit_day},
        "trimmed": False, "stack_id": stack_id, "policy": policy,
        "lookback_days": lookback_days, "vix": vix,
    }


def _mark_now(pos, day, close):
    days_left = (pos["expiry_date"] - date.fromisoformat(day)).days
    frac_left = max(0.0, days_left / pos["total_days"])
    m_now = pt._spread_mark(pos["spread"], float(close), frac_left)
    profit_ps = max(-pos["max_loss_ps"],
                    min(m_now - pos["m_entry"], pos["max_profit_ps"]))
    return profit_ps, frac_left


def _settle_portion(pos, day, close, lots_closing, exit_reason, stats):
    """Close exactly `lots_closing` of pos's remaining lots at today's
    mark. Mutates pos['lots_remaining']. Returns a row dict ready for
    _record(), or None if there's nothing left to close."""
    lots_closing = max(0, min(lots_closing, pos["lots_remaining"]))
    if lots_closing == 0:
        return None
    profit_ps, frac_left = _mark_now(pos, day, close)
    spread = pos["spread"]
    qty = int(spread["lot_size"]) * lots_closing
    gross_pnl = profit_ps * qty
    cost_spread = dict(spread)
    cost_spread["lots"] = lots_closing
    frictions, slippage = pt._spread_exit_costs(cost_spread, float(close),
                                                 frac_left, vix=pos["vix"])
    pnl_net = round(gross_pnl - frictions - slippage, 2)
    max_loss_total = pos["max_loss_ps"] * int(spread["lot_size"]) * lots_closing
    max_profit_total = pos["max_profit_ps"] * int(spread["lot_size"]) * lots_closing
    capture_pct = (gross_pnl / max_profit_total * 100) if max_profit_total > 0 else 0.0
    pos["lots_remaining"] -= lots_closing
    result = ("win" if pnl_net > 0 else "loss" if pnl_net < 0 else "scratch")
    resolution = ("closed_out" if exit_reason in ("staged_trim", "staged_full_exit")
                  else pos["schedule"]["resolution"])
    row = {
        "journal_ref": pos["ref"] if pos["lots_remaining"] == 0 else
                       f"{pos['ref']}:{exit_reason}:{pos['lots_original'] - pos['lots_remaining']}",
        "underlying": pos["underlying"], "strategy": spread["strategy"],
        "view": pos["view"], "proposed_on": pos["entry_day"].isoformat(),
        "expiry": spread["expiry"], "vix": pos["vix"],
        "net_credit": spread.get("net_credit"), "net_debit": spread.get("net_debit"),
        "spread_width": spread.get("spread_width"),
        "max_loss": pos["max_loss_ps"] * int(spread["lot_size"]),
        "max_profit": pos["max_profit_ps"] * int(spread["lot_size"]),
        "lots": lots_closing, "lot_size": int(spread["lot_size"]),
        "resolution": resolution, "exit_date": day, "pnl_net": pnl_net,
        "frictions_rs": round(frictions, 2), "slippage_rs": round(slippage, 2),
        "capture_pct": round(capture_pct, 2),
        "r_multiple": round(pnl_net / max_loss_total, 2) if max_loss_total > 0 else None,
        "result": result, "verdict": exit_reason,
        "policy": pos["policy"], "stack_id": pos["stack_id"],
        "lookback_days": pos["lookback_days"], "lots_closed": lots_closing,
        "exit_reason": exit_reason,
    }
    stats["results"][result] += 1
    if exit_reason == "staged_trim":
        stats["trims"] += 1
    elif exit_reason in ("staged_full_exit", "profit_take", "pre_expiry_exit"):
        stats["exits"] += 1
    return row


def _record(conn, row, entry_for_brain=None):
    conn.execute(
        "INSERT OR IGNORE INTO simulated_trades (journal_ref, underlying, "
        "strategy, view, proposed_on, expiry, vix, net_credit, net_debit, "
        "spread_width, max_loss, max_profit, lots, lot_size, resolution, "
        "exit_date, pnl_net, frictions_rs, slippage_rs, capture_pct, "
        "r_multiple, result, verdict, policy, stack_id, lookback_days, "
        "lots_closed, exit_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?)",
        (row["journal_ref"], row["underlying"], row["strategy"], row["view"],
         row["proposed_on"], row["expiry"], row["vix"], row["net_credit"],
         row["net_debit"], row["spread_width"], row["max_loss"],
         row["max_profit"], row["lots"], row["lot_size"], row["resolution"],
         row["exit_date"], row["pnl_net"], row["frictions_rs"],
         row["slippage_rs"], row["capture_pct"], row["r_multiple"],
         row["result"], row["verdict"], row["policy"], row["stack_id"],
         row["lookback_days"], row["lots_closed"], row["exit_reason"]))
    conn.commit()
    if entry_for_brain is not None:
        brain_map.record_resolved_entry(conn, entry_for_brain)


def _closing_entry_for_brain(pos, row):
    """A journal-shaped dict for brain_map.record_resolved_entry — only
    built when a position is FULLY closed (see module docstring)."""
    entry = dict(pos["entry"])
    entry["outcome"] = {
        "checked": row["exit_date"], "resolution": row["resolution"],
        "price": None, "exit_date": row["exit_date"], "pct": row["capture_pct"],
        "r_multiple": row["r_multiple"],
        "days_in_trade": (date.fromisoformat(row["exit_date"])
                          - pos["entry_day"]).days,
        "pnl_rs": row["pnl_net"], "frictions_rs": row["frictions_rs"],
        "slippage_rs": row["slippage_rs"], "exit_style": "atomic_basket",
        "hypothetical": False, "position_closed": False, "simulated": True,
        "verdict": row["verdict"],
    }
    return entry


# ----------------------------------------------------------- continuation

def _fresh_extreme(closes, lookback_days, bullish: bool) -> bool:
    window = closes[-lookback_days:] if len(closes) >= lookback_days else closes
    if len(window) < 2:
        return False
    return (window[-1] >= max(window)) if bullish else (window[-1] <= min(window))


# --------------------------------------------------------------- the loop
# NOTE: continuation confirmation (view match + mark improvement + fresh
# N-day extreme) is evaluated inline in _run_underlying's pyramid branch,
# not as a standalone helper — it needs the current day's close and the
# open stack's live mark together, both already in scope there.

def _run_underlying(underlying, bars, vix_by_date, start, end, policy,
                    lookback_days, conn, cfg, stats):
    step = STRIKE_STEPS.get(underlying, 50.0)
    stack = []          # open positions (pyramid: up to max_stack; baseline: 0 or 1)
    stack_id = None
    seq = 0
    blocked_until = ""  # baseline gate only

    for i, (day, _low, _high, close) in enumerate(bars):
        if not (start <= day <= end) or day <= blocked_until:
            continue
        stats["days_scanned"] += 1
        closes = [float(b[3]) for b in bars[:i + 1]]
        analysis = analysis_from_closes(underlying, closes)
        if analysis is None:
            continue
        vix = vix_by_date.get(day)

        # ---- 1. manage every open position (staged adverse, then schedule) ----
        for pos in list(stack):
            profit_ps, frac_left = _mark_now(pos, day, close)
            loss_frac_pct = (max(0.0, -profit_ps) / pos["max_loss_ps"] * 100
                             if pos["max_loss_ps"] else 0.0)
            row = None
            if policy == "pyramid" and loss_frac_pct >= cfg["full_exit_pct_of_max_loss"]:
                row = _settle_portion(pos, day, close, pos["lots_remaining"],
                                      "staged_full_exit", stats)
            elif (policy == "pyramid" and not pos["trimmed"]
                  and loss_frac_pct >= cfg["trim_pct_of_max_loss"]):
                trim_lots = max(1, round(pos["lots_remaining"] * cfg["trim_fraction"]))
                row = _settle_portion(pos, day, close, trim_lots, "staged_trim", stats)
                pos["trimmed"] = True
            elif day >= pos["schedule"]["exit_day"]:
                row = _settle_portion(pos, day, close, pos["lots_remaining"],
                                      pos["schedule"]["resolution"], stats)

            if row is not None:
                brain_entry = (_closing_entry_for_brain(pos, row)
                               if pos["lots_remaining"] == 0 else None)
                _record(conn, row, brain_entry)
                stats["resolved"] += 1
            if pos["lots_remaining"] == 0:
                stack.remove(pos)

        # ---- 2. open new positions ----
        if policy == "baseline":
            if stack:
                continue
            pos = _open_position(underlying, day, i, bars, analysis, vix, step,
                                 policy, None, f"{underlying}:{day}", seq, stats)
            if pos is None:
                continue
            seq += 1
            stack.append(pos)
            stats["proposed"] += 1
            blocked_until = pos["schedule"]["exit_day"]
        else:  # pyramid
            if not stack:
                pos = _open_position(underlying, day, i, bars, analysis, vix, step,
                                     policy, lookback_days, f"{underlying}:{day}",
                                     seq, stats)
                if pos is None:
                    continue
                seq += 1
                stack_id = pos["stack_id"]
                stack.append(pos)
                stats["proposed"] += 1
                continue
            if len(stack) >= cfg["max_stack"]:
                stats["stack_capped_by_count"] += 1
                continue
            newest = stack[-1]
            view = market_view(analysis)
            profit_ps, _ = _mark_now(newest, day, close)
            bullish = (view == "bullish")
            confirmed = (view == newest["view"] and profit_ps > 0
                        and _fresh_extreme(closes, lookback_days, bullish))
            stats["stack_depth_samples"].append(len(stack))
            if not confirmed:
                continue
            add = _open_position(underlying, day, i, bars, analysis, vix, step,
                                 policy, lookback_days, stack_id, seq, stats)
            if add is None:
                continue
            seq += 1
            stack.append(add)
            stats["proposed"] += 1
            stats["adds"] += 1

    return stats


def run_h4_experiment(start: str, end: str, underlyings=("NIFTY 50",), *,
                      conn=None, bars_by_underlying: dict = None,
                      vix_by_date: dict = None, config: dict = None) -> dict:
    """Runs BOTH policies ('baseline', 'pyramid') for every lookback in
    `config["lookback_grid"]` (pyramid only — baseline has no lookback
    parameter, run once, stored with lookback_days=NULL), over the same
    bars/VIX series, so the only variable across a comparison is the
    management policy. Returns {"runs": [...stats per (policy, lookback)],
    "report": compute_report(conn)}."""
    cfg = dict(DEFAULTS)
    cfg.update(config or {})
    owns_conn = conn is None
    if conn is None:
        conn = brain_map.connect()
    ensure_schema(conn)
    self_fetched = bars_by_underlying is None
    if self_fetched:
        bars_by_underlying = {u: _fetch_bars(u, start) for u in underlyings}
    # Abort BEFORE any policy walks a single day — see H4DataError.
    for u in underlyings:
        _validate_bars(u, bars_by_underlying.get(u), self_fetched)
    vix_by_date = vix_by_date or {}

    runs = []
    for underlying in underlyings:
        bars = bars_by_underlying.get(underlying) or []
        stats = {"days_scanned": 0, "proposed": 0, "resolved": 0,
                 "unresolved_at_range_end": 0, "adds": 0, "trims": 0,
                 "exits": 0, "stack_capped_by_count": 0,
                 "stack_depth_samples": [], "results": {"win": 0, "loss": 0, "scratch": 0}}
        _run_underlying(underlying, bars, vix_by_date, start, end,
                        "baseline", None, conn, cfg, stats)
        runs.append({"underlying": underlying, "policy": "baseline",
                     "lookback_days": None, "stats": stats})

        for lookback_days in cfg["lookback_grid"]:
            stats = {"days_scanned": 0, "proposed": 0, "resolved": 0,
                     "unresolved_at_range_end": 0, "adds": 0, "trims": 0,
                     "exits": 0, "stack_capped_by_count": 0,
                     "stack_depth_samples": [], "results": {"win": 0, "loss": 0, "scratch": 0}}
            _run_underlying(underlying, bars, vix_by_date, start, end,
                            "pyramid", lookback_days, conn, cfg, stats)
            runs.append({"underlying": underlying, "policy": "pyramid",
                         "lookback_days": lookback_days, "stats": stats})

    if owns_conn:
        report = compute_report(conn, drawdown_tolerance_pct=cfg["drawdown_tolerance_pct"])
        conn.close()
    else:
        report = compute_report(conn, drawdown_tolerance_pct=cfg["drawdown_tolerance_pct"])
    return {"runs": runs, "report": report}


# ------------------------------------------------------------- comparator

def _rows_for(conn, policy: str, lookback_days=None) -> list:
    q = ("SELECT r_multiple, pnl_net FROM simulated_trades "
        "WHERE journal_ref LIKE 'sim:h4:%' AND policy = ? "
        "AND r_multiple IS NOT NULL")
    params = [policy]
    if lookback_days is None:
        q += " AND lookback_days IS NULL"
    else:
        q += " AND lookback_days = ?"
        params.append(lookback_days)
    return conn.execute(q, params).fetchall()


def compute_report(conn, lookback_grid=None, drawdown_tolerance_pct=10.0) -> dict:
    """Reads back this run's own h4-namespaced rows (never
    src.simulator's production rows — the 'sim:h4:' prefix filter is the
    isolation boundary) and scores baseline vs each lookback's pyramid
    run with performance.py's own Sharpe/Sortino/max_drawdown math."""
    lookback_grid = lookback_grid or DEFAULTS["lookback_grid"]

    def _metrics(rows):
        rs = [r[0] for r in rows]
        pnls = [r[1] for r in rows]
        return {
            "n": len(rs),
            "sharpe": sharpe(rs), "sortino": sortino(rs),
            "max_drawdown_r": max_drawdown(rs),
            "max_drawdown_rs": max_drawdown(pnls),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        }

    baseline = _metrics(_rows_for(conn, "baseline"))
    grid = {}
    for lb in lookback_grid:
        pyramid = _metrics(_rows_for(conn, "pyramid", lb))
        verdict = "insufficient_data"
        if baseline["n"] >= 2 and pyramid["n"] >= 2:
            base_dd = baseline["max_drawdown_r"] or 0.0
            pyr_dd = pyramid["max_drawdown_r"] or 0.0
            dd_ok = (base_dd == 0 or pyr_dd <= base_dd * (1 + drawdown_tolerance_pct / 100))
            sortino_ok = (pyramid["sortino"] is not None and baseline["sortino"] is not None
                         and pyramid["sortino"] > baseline["sortino"])
            verdict = "graduates" if (dd_ok and sortino_ok) else "does_not_graduate"
        grid[lb] = {"metrics": pyramid, "verdict": verdict}

    return {"baseline": baseline, "pyramid_by_lookback": grid,
           "best_lookback": max(
               (lb for lb, r in grid.items() if r["verdict"] == "graduates"),
               key=lambda lb: grid[lb]["metrics"]["sortino"] or -999, default=None)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--underlying", default="NIFTY 50")
    args = ap.parse_args()

    conn = brain_map.connect()
    vix_by_date = _fetch_vix_series(args.start)
    if not vix_by_date:
        # Not fatal (the VIX gate then blocks range-bound structures —
        # documented live-outage behaviour), but it materially changes
        # which structures can be proposed, so it must never be silent.
        print("  ⚠️  WARNING: no India VIX history loaded — every day will "
              "face the no-VIX gate. Results are NOT comparable to a "
              "VIX-complete run.", file=sys.stderr)
    try:
        out = run_h4_experiment(args.start, args.end, (args.underlying,),
                                conn=conn, vix_by_date=vix_by_date)
    except H4DataError as e:
        print(f"\n❌ H4 EXPERIMENT ABORTED — {e}", file=sys.stderr)
        conn.close()
        sys.exit(1)
    conn.close()
    print(f"H4 experiment {args.start}..{args.end} on {args.underlying}")
    for run in out["runs"]:
        s = run["stats"]
        print(f"  {run['policy']:8s} lookback={run['lookback_days']} "
              f"proposed={s['proposed']} resolved={s['resolved']} "
              f"adds={s['adds']} trims={s['trims']}")
    print("Report:", out["report"])


if __name__ == "__main__":
    main()

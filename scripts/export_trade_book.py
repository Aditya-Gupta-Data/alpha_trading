#!/usr/bin/env python3
"""
scripts/export_trade_book.py — MANUAL OFFLINE TOOL (read-only trade-book export)
===============================================================================

Flattens `data/journal.jsonl` — the ledger of record for every trade this
system has taken — into one wide CSV for external P&L audit.

READ-ONLY, by construction: it opens the journal for reading, writes exactly
one CSV to a path you name, and imports nothing from `src/`. It cannot append
to the journal, cannot touch `brain_map.db`, and is on no execution path.

WHAT IS AND ISN'T A TRADE (the honesty rules this export inherits):

  * `decision: "rejected"` rows are PROPOSALS THAT NEVER OPENED. They carry a
    modelled outcome for learning purposes, not money. Excluded by default;
    `--include-rejected` brings them in, always flagged in the `decision`
    column so a rejected row can never be silently averaged into live P&L.
  * `outcome.hypothetical: true` rows are the decision-#31 tracked-but-not-real
    shadows — the same exclusion `src/performance.py` applies to Sharpe.
    Excluded by default; `--include-hypothetical` brings them in, flagged in
    the `hypothetical` column.
  * Rows with `outcome: null` are OPEN positions. Included always, with
    `status=OPEN` and empty P&L cells — an unrealized number is not a realized
    one and this file will not invent one.

NULL HONESTY: every cell that has no source value is left EMPTY, never zero.
A blank `r_multiple` means the journal never stamped one; a `0` would be a
claim that the trade broke even.

TWO DIFFERENT "CAPITAL" NUMBERS — both exported, because a P&L audit needs to
know which denominator it is using:
  * `max_risk_rs`      — the trade's defined maximum loss (spread `max_loss`,
                         or an equity plan's `max_loss_rs`). This is the
                         denominator of `r_multiple`.
  * `margin_blocked_rs`— the SPAN margin actually locked for a spread
                         (`spread.margin.total_margin`). Larger than max risk;
                         this is the capital-efficiency denominator.

TIME RESOLUTION: entry timestamps come from `created_at` (IST, ISO-8601) where
the row has one; the four pre-`short_id` legacy rows only ever recorded a date,
so their `entry_time` is empty. The journal records exits at DATE resolution
only — `exit_time` is therefore empty for every row, deliberately, rather than
filled with a plausible-looking market close.

Usage:
    python3 scripts/export_trade_book.py
    python3 scripts/export_trade_book.py --out /tmp/audit.csv --include-rejected
"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = ROOT / "data" / "journal.jsonl"
DEFAULT_OUT = ROOT / "trade_book_audit.csv"

COLUMNS = [
    "journal_line",        # always present — the file-order reference
    "trade_id",            # short_id; empty on pre-short_id legacy rows
    "desk",                # OPTIONS | EQUITY — never blend these blindly
    "mode",                # REAL_PAPER | PAPER_CAPITAL | PAPER_TELEMETRY
    "capital_at_risk",     # TRUE only when real paper capital was locked
    "status",              # OPEN | RESOLVED
    "decision",            # approved | rejected
    "hypothetical",        # TRUE on decision-#31 shadow rows
    "entry_date",
    "entry_time",
    "exit_date",
    "exit_time",           # always empty — journal resolves at date resolution
    "days_in_trade",
    "ticker",
    "strategy",            # humanised: "Bear Put Spread", "Equity BUY"
    "structure_raw",       # the raw stored key, for machine grouping
    "direction",
    "expiry",
    "lots",
    "lot_size",
    "qty",
    "entry_price",         # net debit/credit per unit for spreads; share price otherwise
    "exit_price",
    "max_risk_rs",         # defined max loss — the r_multiple denominator
    "max_profit_rs",
    "margin_blocked_rs",   # SPAN margin locked (spreads only)
    "realized_pnl_rs",     # NET of frictions, as the tracker stamped it
    "r_multiple",
    "pct_of_max",          # outcome.pct — % of max profit/loss captured
    "frictions_rs",
    "slippage_rs",
    "exit_reason",         # humanised resolution
    "exit_reason_raw",
    "exit_style",
    "verdict",             # the tracker's own one-line verdict text
    "position_closed",
    "regime_trend",
    "regime_vix",
    "signal",
]

# Humanised labels. Anything not listed falls back to Title Case of the raw
# key — a new strategy or resolution appears readably instead of vanishing.
STRATEGY_LABELS = {
    "bear_put_spread": "Bear Put Spread",
    "bull_call_spread": "Bull Call Spread",
    "bull_put_spread": "Bull Put Spread",
    "bear_call_spread": "Bear Call Spread",
    "iron_condor": "Iron Condor",
    "long_straddle": "Long Straddle",
    "long_strangle": "Long Strangle",
}

RESOLUTION_LABELS = {
    "stop_hit": "Stop Hit",
    "target_hit": "Target Hit",
    "profit_take": "Profit Take",
    "pre_expiry_exit": "Pre-Expiry Exit",
    "time_stop": "Time Stop",
    "expired": "Expired",
    "fundamental_break": "Fundamental Break",
    "strong_sell_tier": "Strong-Sell Tier Exit",
}


def humanise(raw, labels):
    """Known key → its label; unknown key → Title Case; None → ''."""
    if not raw:
        return ""
    return labels.get(raw, str(raw).replace("_", " ").title())


def split_timestamp(created_at, fallback_date):
    """('2026-07-24T09:15:31+05:30') -> ('2026-07-24', '09:15:31+05:30').

    Falls back to the row's `date` with an EMPTY time — the legacy rows
    genuinely never recorded a clock time and this will not guess one."""
    if created_at and "T" in str(created_at):
        date_part, _, time_part = str(created_at).partition("T")
        return date_part, time_part
    return created_at or fallback_date or "", ""


def flatten(entry, line_no):
    """One journal row -> one CSV dict. Never raises on a missing key."""
    outcome = entry.get("outcome") or {}
    spread = entry.get("spread") or {}
    plan = entry.get("plan") or {}
    regime = entry.get("regime") or {}
    margin = spread.get("margin") or {}
    levers = entry.get("risk_levers") or {}

    entry_date, entry_time = split_timestamp(entry.get("created_at"), entry.get("date"))

    if spread:
        structure_raw = spread.get("strategy") or "spread"
        strategy = humanise(structure_raw, STRATEGY_LABELS)
        # A debit spread's cost and a credit spread's premium are both the
        # per-unit entry price; whichever side the row recorded is the truth.
        entry_price = spread.get("net_debit")
        if entry_price is None:
            entry_price = spread.get("net_credit")
        max_risk = spread.get("max_loss")
        max_profit = spread.get("max_profit")
    else:
        structure_raw = (entry.get("pattern_tags") or [None])[0] or "equity"
        action = entry.get("action") or ""
        strategy = f"Equity {action}".strip()
        entry_price = entry.get("price")
        max_risk = plan.get("max_loss_rs")
        max_profit = None

    return {
        "journal_line": line_no,
        "trade_id": entry.get("short_id") or "",
        "desk": "OPTIONS",
        "mode": "REAL_PAPER",
        "capital_at_risk": "TRUE",
        "status": "RESOLVED" if outcome else "OPEN",
        "decision": entry.get("decision") or "",
        "hypothetical": "TRUE" if outcome.get("hypothetical") else "",
        "entry_date": entry_date,
        "entry_time": entry_time,
        "exit_date": outcome.get("exit_date") or "",
        "exit_time": "",
        "days_in_trade": outcome.get("days_in_trade"),
        "ticker": entry.get("ticker") or "",
        "strategy": strategy,
        "structure_raw": structure_raw,
        "direction": spread.get("direction") or "",
        "expiry": spread.get("expiry") or "",
        "lots": spread.get("lots"),
        "lot_size": spread.get("lot_size"),
        "qty": entry.get("shares"),
        "entry_price": entry_price,
        "exit_price": outcome.get("price"),
        "max_risk_rs": max_risk if max_risk is not None else levers.get("max_loss_rs"),
        "max_profit_rs": max_profit,
        "margin_blocked_rs": margin.get("total_margin"),
        "realized_pnl_rs": outcome.get("pnl_rs"),
        "r_multiple": outcome.get("r_multiple"),
        "pct_of_max": outcome.get("pct"),
        "frictions_rs": outcome.get("frictions_rs"),
        "slippage_rs": outcome.get("slippage_rs"),
        "exit_reason": humanise(outcome.get("resolution"), RESOLUTION_LABELS),
        "exit_reason_raw": outcome.get("resolution") or "",
        "exit_style": outcome.get("exit_style") or "",
        "verdict": outcome.get("verdict") or "",
        "position_closed": outcome.get("position_closed"),
        "regime_trend": regime.get("trend") or "",
        "regime_vix": regime.get("vix_band") or "",
        "signal": entry.get("signal") or "",
    }


EQUITY_SETUP_LABELS = {
    "block_vwap_pullback": "Equity Block-VWAP Pullback",
    "darling_ripe": "Equity Darling (legacy ripe)",
    "darling_buy": "Equity Darling Buy",
}

EQUITY_REASON_LABELS = {
    "stop_loss": "Stop Hit",
    "target": "Target Hit",
    "time_stop": "Time Stop",
    "fundamental_break": "Fundamental Break",
    "strong_sell_tier": "Strong-Sell Tier Exit",
}


def flatten_equity(entry_ev, exit_ev, line_no):
    """One paired equity-shadow (entry, exit|None) -> one CSV dict.

    THE CAPITAL DISTINCTION THAT MAKES OR BREAKS THIS AUDIT:
    the Shadow Equity Engine logs two populations into ONE ledger.

      * mode="PAPER_TELEMETRY" — `capital_allocated: 0`. These are pure
        signal telemetry ("log the false positives to train a better
        model"). They have an honest R-multiple but NO rupee P&L, because
        no rupees were ever at risk. Their `realized_pnl_rs` is left EMPTY,
        never 0 — a 0 would let them dilute a real rupee total while
        pretending to be break-even trades.
      * mode="PAPER_CAPITAL" — funded by the equity desk (decision #79),
        carrying a real `funding.qty` and `notional`. For these, and ONLY
        these, rupee P&L is computed as qty x (exit - entry).

    The `capital_at_risk` column carries this distinction into the CSV so
    any downstream pivot can filter on it in one step.
    """
    action = entry_ev.get("kya_kara_action") or {}
    trigger = entry_ev.get("kyu_trigger") or {}
    context = entry_ev.get("kaise_context") or {}
    funding = entry_ev.get("funding") or {}
    autopsy = (exit_ev or {}).get("kya_sikha_autopsy") or {}

    mode = entry_ev.get("mode") or ""
    funded = bool(funding.get("funded")) and mode == "PAPER_CAPITAL"
    qty = funding.get("qty") if funded else None

    entry_price = action.get("entry_price")
    stop = action.get("stop")
    exit_price = (exit_ev or {}).get("exit_price")

    # Rupee columns exist only where rupees existed. See docstring.
    pnl = max_risk = None
    if funded and qty:
        if entry_price is not None and stop is not None:
            max_risk = round(qty * (float(entry_price) - float(stop)), 2)
        if exit_ev and exit_price is not None and entry_price is not None:
            pnl = round(qty * (float(exit_price) - float(entry_price)), 2)

    entry_date, entry_time = split_timestamp(entry_ev.get("ts"), entry_ev.get("as_of"))
    exit_date, exit_time = split_timestamp((exit_ev or {}).get("ts"), None)

    setup_raw = trigger.get("setup") or "equity_shadow"
    nifty = context.get("nifty_trend") or {}

    return {
        "journal_line": line_no,
        "trade_id": entry_ev.get("id") or "",
        "desk": "EQUITY",
        "mode": mode,
        "capital_at_risk": "TRUE" if funded else "FALSE",
        "status": "RESOLVED" if exit_ev else "OPEN",
        "decision": "approved",   # a logged shadow entry IS the decision
        # NOT flagged `hypothetical` — that column means the decision-#31
        # options shadows, which are excluded by default. A zero-capital
        # equity row is a real signal that really fired; its honest caveat
        # is `capital_at_risk=FALSE`, and it must survive into the export.
        "hypothetical": "",
        "entry_date": entry_date,
        "entry_time": entry_time,
        "exit_date": exit_date,
        "exit_time": exit_time,
        "days_in_trade": autopsy.get("held_days"),
        "ticker": entry_ev.get("ticker") or "",
        "strategy": humanise(setup_raw, EQUITY_SETUP_LABELS),
        "structure_raw": setup_raw,
        "direction": action.get("side") or "",
        "expiry": "",
        "lots": "",
        "lot_size": "",
        "qty": qty,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "max_risk_rs": max_risk,
        "max_profit_rs": None,
        "margin_blocked_rs": funding.get("notional"),
        "realized_pnl_rs": pnl,
        "r_multiple": autopsy.get("r_multiple"),
        "pct_of_max": None,
        "frictions_rs": None,
        "slippage_rs": None,
        "exit_reason": humanise((exit_ev or {}).get("reason"), EQUITY_REASON_LABELS),
        "exit_reason_raw": (exit_ev or {}).get("reason") or "",
        "exit_style": "",
        "verdict": autopsy.get("category") or "",
        "position_closed": True if exit_ev else None,
        "regime_trend": "uptrend" if nifty.get("uptrend") else ("downtrend" if nifty.get("uptrend") is False else ""),
        "regime_vix": (context.get("vix") if context.get("vix") is not None else ""),
        "signal": trigger.get("signal") or "",
    }


def pair_equity_events(path):
    """Fold the append-only event stream into positions.

    The ledger is EVENTS, not trades: an entry and its exit are two lines
    sharing an `id`. Unpaired entries are open positions. An exit with no
    entry is reported, never silently dropped — it would mean the ledger
    lost a line."""
    entries, exits = {}, {}
    order = {}
    for line_no, ev in read_journal(path):
        ev_id = ev.get("id")
        if not ev_id:
            print(f"  ! equity line {line_no}: event has no id, skipped", file=sys.stderr)
            continue
        if ev.get("event") == "entry":
            entries[ev_id] = ev
            order.setdefault(ev_id, line_no)
        elif ev.get("event") == "exit":
            exits[ev_id] = ev
        else:
            print(f"  ! equity line {line_no}: unknown event type "
                  f"{ev.get('event')!r}, skipped", file=sys.stderr)

    orphans = set(exits) - set(entries)
    for ev_id in sorted(orphans):
        print(f"  ! equity exit {ev_id} has no matching entry — ledger gap, skipped",
              file=sys.stderr)

    return [flatten_equity(ev, exits.get(ev_id), order[ev_id])
            for ev_id, ev in entries.items()]


def read_journal(path):
    """Yield (line_no, entry). Junk-tolerant: a corrupt line is reported to
    stderr and skipped, never silently dropped and never fatal."""
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield line_no, json.loads(raw)
            except ValueError as exc:
                print(f"  ! line {line_no}: unparseable JSON, skipped ({exc})",
                      file=sys.stderr)


def _block(rows, label):
    """One desk's (or the whole book's) numbers.

    Rupee P&L and R-multiple are counted over DIFFERENT populations on
    purpose: rupee P&L over rows that actually risked rupees, R over every
    row the journal stamped an R on. Reporting a rupee total over rows whose
    capital was zero would be the single easiest way to fake this audit."""
    resolved = [r for r in rows if r["status"] == "RESOLVED"]
    open_rows = [r for r in rows if r["status"] == "OPEN"]
    with_pnl = [r for r in resolved if r["realized_pnl_rs"] is not None]
    with_r = [r for r in resolved if r["r_multiple"] is not None]

    lines = [f"  {label}",
             f"    rows {len(rows):<4} resolved {len(resolved):<4} open {len(open_rows)}"]
    if with_pnl:
        pnl = sum(float(r["realized_pnl_rs"]) for r in with_pnl)
        wins = sum(1 for r in with_pnl if float(r["realized_pnl_rs"]) > 0)
        lines.append(f"    net realized P&L : Rs. {pnl:>12,.2f}   "
                     f"(over {len(with_pnl)} capital-at-risk trades)")
        lines.append(f"    win rate (rupee) : {wins}/{len(with_pnl)} "
                     f"({100.0 * wins / len(with_pnl):.1f}%)")
    else:
        lines.append("    net realized P&L : n/a — no capital-at-risk resolved trades")
    if with_r:
        rs = [float(r["r_multiple"]) for r in with_r]
        rwins = sum(1 for x in rs if x > 0)
        lines.append(f"    avg R-multiple   : {sum(rs) / len(rs):>+8.3f}       "
                     f"(over {len(with_r)} R-stamped trades)")
        lines.append(f"    win rate (R)     : {rwins}/{len(with_r)} "
                     f"({100.0 * rwins / len(with_r):.1f}%)")
    zero_cap = [r for r in resolved if r["capital_at_risk"] == "FALSE"]
    if zero_cap:
        lines.append(f"    of which ZERO-CAPITAL telemetry: {len(zero_cap)} "
                     f"(R only, no rupee P&L by construction)")
    return lines


def summarise(rows):
    """Terminal sanity read: the whole book, then each desk on its own."""
    lines = _block(rows, "WHOLE DESK")
    for desk in ("OPTIONS", "EQUITY"):
        subset = [r for r in rows if r["desk"] == desk]
        if subset:
            lines.append("")
            lines += _block(subset, f"{desk} DESK")
    dates = sorted(r["exit_date"] for r in rows if r["exit_date"])
    if dates:
        lines += ["", f"  exit date range  : {dates[0]} -> {dates[-1]}"]
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export the resolved trade book to CSV (read-only).")
    ap.add_argument("--journal", default=str(DEFAULT_JOURNAL),
                    help=f"options journal path (default: {DEFAULT_JOURNAL})")
    ap.add_argument("--equity-journal", default=None,
                    help="equity shadow journal (logs/equity_shadow_journal.jsonl); "
                         "omitted = options desk only")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"CSV output path (default: {DEFAULT_OUT})")
    ap.add_argument("--include-rejected", action="store_true",
                    help="include proposals that were never opened (flagged in `decision`)")
    ap.add_argument("--include-hypothetical", action="store_true",
                    help="include decision-#31 shadow outcomes (flagged in `hypothetical`)")
    ap.add_argument("--resolved-only", action="store_true",
                    help="drop OPEN positions from the export")
    args = ap.parse_args(argv)

    journal_path = Path(args.journal)
    if not journal_path.exists():
        print(f"ERROR: no journal at {journal_path}", file=sys.stderr)
        return 1

    candidates = [flatten(entry, line_no)
                  for line_no, entry in read_journal(journal_path)]

    equity_path = None
    if args.equity_journal:
        equity_path = Path(args.equity_journal)
        if not equity_path.exists():
            print(f"ERROR: no equity journal at {equity_path}", file=sys.stderr)
            return 1
        candidates += pair_equity_events(equity_path)

    rows, skipped_rejected, skipped_hypo, skipped_open = [], 0, 0, 0
    for row in candidates:
        if row["decision"] == "rejected" and not args.include_rejected:
            skipped_rejected += 1
            continue
        if row["hypothetical"] and not args.include_hypothetical:
            skipped_hypo += 1
            continue
        if row["status"] == "OPEN" and args.resolved_only:
            skipped_open += 1
            continue
        rows.append(row)

    rows.sort(key=lambda r: (r["entry_date"], r["entry_time"], r["journal_line"]))

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in COLUMNS})

    print(f"\nTRADE BOOK EXPORT  <-  {journal_path}")
    if equity_path:
        print(f"                   <-  {equity_path}")
    print(f"                   ->  {out_path}\n")
    for line in summarise(rows):
        print(line)
    excluded = []
    if skipped_rejected:
        excluded.append(f"{skipped_rejected} rejected (never opened; --include-rejected)")
    if skipped_hypo:
        excluded.append(f"{skipped_hypo} hypothetical (#31 shadows; --include-hypothetical)")
    if skipped_open:
        excluded.append(f"{skipped_open} open (--resolved-only was set)")
    if excluded:
        print("\n  excluded:")
        for item in excluded:
            print(f"    - {item}")
    print("\n  Empty cells mean the journal never recorded that value. Not zero.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

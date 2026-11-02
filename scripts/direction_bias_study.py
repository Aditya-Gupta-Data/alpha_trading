#!/usr/bin/env python3
# MANUAL OFFLINE TOOL
"""
scripts/direction_bias_study.py — MANUAL OFFLINE TOOL (read-only)
=================================================================

The question (owner, 2026-11-02): on the 11-02 Live Book the primary held
6 bearish / 5 bullish / 4 neutral, the live-quote book 3 of 4 bearish and
the Shadow Learner 21 of 28 bearish — through a results-season rally. WHY
does the system keep picking the bearish side, and has it paid?

READ-ONLY. Reads `data/journal.jsonl`, `data/proposal_ledger.jsonl` and,
with `--db`, `paper_live_positions` (read-only URI). Writes stdout or the
one `--out` file you name. Changes no rule — the view classifier and the
advisory vetoes are Department 5 matters; this tool only measures them.

What it reports:
  1. Settled real trades by direction (approved, non-hypothetical):
     count, win rate, total and average P&L — overall and by entry month.
  2. What the view classifier said at entry (`regime_read`, stamped on
     every proposal since 2026-09-23): view x graded trend, so a bearish
     book can be split into MILD bearish (spot under both 50/200 SMAs but
     less than 2% under the 200) vs STRONG bearish.
  3. The proposal ledger: every evaluation by direction and fate, plus the
     count of BULLISH setups refused by the smart-money/sector advisory —
     there is no bearish counterpart to that veto, so this is the size of
     one known asymmetry.
  4. With `--db`: the crossed-quote books' closed rows (incl. the Shadow
     Learner, which takes raw setups with no advisory gate) by direction.

Honesty: a direction the journal did not stamp is counted as `unknown`,
never inferred from P&L. A cell under n = 20 is flagged (#86). Win = P&L
>= 0 after frictions.

Usage (on the VM, from ~/alpha_trading):
    venv/bin/python scripts/direction_bias_study.py
    venv/bin/python scripts/direction_bias_study.py --db data/brain_map.db --out /tmp/direction.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_N = 20
STRATEGY_DIRECTION = {"bear_put_spread": "bearish", "bear_call_spread": "bearish",
                      "bull_call_spread": "bullish", "bull_put_spread": "bullish",
                      "iron_condor": "neutral", "iron_butterfly": "neutral"}
# the smart-money / sector advisory veto on BULLISH index spreads (src/analysis/regime_filters.py:158)
BULLISH_VETO_MARKERS = ("smart-money/sector veto",)


def _jsonl(path) -> list:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def direction_of(spread: dict) -> str:
    d = str((spread or {}).get("direction") or "").lower()
    if d in ("bullish", "bearish", "neutral"):
        return d
    return STRATEGY_DIRECTION.get((spread or {}).get("strategy"), "unknown")


def settled_trades(journal_rows: list) -> list:
    """[{direction, month, pnl}] for approved, settled, non-hypothetical spreads."""
    out = []
    for e in journal_rows:
        s, o = e.get("spread"), e.get("outcome")
        if not s or e.get("decision") != "approved" or not o or o.get("hypothetical"):
            continue
        try:
            pnl = float(o["pnl_rs"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"direction": direction_of(s), "month": str(e.get("date") or "")[:7] or "unknown",
                    "pnl": pnl})
    return out


def scoreboard(trades: list, key: str = "direction") -> list:
    """[{group, n, wins, win_rate, pnl, avg}] sorted by group."""
    groups = {}
    for t in trades:
        groups.setdefault(t[key], []).append(t["pnl"])
    out = []
    for g in sorted(groups):
        p = groups[g]
        wins = sum(1 for x in p if x >= 0)
        out.append({"group": g, "n": len(p), "wins": wins,
                    "win_rate": round(100.0 * wins / len(p), 1),
                    "pnl": round(sum(p), 2), "avg": round(sum(p) / len(p), 2)})
    return out


def classifier_mix(journal_rows: list) -> dict:
    """{(view, grade): count} from every journal row carrying a regime_read
    (any decision — this is what the classifier SAID, not what traded)."""
    mix = {}
    for e in journal_rows:
        rr = e.get("regime_read")
        if not isinstance(rr, dict):
            continue
        k = (str(rr.get("view") or "unknown"), str(rr.get("grade") or "unknown"))
        mix[k] = mix.get(k, 0) + 1
    return mix


def ledger_mix(ledger_rows: list) -> tuple[dict, int, int]:
    """({(direction, fate): n}, bullish advisory vetoes, total evaluations)."""
    mix, vetoes = {}, 0
    for r in ledger_rows:
        d = r.get("direction") or STRATEGY_DIRECTION.get(r.get("strategy"), "unknown")
        k = (str(d), str(r.get("fate") or "unknown"))
        mix[k] = mix.get(k, 0) + 1
        reason = str(r.get("reason") or "").lower()
        if any(m in reason for m in BULLISH_VETO_MARKERS):
            vetoes += 1
    return mix, vetoes, len(ledger_rows)


def live_trades(db_path) -> tuple[list, str | None]:
    try:
        conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return [], f"database unreadable: {exc}"
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT account_id, strategy, direction, opened_at, pnl_net "
                            "FROM paper_live_positions WHERE state = 'closed'").fetchall()
    except sqlite3.Error as exc:
        return [], f"paper_live_positions unreadable: {exc}"
    finally:
        conn.close()
    out = []
    for r in rows:
        if r["pnl_net"] is None:
            continue
        out.append({"account": r["account_id"],
                    "direction": direction_of({"direction": r["direction"], "strategy": r["strategy"]}),
                    "month": str(r["opened_at"] or "")[:7] or "unknown", "pnl": float(r["pnl_net"])})
    return out, None


def _rs(x) -> str:
    return f"{'+' if x >= 0 else '-'}Rs.{abs(x):,.0f}"


def _board(rows: list, label: str) -> list:
    out = [f"| {label} | Trades | Wins | Win rate | Total P&L | Avg P&L |", "|---|---|---|---|---|---|"]
    for b in rows:
        flag = " ⚠️ n<20" if b["n"] < MIN_N else ""
        out.append(f"| {b['group']} | {b['n']}{flag} | {b['wins']} | {b['win_rate']}% | "
                   f"{_rs(b['pnl'])} | {_rs(b['avg'])} |")
    return out


def render(journal_rows: list, ledger_rows: list, live: list | None, notes: list) -> str:
    out = ["# Direction bias study", "", "Read-only; changes no rule.", ""]
    trades = settled_trades(journal_rows)
    out += [f"## 1. Settled real trades (PAPER_10L journal) — {len(trades)}", ""]
    if trades:
        out += _board(scoreboard(trades), "Direction") + [""]
        out += ["By entry month and direction:", ""]
        monthly = [dict(t, md=f"{t['month']} {t['direction']}") for t in trades]
        out += _board(scoreboard(monthly, key="md"), "Month / direction") + [""]
    else:
        out += ["No settled trades found.", ""]
    mix = classifier_mix(journal_rows)
    out += ["## 2. What the view classifier said at entry (`regime_read`)", ""]
    if mix:
        total = sum(mix.values())
        out += ["| View | Graded trend | Rows | Share |", "|---|---|---|---|"]
        for (view, grade), n in sorted(mix.items(), key=lambda kv: -kv[1]):
            out.append(f"| {view} | {grade} | {n} | {100.0 * n / total:.0f}% |")
        mild = sum(n for (v, g), n in mix.items() if v == "bearish" and g == "bearish")
        strong = sum(n for (v, g), n in mix.items() if v == "bearish" and g == "strong_bearish")
        out += ["", f"Bearish views: {mild} MILD (under both SMAs, less than 2% under the 200) vs "
                    f"{strong} STRONG.", ""]
    else:
        out += ["No row carries a regime_read (stamped from 2026-09-23).", ""]
    lm, vetoes, n_eval = ledger_mix(ledger_rows)
    out += [f"## 3. Proposal ledger — {n_eval} evaluations", ""]
    if lm:
        out += ["| Direction | Fate | Evaluations |", "|---|---|---|"]
        for (d, f), n in sorted(lm.items(), key=lambda kv: (kv[0][0], -kv[1])):
            out.append(f"| {d} | {f} | {n} |")
        out += ["", f"**Bullish setups refused by the smart-money/sector advisory: {vetoes}.** "
                    "There is no bearish counterpart to this veto.", ""]
    else:
        out += ["No ledger rows.", ""]
    if live is not None:
        out += [f"## 4. Crossed-quote books (closed rows) — {len(live)}", ""]
        for acct in sorted({t["account"] for t in live}):
            out += [f"**{acct}**", ""] + _board(scoreboard([t for t in live if t["account"] == acct]),
                                               "Direction") + [""]
        if not live:
            out += ["No closed rows.", ""]
    if notes:
        out += ["## Not read", ""] + [f"- {n}" for n in notes] + [""]
    out += ["## How to read this", "",
            "- A direction that wins less often AND loses more on average is not an edge, whatever the "
            "classifier says. Compare section 1 with section 4: the learner has no advisory gate, so its "
            "mix is the classifier's raw opinion.",
            "- Section 2 shows whether the bearish book is built on MILD reads. A mild read is the one "
            "most likely to be a pullback inside an uptrend, i.e. the side that loses in a rally.",
            "- Nothing here changes a rule. Any change to the classifier or the vetoes is a Department 5 "
            "decision, forward-tested from its own date."]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--journal", default=str(ROOT / "data" / "journal.jsonl"))
    ap.add_argument("--ledger", default=str(ROOT / "data" / "proposal_ledger.jsonl"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    notes = []
    journal = _jsonl(a.journal)
    if not journal:
        notes.append(f"journal empty or unreadable: {a.journal}")
    ledger = _jsonl(a.ledger)
    if not ledger:
        notes.append(f"proposal ledger empty or unreadable: {a.ledger}")
    live = None
    if a.db:
        live, err = live_trades(a.db)
        if err:
            notes.append(err)
    text = render(journal, ledger, live, notes)
    if a.out:
        Path(a.out).write_text(text)
        print(f"written: {a.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

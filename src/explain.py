"""
src/explain.py — why did this trade fire? One command, the whole story
======================================================================

    python3 -m src.explain <short_id>

Reconstructs a journaled trade from everything the system froze at
proposal time: the entry itself (plan/spread economics, decision, why),
the Phase-2 decision receipt (inputs, overrides, book, advisor lines),
the evidence snapshot (what all six layers said at that moment), and the
outcome + Brain-Map join when resolved. Read-only everywhere — the
defense against the classic unified-brain death where the system keeps
trading but nobody can audit a firing anymore.

No arguments lists the newest journaled entries with their short_ids.
"""

import json
import sys
from pathlib import Path

from src import journal

ROOT = Path(__file__).resolve().parent.parent


def _fmt_money(v) -> str:
    try:
        return f"Rs.{float(v):,.0f}"
    except (TypeError, ValueError):
        return "?"


def _entry_for(short_id: str, entries: list = None) -> dict | None:
    entries = entries if entries is not None else journal.read_all()
    for e in reversed(entries):
        if e.get("short_id") == short_id:
            return e
    return None


def explain(short_id: str, entries: list = None, conn=None) -> str:
    """The full reconstruction as printable text. Never raises — missing
    pieces render as honest absences, not guesses."""
    entry = _entry_for(short_id, entries)
    if entry is None:
        return (f"No journal entry with short_id '{short_id}'. "
                "Run without arguments to list recent entries.")

    lines = [f"═══ {entry.get('ticker', '?')} · {entry.get('date', '?')} · "
             f"id {short_id} ═══",
             f"decision: {entry.get('decision', '?')} · "
             f"signal: {entry.get('signal', '?')}"]
    if entry.get("why"):
        lines.append(f"why: {entry['why']}")

    spread = entry.get("spread")
    if spread:
        legs = spread.get("legs") or []
        lines.append(f"structure: {spread.get('strategy', '?')} "
                     f"({len(legs)} leg(s)) · max profit "
                     f"{_fmt_money(spread.get('max_profit_rs'))} · max loss "
                     f"{_fmt_money(spread.get('max_loss_rs'))}")
    plan = entry.get("plan")
    if plan:
        sl, tg = plan.get("stop_loss") or {}, plan.get("target") or {}
        lines.append(f"plan: stop {sl.get('price', '?')} · target "
                     f"{tg.get('price', '?')} · R:R {tg.get('rr', '?')}")

    receipt = entry.get("receipt")
    if receipt:
        a = receipt.get("analysis") or {}
        lines.append("─── receipt (frozen at proposal) ───")
        lines.append(f"vix {receipt.get('vix', '?')} · "
                     f"{'uptrend' if a.get('uptrend') else 'downtrend' if a.get('uptrend') is not None else 'trend ?'}"
                     f" · rsi {a.get('rsi', '?')} · spot {a.get('price', '?')}"
                     f" · book {receipt.get('book', '?')}")
        if receipt.get("vol_overrides"):
            lines.append(f"vol_bridge overrides: {receipt['vol_overrides']}")
        if receipt.get("memory_context"):
            lines.append(f"memory: {receipt['memory_context'][:160]}")
        if receipt.get("skeptic_note"):
            lines.append(f"skeptic: {receipt['skeptic_note'][:160]}")
    else:
        lines.append("(no receipt — journaled before the Phase-2 substrate)")

    evidence = entry.get("evidence")
    if evidence:
        from src.confluence.evidence import summarize
        lines.append("─── evidence (all layers, at proposal time) ───")
        lines.append(summarize(evidence))
    else:
        lines.append("(no evidence snapshot — pre-substrate entry)")

    outcome = entry.get("outcome")
    if outcome:
        lines.append("─── outcome ───")
        lines.append(f"{outcome.get('resolution', '?')} on "
                     f"{outcome.get('exit_date', '?')} · "
                     f"P&L {_fmt_money(outcome.get('pnl_rs'))} · "
                     f"R {outcome.get('r_multiple', 'n/a')} · "
                     f"{outcome.get('verdict', '')}")
    else:
        lines.append("(unresolved — the tracker hasn't closed this yet)")

    # Brain-Map join: is the evidence snapshot persisted against the ref?
    try:
        from src import brain_map
        from src.confluence.evidence import load_snapshot
        own = conn is None
        if conn is None:
            conn = brain_map.connect()
        try:
            ref = brain_map.journal_ref_for(entry)
            stored = load_snapshot(conn, ref)
            lines.append(f"brain_map: evidence_snapshots["
                         f"{ref}] {'present' if stored else 'absent'}")
        finally:
            if own:
                conn.close()
    except Exception:
        pass
    return "\n".join(lines)


def recent(entries: list = None, n: int = 10) -> str:
    entries = entries if entries is not None else journal.read_all()
    if not entries:
        return "(journal is empty)"
    lines = [f"{len(entries)} journal entries — newest {min(n, len(entries))}:"]
    for e in entries[-n:][::-1]:
        lines.append(f"  {e.get('short_id', '????????')} · "
                     f"{e.get('date', '?')} · {e.get('ticker', '?')} · "
                     f"{e.get('decision', '?')}"
                     f"{' · resolved' if e.get('outcome') else ''}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(explain(sys.argv[1]) if len(sys.argv) > 1 else recent())

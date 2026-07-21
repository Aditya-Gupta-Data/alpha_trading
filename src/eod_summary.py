"""
Alpha Trading — End-of-Day Summary broadcaster

Runs daily at 15:30 IST (10:00 UTC, market just closed) to push a terse
status card to the Discord channel:

    python3 -m src.eod_summary

Data sources (both local, no network other than the final Discord POST):
  data/journal.jsonl   — active approved positions + today's resolved exits
  data/brain_map.db    — today's outcomes rows (win/loss count)

Computes:
  * Daily MTM P&L       — sum of pnl_rs for exits with today's exit_date
  * Active positions    — approved entries with no outcome (spreads + equities)
  * Net delta exposure  — strategy-level directional bias across open spreads
  * Win/loss count      — from brain_map outcomes (cross-check vs journal)

Cron schedule on the VM (IST = UTC+5:30):
    0 10 * * 1-5  cd /home/aditya/alpha_trading && \
                  /home/aditya/alpha_trading/venv/bin/python3 -m src.eod_summary
"""

import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "brain_map.db"
JOURNAL_PATH    = ROOT / "data" / "journal.jsonl"

# Strategy-level net-delta bias approximation.
# bull call / bear put spreads carry directional exposure; iron condor /
# butterfly are balanced by construction (symmetric short strikes cancel).
# Multiplied by _ATM_DELTA × lots × lot_size to express in synthetic
# share-equivalents of the underlying.
_STRATEGY_DELTA_BIAS = {
    "bull_call_spread":  1.0,
    "bear_put_spread":  -1.0,
    "iron_condor":       0.0,
    "iron_butterfly":    0.0,
}
_ATM_DELTA = 0.5   # ATM-option delta approximation


def _today() -> str:
    return date.today().isoformat()


def _read_journal(path=None) -> list:
    p = Path(path or JOURNAL_PATH)
    if not p.exists():
        return []
    entries = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _open_approved_spreads(entries: list) -> list:
    return [
        e for e in entries
        if e.get("decision") == "approved"
        and e.get("outcome") is None
        and e.get("spread")
    ]


def _open_approved_equities(entries: list) -> list:
    return [
        e for e in entries
        if e.get("decision") == "approved"
        and e.get("outcome") is None
        and e.get("plan")
        and not e.get("spread")
    ]


def query_todays_resolutions(db_path=None) -> list:
    """Rows from brain_map.db outcomes table resolved on today's date."""
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.exists():
        return []
    today = _today()
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, archetype, r_multiple, result FROM outcomes WHERE date = ?",
            (today,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        print(f"  (eod_summary: brain_map query failed: {exc})")
        return []


def compute_net_delta_exposure(open_spreads: list) -> float:
    """Approximate net delta exposure (synthetic share-equivalents) across all
    open spread positions.

    Uses the strategy's directional bias (_STRATEGY_DELTA_BIAS) since individual
    leg strikes are not compared to a live spot price. Scaled by lots × lot_size.
    Unknown strategies contribute zero (market-neutral assumption).
    """
    net = 0.0
    for entry in open_spreads:
        spread = entry.get("spread") or {}
        strategy = spread.get("strategy", "")
        lots     = int(spread.get("lots", 1))
        lot_size = int(spread.get("lot_size", 1))
        qty      = lots * lot_size
        bias     = _STRATEGY_DELTA_BIAS.get(strategy, 0.0)
        net     += bias * _ATM_DELTA * qty
    return round(net, 2)


def build_eod_card(db_path=None) -> dict:
    """Build the EOD broadcast payload from journal + brain_map.db.

    Returns a payload dict ready for broadcast_alert(payload). Exported so
    tests can call it directly with mocked data sources.
    """
    today   = _today()
    entries = _read_journal()

    # Today's exits from the journal (approved entries that resolved today).
    todays_exits = [
        e for e in entries
        if (e.get("outcome") or {}).get("exit_date") == today
        and e.get("decision") == "approved"
    ]

    open_spreads   = _open_approved_spreads(entries)
    open_equities  = _open_approved_equities(entries)
    active_total   = len(open_spreads) + len(open_equities)

    # Daily MTM P&L from the journal's pnl_rs field (net of frictions).
    daily_pnl = sum(
        float((e.get("outcome") or {}).get("pnl_rs") or 0.0)
        for e in todays_exits
    )

    # Brain Map win/loss count for today (cross-check, not the primary P&L).
    db_rows = query_todays_resolutions(db_path=db_path)
    wins    = sum(1 for r in db_rows if r.get("result") == "win")
    losses  = sum(1 for r in db_rows if r.get("result") == "loss")

    # Net delta from open spread positions.
    net_delta = compute_net_delta_exposure(open_spreads)

    # Build Discord field list.
    fields: list = []

    if todays_exits:
        sign = "+" if daily_pnl >= 0 else ""
        fields.append({
            "name":   "Today's MTM P&L",
            "value":  f"Rs.{sign}{daily_pnl:,.0f}",
            "inline": True,
        })
        fields.append({
            "name":   "Resolved Today",
            "value":  f"{len(todays_exits)} trade(s)",
            "inline": True,
        })
    else:
        fields.append({"name": "Resolved Today", "value": "None", "inline": True})

    if wins + losses > 0:
        fields.append({
            "name":   "Brain Map W/L",
            "value":  f"{wins}W / {losses}L",
            "inline": True,
        })

    fields += [
        {"name": "Active Spreads",   "value": str(len(open_spreads)),  "inline": True},
        {"name": "Active Equities",  "value": str(len(open_equities)), "inline": True},
        {"name": "Total Active",     "value": str(active_total),       "inline": True},
    ]

    if net_delta != 0.0:
        direction = "long" if net_delta > 0 else "short"
        fields.append({
            "name":   "Net Delta",
            "value":  f"{net_delta:+.1f} ({direction} bias)",
            "inline": True,
        })
    else:
        fields.append({"name": "Net Delta", "value": "±0 (flat)", "inline": True})

    # One-firm-view (decision #82, VM-native since #83): the equity
    # desk's live book rides on this card too — all local, fail-open.
    try:
        from src import equity_desk
        fields.append({"name": "💼 Equity Desk",
                       "value": equity_desk.render_book_lines(),
                       "inline": False})
    except Exception:
        pass

    # Directive 4 (#84): everything the daily Discord budget spooled —
    # trades, rotations, sizing changes, review flags — lands HERE.
    try:
        from src.notifier import drain_digest_queue
        batched = drain_digest_queue()
        if batched:
            fields.append({"name": "📦 Batched signals",
                           "value": batched[:1024], "inline": False})
    except Exception:
        pass

    if active_total == 0 and not todays_exits:
        description = "No open positions. Engine idle until next signal."
    else:
        description = "Market closed. Open positions monitored by plan_tracker."

    return {
        "event":       "eod",
        "ticker":      "",
        "date":        today,
        "description": description,
        "fields":      fields,
    }


async def broadcast_eod(db_path=None) -> bool:
    """Build the EOD card and send it to Discord. Returns True on success."""
    from src.notifier import broadcast_alert
    payload = build_eod_card(db_path=db_path)
    return await broadcast_alert(payload)


def main() -> int:
    today = _today()
    print(f"Alpha Trading EOD Summary — {today}")
    ok = asyncio.run(broadcast_eod())
    if ok:
        print("EOD summary broadcast to Discord ✓")
    else:
        print("EOD summary Discord delivery failed "
              "(webhook unconfigured or unreachable)")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

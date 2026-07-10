"""
src/portfolio_report.py — the 2-hour intraday portfolio report card
===================================================================

A cron-triggered, strictly READ-ONLY snapshot of the open paper book,
posted to Discord as one embed: open position count, the current top
winner and top loser (marked live), and portfolio exposure (locked SPAN
margin vs. account equity).

Reuses what already exists instead of re-deriving anything:
  * open positions      — the same journal predicates as src/positions.py
  * live spread marks   — live_bridge.evaluate_open_positions(): the plan
                          tracker's exact exit arithmetic (modeled mark,
                          no-arbitrage clamp) at the current spot
  * equity marks        — live price vs. entry price × shares
  * spots               — the hardened SafeDhanClient (Phase 1)
  * exposure            — plain SELECTs on brain_map.db opened with the
                          SQLite READ-ONLY URI (mode=ro): this job can
                          never write or lock the database. It calls no
                          portfolio_manager function (those ensure schema
                          = a write) — missing tables just mean "no data".

Cron contract (scripts/setup_cron.sh, every 2 hours): the SCRIPT decides
whether posting makes sense — outside NSE market hours (Mon-Fri
09:15-15:30 IST) it exits quietly instead of spamming the channel at
02:00, so the crontab line stays a simple `0 */2 * * *`. `--force` posts
regardless (manual checks).

Run manually from the project folder:

    python3 -m src.portfolio_report            # posts only if market open
    python3 -m src.portfolio_report --force    # posts right now
"""

import argparse
import sqlite3
from pathlib import Path

from src import journal
from src.live_bridge import evaluate_open_positions
from src.market_loop import is_market_open, ist_now
from src.plan_tracker import _spread_trackable

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "brain_map.db"


# ------------------------------------------------------------- open book

def _open_entries(entries: list = None) -> tuple:
    """(open_spread_entries, open_equity_entries) — the same "approved and
    unresolved" predicates positions.py and the tracker use."""
    if entries is None:
        entries = journal.read_all()
    spreads, equities = [], []
    for e in entries:
        if e.get("decision") != "approved" or e.get("outcome") is not None:
            continue
        if e.get("spread") and _spread_trackable(e):
            spreads.append(e)
        elif (e.get("plan") or {}).get("stop_loss"):
            equities.append(e)
    return spreads, equities


def mark_positions(spread_entries: list, equity_entries: list,
                   spot_fn) -> list:
    """Every open position marked live: [{short_id, ticker, strategy,
    live_pnl_rs, detail}]. Positions whose ticker has no quote this cycle
    are skipped (never guessed) — the embed reports how many."""
    marked = []
    spots = {}
    for e in spread_entries + equity_entries:
        t = e.get("ticker")
        if t not in spots:
            try:
                spots[t] = spot_fn(t)
            except Exception:
                spots[t] = None

    for sig in evaluate_open_positions(
            {t: s for t, s in spots.items() if s is not None},
            entries=spread_entries):
        marked.append({"short_id": sig["short_id"], "ticker": sig["ticker"],
                       "strategy": sig["strategy"],
                       "live_pnl_rs": sig["live_pnl_rs"],
                       "detail": f"{sig['capture_pct']:.0f}% of max profit, "
                                 f"{sig['days_left']}d to expiry"})
    for e in equity_entries:
        spot = spots.get(e.get("ticker"))
        if spot is None or not e.get("price") or not e.get("shares"):
            continue
        pnl = round((float(spot) - float(e["price"])) * float(e["shares"]), 2)
        marked.append({"short_id": e.get("short_id"), "ticker": e["ticker"],
                       "strategy": (e.get("plan") or {}).get("variant") or "swing",
                       "live_pnl_rs": pnl,
                       "detail": f"entry Rs.{e['price']} → Rs.{spot}"})
    return marked


# -------------------------------------------------------------- exposure

def read_exposure(db_path: Path = None) -> dict | None:
    """Locked SPAN margin vs. account equity via read-only SELECTs.
    None when the capital tables don't exist yet (fresh DB) or the file
    is absent — the embed then simply omits the exposure field."""
    db_path = Path(db_path) if db_path is not None else DB_PATH
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            locked = conn.execute(
                "SELECT COALESCE(SUM(margin_rs), 0) FROM margin_locks "
                "WHERE released_at IS NULL").fetchone()[0]
            row = conn.execute(
                "SELECT starting_capital + realized_pnl FROM account_state "
                "WHERE id = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None   # tables not created yet — nothing to report
    if row is None:
        return None
    equity = float(row[0])
    locked = float(locked)
    return {"locked_margin_rs": round(locked, 2),
            "equity_rs": round(equity, 2),
            "exposure_pct": round(locked / equity * 100, 2) if equity else None}


# ----------------------------------------------------------------- embed

def _rs(v) -> str:
    return f"Rs.{v:+,.2f}" if v is not None else "n/a"


def build_report_payload(marked: list, open_count: int, unmarked: int,
                         exposure: dict | None, now) -> dict:
    """The broadcast_alert payload — pure function, tests pin its shape."""
    fields = [{"name": "Open Positions", "value": str(open_count),
               "inline": True}]
    if marked:
        winner = max(marked, key=lambda m: m["live_pnl_rs"])
        loser = min(marked, key=lambda m: m["live_pnl_rs"])
        net = round(sum(m["live_pnl_rs"] for m in marked), 2)
        fields.append({"name": "Net Live P&L (marked)",
                       "value": _rs(net), "inline": True})
        fields.append({"name": f"Top Winner — {winner['ticker']}",
                       "value": f"{_rs(winner['live_pnl_rs'])} "
                                f"({winner['detail']})", "inline": False})
        if loser["short_id"] != winner["short_id"]:
            fields.append({"name": f"Top Loser — {loser['ticker']}",
                           "value": f"{_rs(loser['live_pnl_rs'])} "
                                    f"({loser['detail']})", "inline": False})
    if exposure is not None:
        value = (f"Rs.{exposure['locked_margin_rs']:,.0f} locked of "
                 f"Rs.{exposure['equity_rs']:,.0f} equity")
        if exposure["exposure_pct"] is not None:
            value += f" ({exposure['exposure_pct']:.1f}%)"
        fields.append({"name": "Exposure", "value": value, "inline": False})
    if unmarked:
        fields.append({"name": "Unmarked",
                       "value": f"{unmarked} position(s) had no live quote "
                                "this cycle", "inline": False})
    return {
        "event": "portfolio_report",
        "ticker": "paper book",
        "date": now.date().isoformat(),
        "time": now.strftime("%Y-%m-%d %H:%M IST"),
        "description": "Automated 2-hourly snapshot — read-only; the plan "
                       "tracker owns every exit. Paper only.",
        "fields": fields,
    }


# ------------------------------------------------------------------ main

def run(entries: list = None, spot_fn=None, db_path: Path = None,
        now_fn=ist_now, notify_fn=None, force: bool = False) -> dict:
    """One report cycle. Everything injectable for offline tests.
    Returns {"posted": bool, "reason": str, "payload": dict-or-None}."""
    now = now_fn()
    if not force and not is_market_open(now):
        print(f"[Report Card] {now:%H:%M IST} — market closed, not posting.")
        return {"posted": False, "reason": "market closed", "payload": None}

    if spot_fn is None:
        from src.dhan_guard import SafeDhanClient
        spot_fn = SafeDhanClient().get_live_price
    if notify_fn is None:
        from src.notifier import fire_broadcast
        notify_fn = fire_broadcast

    spreads, equities = _open_entries(entries)
    open_count = len(spreads) + len(equities)
    marked = mark_positions(spreads, equities, spot_fn)
    exposure = read_exposure(db_path)
    payload = build_report_payload(marked, open_count,
                                   open_count - len(marked), exposure, now)
    notify_fn(payload)
    print(f"[Report Card] posted — {open_count} open, "
          f"{len(marked)} marked live.")
    return {"posted": True, "reason": "ok", "payload": payload}


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post the read-only portfolio report card to Discord")
    parser.add_argument("--force", action="store_true",
                        help="post even outside market hours")
    args = parser.parse_args(argv)
    run(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

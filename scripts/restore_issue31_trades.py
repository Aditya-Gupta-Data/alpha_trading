# MANUAL OFFLINE TOOL — run once, by hand, on the VM (decision #105, 2026-09-23).
"""
scripts/restore_issue31_trades.py — put back the spreads the #103 premium stop
cut retroactively (ledger Issue 31), exactly as if that rule had never run.

    venv/bin/python scripts/restore_issue31_trades.py --dry-run   # show, touch nothing
    venv/bin/python scripts/restore_issue31_trades.py             # backup, then restore

What "restore" means, per trade (the mirror image of one tracker settlement):
  journal.jsonl     outcome -> None (the trade is OPEN again). The removed
                    outcome dict is appended VERBATIM to
                    data/issue31_voided_outcomes.jsonl — nothing is lost.
  margin_locks      released_at -> NULL, pnl_net -> NULL (the margin is locked
                    again, same row, same locked_at).
  account_state     realized_pnl -= pnl_net (the loss is un-booked). peak is
                    untouched: a loss never raised it.
  portfolio.json    cash -= pnl_net (the paper cash the settlement moved).
  account_events    one 'issue31_restore' row per trade — the audit trail.
  equity_curve      one new point after the restore (append-only, never edited).
Deliberately NOT touched:
  trade_tickets     the FILLED EXIT tickets stay — the OMS ledger is append-only;
                    they record a fill the venue did make, since voided here.
  outcomes          the brain_map rows stay — `brain_map.record_outcome` upserts
                    by journal_ref, so the REAL resolution overwrites each one
                    when it comes; the loss-permanence invariant (#33) is not
                    broken by hand.
Guards: refuses any ref whose journal outcome is not `stop_loss`, whose lock is
not released, or whose lock pnl_net differs from the journal pnl by > Rs.0.01.
Idempotent: a ref already OPEN is skipped, not double-restored.
"""
import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import journal, portfolio as pf                        # noqa: E402
from src import portfolio_manager as pm                          # noqa: E402
from src.brain_map import DEFAULT_DB_PATH                        # noqa: E402

REFS = ("f8356c9c", "bd73554d", "efe1681e", "54365ef1", "2ff3443a")
VOIDED_PATH = ROOT / "data" / "issue31_voided_outcomes.jsonl"


def _backup(paths, stamp):
    out = []
    for p in paths:
        if p.exists():
            b = p.with_name(f"{p.name}.bak-issue31-{stamp}")
            shutil.copy2(p, b)
            out.append(b)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    db_path = Path(args.db)

    entries = journal.read_all()
    by_id = {e.get("short_id"): e for e in entries}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    plan, skipped = [], []
    for ref in REFS:
        e = by_id.get(ref)
        if e is None:
            skipped.append((ref, "not in journal")); continue
        o = e.get("outcome")
        if not o:
            skipped.append((ref, "already OPEN")); continue
        if o.get("resolution") != "stop_loss":
            skipped.append((ref, f"resolution is {o.get('resolution')!r}, not stop_loss — refused")); continue
        lock = conn.execute("SELECT * FROM margin_locks WHERE journal_ref = ?", (ref,)).fetchone()
        if lock is None or lock["released_at"] is None:
            skipped.append((ref, "lock missing or still active — refused")); continue
        if abs(float(lock["pnl_net"]) - float(o["pnl_rs"])) > 0.01:
            skipped.append((ref, f"lock pnl {lock['pnl_net']} != journal pnl {o['pnl_rs']} — refused")); continue
        plan.append((ref, e, o, float(o["pnl_rs"])))

    before = pm.account_summary(conn)
    total = round(sum(p[3] for p in plan), 2)
    print(f"account before: realized {before['realized_pnl']:,.2f} · equity {before['equity']:,.2f} "
          f"· locked {before['locked_margin']:,.2f} · open locks {before['open_locks']}")
    for ref, e, o, pnl in plan:
        print(f"  RESTORE {ref} {e['ticker']:<18} exit_date {o['exit_date']} pnl {pnl:+,.2f} "
              f"ticket {(o.get('execution') or {}).get('ticket_id')}")
    for ref, why in skipped:
        print(f"  skip    {ref}: {why}")
    print(f"un-book total {total:+,.2f} -> realized would be {before['realized_pnl'] - total:,.2f}")
    if args.dry_run or not plan:
        print("(dry run — nothing changed)" if args.dry_run else "(nothing to do)")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = _backup([db_path, journal.JOURNAL_PATH, pf.PORTFOLIO_PATH], stamp)
    print("backups:", *[str(b) for b in backups], sep="\n  ")

    # 1. audit sidecar first — the outcome must exist somewhere before it is cleared
    with VOIDED_PATH.open("a") as f:
        for ref, e, o, pnl in plan:
            f.write(json.dumps({"short_id": ref, "ticker": e["ticker"], "voided_at": stamp,
                                "reason": "Issue 31 — #103 premium stop reversed (decision #105)",
                                "outcome": o}) + "\n")
    # 2. DB, one transaction
    try:
        for ref, e, o, pnl in plan:
            conn.execute("UPDATE margin_locks SET released_at = NULL, pnl_net = NULL "
                         "WHERE journal_ref = ?", (ref,))
            conn.execute("UPDATE account_state SET realized_pnl = round(realized_pnl - ?, 2) "
                         "WHERE id = 1", (pnl,))
            pm.log_event(conn, "issue31_restore",
                         f"{ref} {e['ticker']} re-opened: #103 stop_loss of Rs.{pnl:,.2f} "
                         f"(exit_date {o['exit_date']}) un-booked, margin re-locked "
                         f"(decision #105)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    snap = pm._snapshot_equity(conn)
    # 3. journal
    for ref, e, o, pnl in plan:
        e["outcome"] = None
    journal.rewrite_all(entries)
    # 4. paper cash
    book = pf.load()
    book["cash"] = round(book["cash"] - total, 2)
    pf.save(book)

    after = pm.account_summary(conn)
    print(f"account after:  realized {after['realized_pnl']:,.2f} · equity {after['equity']:,.2f} "
          f"· locked {after['locked_margin']:,.2f} · open locks {after['open_locks']} "
          f"· drawdown {snap['drawdown_pct']:.2f}%")
    reopened = [x for x in journal.read_all() if x.get("short_id") in REFS and not x.get("outcome")]
    print(f"journal: {len(reopened)} of {len(plan)} restored refs now OPEN; voided outcomes -> {VOIDED_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

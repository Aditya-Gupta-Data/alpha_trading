# MANUAL OFFLINE TOOL — run by hand on the VM, market CLOSED (ledger Issue 41, 2026-10-04).
#   venv/bin/python scripts/void_phantom_trade.py --ref 7f4a4897 --why "..."          # DRY RUN: prints the plan, writes nothing
#   venv/bin/python scripts/void_phantom_trade.py --ref 7f4a4897 --why "..." --yes    # hard-backup, then void
"""
VOID one approved, still-open options trade at ZERO across every account.

Why this exists: on Fri 2026-10-02 (Gandhi Jayanti, NSE closed) the engine
ran a full session on frozen data and FILLED the NIFTY FIN SERVICE bear put
`7f4a4897` in PAPER_10L, PAPER_2L and PAPER_2L_ROT. The trade never existed
on a real market. The owner ruled (2026-10-04): void it at zero.

A VOID IS NOT A SETTLEMENT. No price is read, no P&L and no friction is
booked, the outcome ledger (brain_map `outcomes`) is not written:

  journal row   decision "approved" -> "voided"; an `outcome` with
                resolution "voided", hypothetical true, pnl_rs null (so the
                tracker stops tracking it and every stats reader, which
                skips hypothetical or non-approved rows, skips it); a
                `void` stamp naming who, when, why and what was released.
                The original decision and the per-account verdicts are
                kept under `void.before`.
  margin locks  the primary lock and every shadow lock are released at
                pnl 0 through `pm.release_entry` — the same door a human
                rejection uses.
  events        one `trade_voided` row in `account_events` and one per
                shadow account in `paper_account_events`.
  OMS tickets   left untouched (the append-only record of what the paper
                venue did); recon already drops ticket-only rows whose
                journal row is resolved.

REFUSES when: the market is open; the row is missing, not approved, already
resolved, or not a spread; an EXIT ticket exists for it; or a live-quote
account holds an open live position on it (that account settles itself).

Before any write: brain_map.db is copied with sqlite's online backup API
(integrity-checked) and journal.jsonl is copied under the journal lock.
Every row touched is appended VERBATIM to data/voided_trades.jsonl first.
Idempotent: a second run finds the row already voided and does nothing.
"""
import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import brain_map, journal, portfolio_manager as pm  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
ARCHIVE_NAME = "voided_trades.jsonl"
VOID_EVENT = "trade_voided"
VOID_DECISION = "voided"


def _now() -> datetime:
    return datetime.now(IST)


def _rows(conn, sql: str, args: tuple = ()) -> list:
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, tuple(r))) for r in cur.fetchall()]


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def snapshot(conn, ref: str) -> dict:
    """Everything the database holds for `ref`, verbatim."""
    tables = _tables(conn)
    snap = {"primary_locks": [], "paper_locks": [], "tickets": [], "live_positions": []}
    if "margin_locks" in tables:
        snap["primary_locks"] = _rows(conn, "SELECT * FROM margin_locks WHERE journal_ref = ?", (ref,))
    if "paper_margin_locks" in tables:
        snap["paper_locks"] = _rows(conn, "SELECT * FROM paper_margin_locks WHERE journal_ref = ?", (ref,))
    if "trade_tickets" in tables:
        snap["tickets"] = _rows(conn, "SELECT * FROM trade_tickets WHERE journal_ref = ?", (ref,))
    if "paper_live_positions" in tables:
        snap["live_positions"] = _rows(
            conn, "SELECT account_id, journal_ref, state FROM paper_live_positions WHERE journal_ref = ?", (ref,))
    return snap


def build_plan(conn, ref: str, entry: dict | None, market_open: bool) -> dict:
    """{ok, already_voided, refusals: [...], entry, snapshot, release: {...}}.
    Pure: reads only."""
    snap = snapshot(conn, ref)
    plan = {"ref": ref, "ok": False, "already_voided": False, "refusals": [], "entry": entry,
            "snapshot": snap, "release": {"primary": 0.0, "shadows": {}}}
    if entry is None:
        plan["refusals"].append(f"no journal row with short_id {ref}")
        return plan
    if entry.get("decision") == VOID_DECISION:
        plan["already_voided"] = True
        return plan
    if market_open:
        plan["refusals"].append("the market is open — a void is an offline repair")
    if entry.get("decision") != "approved":
        plan["refusals"].append(f"decision is {entry.get('decision')!r}, not 'approved'")
    if entry.get("outcome") is not None:
        plan["refusals"].append("the row is already resolved (it has an outcome)")
    if not (entry.get("spread") or {}).get("legs"):
        plan["refusals"].append("not a spread entry")
    exits = [t for t in snap["tickets"]
             if str(t.get("kind") or "").upper() == "EXIT" or str(t.get("note") or "").startswith("EXIT ")]
    if exits:
        plan["refusals"].append(f"{len(exits)} EXIT ticket(s) exist — the trade was (partly) exited")
    held = [p for p in snap["live_positions"] if p.get("state") != "closed"]
    if held:
        plan["refusals"].append("a live-quote account holds an open live position on it "
                                f"({', '.join(p['account_id'] for p in held)})")
    for lock in snap["primary_locks"]:
        if lock.get("released_at") is None:
            plan["release"]["primary"] += float(lock["margin_rs"])
    for lock in snap["paper_locks"]:
        if lock.get("released_at") is None:
            acct = lock["account_id"]
            plan["release"]["shadows"][acct] = plan["release"]["shadows"].get(acct, 0.0) + float(lock["margin_rs"])
    plan["release"]["total"] = round(plan["release"]["primary"] + sum(plan["release"]["shadows"].values()), 2)
    plan["ok"] = not plan["refusals"]
    return plan


def backup_db(db_path: Path, stamp: str) -> Path:
    dest = db_path.with_name(f"{db_path.name}.bak-void-{stamp}")
    src = sqlite3.connect(str(db_path), timeout=brain_map.BUSY_TIMEOUT_SECONDS)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        raise SystemExit(f"backup {dest} failed integrity_check: {ok}")
    return dest


def backup_journal(stamp: str) -> Path:
    dest = journal.JOURNAL_PATH.with_name(f"{journal.JOURNAL_PATH.name}.bak-void-{stamp}")
    with journal.locked():
        shutil.copy2(journal.JOURNAL_PATH, dest)
    return dest


def void_trade(conn, ref: str, why: str, who: str, archive_path: Path, now: datetime = None) -> dict:
    """The mutation. Caller has already backed up. Journal first (under its
    lock, validated on the FRESH row), then the locks, then the events — so
    a tracker pass that lands in between sees a resolved, non-approved row
    and at worst releases the same locks at the same zero."""
    now = now or _now()
    stamp = now.replace(tzinfo=None).isoformat(timespec="seconds")
    result = {"ref": ref, "voided": False}
    with journal.locked():
        fresh = journal.get_entry(ref)
        plan = build_plan(conn, ref, fresh, market_open=False)
        if plan["already_voided"]:
            return dict(result, reason="already voided")
        if not plan["ok"]:
            return dict(result, reason="; ".join(plan["refusals"]))
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with open(archive_path, "a") as f:
            f.write(json.dumps({"voided_at": stamp, "ref": ref, "why": why, "who": who,
                                "journal_row": fresh, "database": plan["snapshot"]},
                               default=str, sort_keys=True) + "\n")
        released = plan["release"]

        def _mutate(e):
            if e.get("decision") != "approved" or e.get("outcome") is not None:
                return False
            before = {"decision": e.get("decision"),
                      "accounts": json.loads(json.dumps(e.get("accounts"), default=str))}
            e["decision"] = VOID_DECISION
            e["outcome"] = {"checked": now.date().isoformat(), "settled_at": stamp,
                            "resolution": "voided", "hypothetical": True, "void": True,
                            "pnl_rs": None, "r_multiple": None,
                            "verdict": f"VOID — never a real trade: {why}"}
            for acct, verdict in (e.get("accounts") or {}).items():
                if isinstance(verdict, dict) and verdict.get("status") == "approved":
                    verdict.update(status="voided", voided_at=stamp)
            e["void"] = {"at": stamp, "by": who, "why": why, "before": before,
                         "margin_released_rs": released}
            return True

        if journal.update_entry(ref, _mutate) is None:
            return dict(result, reason="the journal row changed under the lock — nothing written")
        res = pm.release_entry(ref, 0.0, conn=conn, wealth_sweep=False)
        result["release"] = res
        detail = (f"{ref} voided at zero by {who}: {why} "
                  f"(margin released Rs.{released['total']:,.2f}; no P&L, no frictions)")
        pm.log_event(conn, VOID_EVENT, detail)
        for acct in released["shadows"]:
            pm.paper_log_event(conn, acct, VOID_EVENT, ref, detail)
        conn.commit()
    after = snapshot(conn, ref)
    still = ([l for l in after["primary_locks"] if l.get("released_at") is None]
             + [l for l in after["paper_locks"] if l.get("released_at") is None])
    row = journal.get_entry(ref) or {}
    result.update(voided=row.get("decision") == VOID_DECISION and not still,
                  locks_still_open=len(still), margin_released_rs=released,
                  reason="voided" if not still else "journal voided but a lock is still open — see release")
    return result


def _account_lines(conn) -> list:
    out = []
    s = pm.account_summary(conn)
    out.append(f"  {pm.ACCOUNT_PAPER_10L}: free cash Rs.{s['available_cash']:,.2f} · locked "
               f"Rs.{s['locked_margin']:,.2f} · realized Rs.{s['realized_pnl']:,.2f} · {s['open_locks']} lock(s)")
    for acct in pm.shadow_account_ids():
        p = pm.paper_account_summary(conn, acct)
        out.append(f"  {acct}: free cash Rs.{p['available_cash']:,.2f} · locked "
                   f"Rs.{p['locked_margin']:,.2f} · realized Rs.{p['realized_pnl']:,.2f}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Void one phantom options trade at zero (ledger Issue 41).")
    ap.add_argument("--ref", required=True, help="the journal short_id, e.g. 7f4a4897")
    ap.add_argument("--why", default="", help="the stated reason, written to the journal row and the events")
    ap.add_argument("--who", default="owner (CLI)")
    ap.add_argument("--yes", action="store_true", help="execute; without it this is a dry run")
    args = ap.parse_args(argv)
    if not args.why.strip():
        print("Refusing to void without --why (the reason is written to the permanent record).")
        return 1

    from src.market_loop import is_market_open
    conn = brain_map.connect()
    try:
        pm.ensure_schema(conn)
        pm.ensure_accounts_schema(conn)
        plan = build_plan(conn, args.ref, journal.get_entry(args.ref), market_open=is_market_open())
        if plan["already_voided"]:
            print(f"Nothing to do: {args.ref} is already voided.")
            return 0
        e = plan["entry"] or {}
        s = e.get("spread") or {}
        print(f"VOID PLAN for {args.ref}: {e.get('ticker')} {s.get('strategy')} "
              f"exp {s.get('expiry')}, entered {e.get('date')}")
        print(f"  primary lock to release at zero: Rs.{plan['release']['primary']:,.2f}")
        for acct, m in plan["release"]["shadows"].items():
            print(f"  {acct} lock to release at zero: Rs.{m:,.2f}")
        print(f"  total margin released: Rs.{plan['release'].get('total', 0.0):,.2f}")
        print(f"  tickets left untouched: {[t['ticket_id'] for t in plan['snapshot']['tickets']]}")
        print("Accounts BEFORE:", *_account_lines(conn), sep="\n")
        if not plan["ok"]:
            print("REFUSED:", *[f"  - {r}" for r in plan["refusals"]], sep="\n")
            return 1
        if not args.yes:
            print("(dry run — nothing changed; re-run with --yes to void)")
            return 0
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        db_bak = backup_db(Path(brain_map.DEFAULT_DB_PATH), stamp)
        j_bak = backup_journal(stamp)
        print(f"backups:\n  {db_bak}\n  {j_bak}")
        archive = Path(brain_map.DEFAULT_DB_PATH).parent / ARCHIVE_NAME
        out = void_trade(conn, args.ref, args.why.strip(), args.who, archive)
        print(json.dumps({k: v for k, v in out.items() if k != "release"}, indent=1, default=str))
        print("Accounts AFTER:", *_account_lines(conn), sep="\n")
        if out.get("voided"):
            print(f"VERIFIED: {args.ref} is voided, every lock is released at zero. Archive: {archive}")
            return 0
        print(f"NOT VOIDED: {out.get('reason')}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

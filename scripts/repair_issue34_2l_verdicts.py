# MANUAL OFFLINE TOOL — run once, by hand, on the VM (ledger Issue 34, 2026-09-28).
#   venv/bin/python scripts/repair_issue34_2l_verdicts.py          # DRY RUN: prints the plan, writes nothing
#   venv/bin/python scripts/repair_issue34_2l_verdicts.py --yes    # backup the journal, then repair
"""
Issue 34: on 2026-09-25 the owner's approval of 14 stale pending proposals
re-judged PAPER_2L on liquid cash that already excluded each entry's OWN
lock (the #102 re-judge bug, fixed by #115 in c79c4c9), so 7 journal rows
carry `accounts.PAPER_2L.status = "rejected"` although PAPER_2L holds (or
has since settled) an active `paper_margin_locks` row for them.

THE LOCK TABLE IS THE TRUTH (it is what settles): this tool makes the
journal agree with it. For every PAPER_2L lock whose journal verdict is not
`approved`, the verdict becomes `approved` at the lock's own `lots` and
`margin_rs`; the wrong verdict is archived verbatim under `repaired_from`
(RULE 3 — nothing is deleted). One `issue34_repair` row per trade lands in
`paper_account_events`. Idempotent: a second run finds nothing to do.

NOT repaired, said plainly: no PAPER_2L ENTRY ticket was ever issued for
these 7 (the OMS ledger is append-only history — issuing one now would
back-date a fill that never happened), and the two that already settled
(09-25 ratchet_hit) got no PAPER_2L EXIT ticket for the same reason. Their
P&L is correctly in the 2L ledger via release_shadow_locks.
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import brain_map, journal, portfolio_manager as pm  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT = pm.ACCOUNT_PAPER_2L
EVENT = "issue34_repair"
WHY = ("Issue 34 repair 2026-09-28: verdict re-judged on cash that excluded this "
       "entry's own lock (#102 re-judge bug, fixed #115); the lock table held it")


def plan(conn, entries: list) -> list:
    """[{journal_ref, ticker, lots, margin_rs, released_at, was}] — every
    PAPER_2L lock (active or settled) whose journal verdict is not approved.
    Pure: reads only."""
    pm.ensure_accounts_schema(conn)
    byid = {e.get("short_id"): e for e in entries}
    out = []
    rows = conn.execute("SELECT journal_ref, lots, margin_rs, released_at FROM paper_margin_locks "
                        "WHERE account_id = ? ORDER BY locked_at, rowid", (ACCOUNT,)).fetchall()
    for ref, lots, margin, released in (tuple(r) for r in rows):
        e = byid.get(ref)
        if e is None:
            continue
        v = (e.get("accounts") or {}).get(ACCOUNT) or {}
        if v.get("status") == "approved":
            continue
        out.append({"journal_ref": ref, "ticker": e.get("ticker"), "lots": int(lots),
                    "margin_rs": float(margin), "released_at": released, "was": dict(v)})
    return out


def repaired_verdict(item: dict, now: str) -> dict:
    return {"status": "approved", "lots": item["lots"], "margin_rs": item["margin_rs"],
            "reason": "margin locked (Issue 34 repair — verdict corrected to the lock table)",
            "repaired_from": item["was"], "repaired_at": now, "repair": EVENT}


def apply(conn, items: list, now: str = None) -> list:
    """Mutate the journal (race-safe update_entry) + log one event per trade."""
    now = now or datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
    done = []
    for it in items:
        new = repaired_verdict(it, now)

        def _mutate(e, _new=new):
            acc = e.setdefault("accounts", {})
            if not isinstance(acc, dict):
                return False
            if (acc.get(ACCOUNT) or {}).get("status") == "approved":
                return False                      # someone repaired it first
            acc[ACCOUNT] = _new
            return True
        if journal.update_entry(it["journal_ref"], _mutate) is None:
            print(f"  ! {it['journal_ref']} not written (missing or already approved)")
            continue
        pm.paper_log_event(conn, ACCOUNT, EVENT, it["journal_ref"], json.dumps({
            "ticker": it["ticker"], "lots": it["lots"], "margin_rs": it["margin_rs"],
            "released_at": it["released_at"], "was": it["was"].get("status"),
            "was_reason": (it["was"].get("reason") or "")[:120], "why": WHY}, sort_keys=True))
        done.append(it["journal_ref"])
    return done


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    conn = brain_map.connect()
    try:
        items = plan(conn, journal.read_all())
        print(f"PAPER_2L verdicts to repair: {len(items)}")
        for it in items:
            print(f"  {it['journal_ref']} {it['ticker']:<18} lots {it['lots']} margin Rs.{it['margin_rs']:,.0f} "
                  f"{'SETTLED ' + it['released_at'] if it['released_at'] else 'open'}  "
                  f"was: {it['was'].get('status')}")
        if not items:
            print("nothing to do")
            return 0
        if "--yes" not in argv:
            print("\nDRY RUN — nothing written. Re-run with --yes to repair.")
            return 0
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        bak = journal.JOURNAL_PATH.with_name(f"journal.jsonl.bak-issue34-{stamp}")
        shutil.copy2(journal.JOURNAL_PATH, bak)
        print(f"backup: {bak}")
        done = apply(conn, items)
        print(f"repaired {len(done)}: {', '.join(done)}")
        left = plan(conn, journal.read_all())
        print(f"remaining mismatches: {len(left)}")
        return 0 if not left else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

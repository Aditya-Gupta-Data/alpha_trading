# MANUAL OFFLINE TOOL — run once, by hand, on the VM (audit Chunk 1 D5, decision #121, 2026-09-30).
#   venv/bin/python scripts/repair_d5_brain_map_outcomes.py          # DRY RUN: prints the plan, writes nothing
#   venv/bin/python scripts/repair_d5_brain_map_outcomes.py --yes    # hard-backup brain_map.db, then repair
"""
D5: `brain_map.record_outcome` is insert-or-ignore, so the five Issue-31
stop-loss outcomes (voided by #105 on 2026-09-23, when the trades were
restored to OPEN) were never overwritten by the trades' real resolutions.
Every Brain Map consumer (strategy_stats, similar-event recall, the MCP
tools, edge decay, cluster hit-rates) read the voided results.

THE JOURNAL IS THE TRUTH (architect ruling 2026-09-30): this tool makes
`outcomes` agree with it, for these five refs only.

  journal row resolved   -> the outcomes row is OVERWRITTEN in place (same
                            id, so its event links survive) with the values
                            record_resolved_entry derives from the journal:
                            date, archetype, r_multiple, result, regime.
                            post_mortem -> NULL: the stored one analysed the
                            voided stop (an LLM is not re-run by a repair).
  journal row still open -> the outcomes row and its event links are
                            DELETED; the real resolution records it fresh.

Every row touched is first appended VERBATIM to
data/d5_brain_map_outcome_repairs.jsonl, and the whole database is copied
with sqlite's online backup API (a consistent snapshot, integrity-checked)
before any write. Idempotent: a second run finds nothing to do.

It also REPORTS (never touches) any other outcomes row whose result
disagrees with its journal row — the owner authorised these five.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import brain_map, journal  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
REFS = ("f8356c9c", "bd73554d", "efe1681e", "54365ef1", "2ff3443a")
ARCHIVE_NAME = "d5_brain_map_outcome_repairs.jsonl"


# The two repair statements live HERE, in this one-off tool, and nowhere in
# src/: the outcome ledger stays append-only by contract for every live path
# (tests/test_loss_permanence.py guards src/). Decision #121 is the one
# owner-authorised exception, for these five rows.

def replace_outcome(conn, journal_ref, values: dict) -> dict:
    """Overwrite ONE existing row in place (same id, so its event links
    survive) with values re-derived from the journal; post_mortem -> NULL."""
    before = conn.execute("SELECT * FROM outcomes WHERE journal_ref = ?", (journal_ref,)).fetchone()
    if before is None:
        return {"replaced": False, "before": None}
    conn.execute(
        "UPDATE outcomes SET date = ?, ticker = ?, archetype = ?, r_multiple = ?, result = ?, "
        "post_mortem = NULL, regime_trend = ?, regime_vix = ? WHERE journal_ref = ?",
        (values["date"], values["ticker"], values["archetype"], values["r_multiple"],
         values["result"], values["regime_trend"], values["regime_vix"], journal_ref))
    conn.commit()
    return {"replaced": True, "before": dict(before)}


def delete_outcome(conn, journal_ref) -> dict:
    """Remove the outcome of a trade the journal says is still OPEN, with
    its event links, in one transaction — the real resolution is recorded
    fresh by the live path when it comes."""
    before = conn.execute("SELECT * FROM outcomes WHERE journal_ref = ?", (journal_ref,)).fetchone()
    if before is None:
        return {"deleted": False, "before": None, "links": 0}
    try:
        n = conn.execute("DELETE FROM event_outcome_link WHERE outcome_id = ?",
                         (before["id"],)).rowcount
        conn.execute("DELETE FROM outcomes WHERE id = ?", (before["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"deleted": True, "before": dict(before), "links": n}


def desired(entry: dict) -> dict | None:
    """What record_resolved_entry would write for this journal row; None
    when the row is not resolved with an r_multiple (= must not exist)."""
    outcome = entry.get("outcome")
    if not outcome or outcome.get("r_multiple") is None:
        return None
    r = float(outcome["r_multiple"])
    regime = entry.get("regime") or {}
    return {"date": outcome.get("exit_date") or entry["date"],
            "ticker": entry["ticker"],
            "archetype": ((entry.get("spread") or {}).get("strategy")
                          or brain_map._archetype_for(entry.get("signal", ""))),
            "r_multiple": r,
            "result": "win" if r > 0 else ("loss" if r < 0 else "scratch"),
            "regime_trend": regime.get("trend"),
            "regime_vix": regime.get("vix_band")}


def plan(conn, entries: list, refs=REFS) -> list:
    rows = {journal.row_key(e): e for e in entries}
    out = []
    for ref in refs:
        cur = conn.execute("SELECT * FROM outcomes WHERE journal_ref = ?", (ref,)).fetchone()
        cur = dict(cur) if cur is not None else None
        entry = rows.get(ref)
        if entry is None:
            out.append({"ref": ref, "action": "refuse", "why": "no journal row", "current": cur})
            continue
        want = desired(entry)
        if want is None:
            out.append({"ref": ref, "action": "delete" if cur else "none",
                        "why": "journal row is OPEN — no outcome may exist yet", "current": cur})
            continue
        if cur is None:
            out.append({"ref": ref, "action": "none", "why": "no outcomes row (the live path "
                        "records it)", "current": None, "want": want})
            continue
        same = (cur["date"] == want["date"] and cur["result"] == want["result"]
                and cur["archetype"] == want["archetype"]
                and cur["r_multiple"] is not None
                and abs(float(cur["r_multiple"]) - want["r_multiple"]) < 1e-9)
        out.append({"ref": ref, "action": "none" if same else "replace",
                    "why": "already matches the journal" if same else
                           f"journal {entry['outcome'].get('resolution')} "
                           f"pnl Rs.{entry['outcome'].get('pnl_rs')}",
                    "current": cur, "want": want})
    return out


def audit_others(conn, entries: list, skip=REFS) -> list:
    """Every OTHER outcomes row whose result disagrees with its journal row."""
    rows = {journal.row_key(e): e for e in entries}
    bad = []
    for r in conn.execute("SELECT journal_ref, result, r_multiple, date FROM outcomes"):
        ref = r["journal_ref"]
        if ref in skip:
            continue
        e = rows.get(ref)
        if e is None:
            continue                      # composite-key / pre-journal rows: not judged here
        want = desired(e)
        if want is None:
            bad.append({"ref": ref, "brain_map": r["result"], "journal": "OPEN"})
        elif want["result"] != r["result"]:
            bad.append({"ref": ref, "brain_map": r["result"], "journal": want["result"]})
    return bad


def backup(db_path: Path, stamp: str) -> Path:
    dest = db_path.with_name(f"{db_path.name}.bak-d5-{stamp}")
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
        n = chk.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        raise SystemExit(f"backup {dest} failed integrity_check: {ok}")
    print(f"Backup: {dest} (integrity ok, {n} outcomes rows)")
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--yes", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--db", default=None, help="brain_map.db path (default: the real one)")
    args = ap.parse_args(argv)
    db_path = Path(args.db) if args.db else Path(brain_map.DEFAULT_DB_PATH)
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")

    entries = journal.read_all()
    conn = brain_map.connect(db_path)
    try:
        steps = plan(conn, entries)
        for s in steps:
            cur = s.get("current") or {}
            print(f"  {s['ref']}: {s['action'].upper():8} — {s['why']}"
                  + (f" | now {cur.get('result')} r={cur.get('r_multiple')} {cur.get('date')}"
                     if cur else "")
                  + (f" -> {s['want']['result']} r={s['want']['r_multiple']} {s['want']['date']}"
                     if s.get("want") and s["action"] == "replace" else ""))
        others = audit_others(conn, entries)
        print(f"Other outcomes rows disagreeing with the journal (reported, NOT touched): "
              f"{len(others)}")
        for o in others:
            print(f"  {o['ref']}: brain map {o['brain_map']} vs journal {o['journal']}")
        todo = [s for s in steps if s["action"] in ("replace", "delete")]
        if any(s["action"] == "refuse" for s in steps):
            print("REFUSED: a ref has no journal row — nothing written.")
            return 2
        if not todo:
            print("Nothing to repair.")
            return 0
        if not args.yes:
            print(f"DRY RUN — {len(todo)} row(s) would change. Re-run with --yes.")
            return 0
    finally:
        conn.close()

    backup(db_path, stamp)
    archive = db_path.parent / ARCHIVE_NAME
    conn = brain_map.connect(db_path)
    try:
        for s in plan(conn, journal.read_all()):
            if s["action"] not in ("replace", "delete"):
                continue
            links = [dict(r) for r in conn.execute(
                "SELECT * FROM event_outcome_link WHERE outcome_id = ?", (s["current"]["id"],))]
            with open(archive, "a") as f:
                f.write(json.dumps({"repaired_at": datetime.now(IST).isoformat(timespec="seconds"),
                                    "decision": 121, "action": s["action"], "ref": s["ref"],
                                    "row_before": s["current"], "links_before": links,
                                    "why": s["why"]},
                                   sort_keys=True, default=str) + "\n")
            if s["action"] == "replace":
                replace_outcome(conn, s["ref"], s["want"])
            else:
                delete_outcome(conn, s["ref"])
            print(f"  {s['ref']}: {s['action']}d")
        left = [s for s in plan(conn, journal.read_all()) if s["action"] in ("replace", "delete")]
    finally:
        conn.close()
    print(f"Archive: {archive}")
    print("VERIFIED: all five match the journal." if not left
          else f"NOT CLEAN: {len(left)} still differ — {[s['ref'] for s in left]}")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())

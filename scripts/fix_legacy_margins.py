# MANUAL OFFLINE TOOL — one-time repair of pre-#145 debit-spread margin locks (2026-11-06). Never on cron.
"""Shrink OPEN margin locks that still carry the pre-#145 debit-spread margin.

Before b7f923b (2026-10-16) a debit pair blocked (width + debit) x lot; #145 made it
the debit alone. Locks taken before then were never re-sized. A lock is legacy iff its
journal margin matches the pre-#145 formula on the recorded legs and today's
`calculate_span_margin` is materially lower. Each one is scaled by
(recorded - (legacy - current)) / recorded, which keeps its lots and entry VIX stress.
Covers margin_locks (PAPER_10L), paper_margin_locks and portfolio_margin_locks.

    python3 scripts/fix_legacy_margins.py              # dry run
    python3 scripts/fix_legacy_margins.py --apply      # backup, repair, log
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.portfolio import NAKED_SHORT_MARGIN_PCT, VIX_STRESS_BANDS, calculate_span_margin  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
EVENT = "legacy_margin_repair"
MATCH_TOL = 0.005        # recorded margin within 0.5% of the pre-#145 formula
MIN_SHRINK = 0.10        # today's formula must be at least 10% lower
STRESS_FACTORS = sorted({1.0} | {f for _, f in VIX_STRESS_BANDS})


def pre_145_margin(legs: list, lot_size: int) -> float:
    """The pre-#145 formula: every hedged pair blocked (width - net credit) x lot."""
    lot = int(lot_size)
    shorts = [l for l in legs if str(l["side"]).upper() == "SELL"]
    longs = [l for l in legs if str(l["side"]).upper() == "BUY"]
    total = 0.0
    for s in shorts:
        same = [l for l in longs if l["option_type"] == s["option_type"]]
        if same:
            hedge = min(same, key=lambda l: abs(l["strike"] - s["strike"]))
            longs.remove(hedge)
            width = abs(hedge["strike"] - s["strike"])
            total += max(0.0, (width - (s["premium"] - hedge["premium"])) * lot)
        else:
            total += s["premium"] * lot + NAKED_SHORT_MARGIN_PCT * s["strike"] * lot
    return round(total, 2)


def corrected_ratio(entry: dict):
    """New/old lock ratio for a legacy entry, else None."""
    spread = (entry or {}).get("spread") or {}
    recorded = float((spread.get("margin") or {}).get("total_margin") or 0)
    if not recorded or not spread.get("legs") or not spread.get("lot_size"):
        return None
    try:
        legacy = pre_145_margin(spread["legs"], spread["lot_size"])
        current = calculate_span_margin(spread["legs"], spread["lot_size"])["total_margin"]
    except (KeyError, TypeError, ValueError):
        return None
    if abs(recorded - legacy) > max(1.0, MATCH_TOL * recorded):
        return None
    if current > legacy * (1 - MIN_SHRINK):
        return None
    corrected = recorded - (legacy - current)
    if corrected <= 0:
        return None
    return corrected / recorded


def _open_locks(conn) -> list:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    rows = []
    if "margin_locks" in tables:
        rows += [("margin_locks", "PAPER_10L", r, m, None) for r, m in conn.execute(
            "SELECT journal_ref, margin_rs FROM margin_locks WHERE released_at IS NULL")]
    if "paper_margin_locks" in tables:
        rows += [("paper_margin_locks", a, r, m, n) for a, r, m, n in conn.execute(
            "SELECT account_id, journal_ref, margin_rs, lots FROM paper_margin_locks WHERE released_at IS NULL")]
    if "portfolio_margin_locks" in tables:
        rows += [("portfolio_margin_locks", p, r, m, n) for p, r, m, n in conn.execute(
            "SELECT portfolio_id, journal_ref, margin_rs, lots FROM portfolio_margin_locks "
            "WHERE released_at IS NULL")]
    return rows


def still_legacy(margin: float, recorded: float, lots: int) -> bool:
    """The lock still equals recorded x lots x an entry stress factor (a repaired one never does)."""
    return any(abs(float(margin) - recorded * int(lots or 1) * f) <= max(1.0, 0.001 * float(margin))
               for f in STRESS_FACTORS)


def plan(conn, entries: list, skipped: list = None) -> list:
    by_ref = {e.get("short_id"): e for e in entries if e.get("short_id")}
    out = []
    for table, owner, ref, margin, lots in _open_locks(conn):
        entry = by_ref.get(ref)
        ratio = corrected_ratio(entry)
        if ratio is None:
            continue
        spread = entry["spread"]
        recorded = float(spread["margin"]["total_margin"])
        if not still_legacy(margin, recorded, lots if lots is not None else spread.get("lots", 1)):
            if skipped is not None:
                skipped.append({"table": table, "owner": owner, "ref": ref, "lock_rs": round(float(margin), 2),
                                "why": "lock is not the legacy amount (already repaired or re-sized)"})
            continue
        new = round(float(margin) * ratio, 2)
        out.append({"table": table, "owner": owner, "ref": ref, "old_rs": round(float(margin), 2),
                    "new_rs": new, "freed_rs": round(float(margin) - new, 2)})
    return out


_UPDATE = {
    "margin_locks": "UPDATE margin_locks SET margin_rs = ? WHERE journal_ref = ? AND released_at IS NULL "
                    "AND abs(margin_rs - ?) < 0.005 AND ? = 'PAPER_10L'",
    "paper_margin_locks": "UPDATE paper_margin_locks SET margin_rs = ? WHERE journal_ref = ? AND released_at IS NULL "
                          "AND abs(margin_rs - ?) < 0.005 AND account_id = ?",
    "portfolio_margin_locks": "UPDATE portfolio_margin_locks SET margin_rs = ? WHERE journal_ref = ? AND "
                              "released_at IS NULL AND abs(margin_rs - ?) < 0.005 AND portfolio_id = ?",
}


def backup(conn, dest: Path) -> None:
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close()
    if ok != "ok":
        raise RuntimeError(f"backup integrity check failed: {ok}")


def apply(conn, rows: list, archive: Path, now: str) -> int:
    """All rows in one transaction; any row that moved since the plan aborts the lot."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for p in rows:
            cur = conn.execute(_UPDATE[p["table"]], (p["new_rs"], p["ref"], p["old_rs"], p["owner"]))
            if cur.rowcount != 1:
                raise RuntimeError(f"{p['table']} {p['owner']} {p['ref']}: lock changed since the plan")
        accounts = {}
        for p in rows:
            if p["table"] != "portfolio_margin_locks":
                accounts.setdefault(p["owner"], []).append(p)
        for acct, ps in accounts.items():
            detail = json.dumps({"refs": [p["ref"] for p in ps], "freed_rs": round(sum(p["freed_rs"] for p in ps), 2),
                                 "rows": ps})
            if acct == "PAPER_10L":
                conn.execute("INSERT INTO account_events (ts, event_type, detail) VALUES (?, ?, ?)",
                             (now, EVENT, detail))
            else:
                conn.execute("INSERT INTO paper_account_events (account_id, ts, event_type, journal_ref, detail) "
                             "VALUES (?, ?, ?, NULL, ?)", (acct, now, EVENT, detail))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    with archive.open("a") as fh:
        for p in rows:
            fh.write(json.dumps(dict(p, ts=now)) + "\n")
    return len(rows)


def _read_journal(path: Path) -> list:
    out = []
    with path.open() as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "brain_map.db"))
    ap.add_argument("--journal", default=str(ROOT / "data" / "journal.jsonl"))
    ap.add_argument("--archive", default=None, help="default: legacy_margin_repairs.jsonl beside the db")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    db = Path(a.db)
    conn = sqlite3.connect(str(db), timeout=30, isolation_level=None)
    try:
        skipped = []
        rows = plan(conn, _read_journal(Path(a.journal)), skipped)
        for p in rows:
            print(json.dumps(p))
        for p in skipped:
            print("SKIP " + json.dumps(p))
        freed_10l = round(sum(p["freed_rs"] for p in rows if p["table"] == "margin_locks"), 2)
        print(f"{len(rows)} legacy lock row(s); PAPER_10L frees Rs.{freed_10l:,.2f}"
              + ("" if a.apply else " (dry run; add --apply)"))
        if not a.apply or not rows:
            return 0
        now = datetime.now(IST).replace(tzinfo=None, microsecond=0).isoformat()
        dest = db.with_name(f"{db.name}.bak-legacy-margins-{now.replace(':', '').replace('-', '')}")
        backup(conn, dest)
        print(f"backup {dest} (integrity ok)")
        n = apply(conn, rows, Path(a.archive) if a.archive else db.with_name("legacy_margin_repairs.jsonl"), now)
        print(f"APPLIED {n} row(s)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

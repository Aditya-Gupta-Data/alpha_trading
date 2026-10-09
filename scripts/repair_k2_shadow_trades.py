# MANUAL OFFLINE TOOL — audit Chunk 6 K2 (2026-10-09). Never on cron.
"""Re-align resolved shadow_trades rows with their host's REPAIRED outcome (#121 never
touched this table). Dry-run by default; --apply mutates and logs one account_events row.

    python3 scripts/repair_k2_shadow_trades.py            # report only
    python3 scripts/repair_k2_shadow_trades.py --apply    # fix + log
"""
import argparse, json, sqlite3, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

Q = ("SELECT s.journal_ref, s.host_ref, s.result, s.r_multiple, s.resolution_date, "
     "o.result AS o_result, o.r_multiple AS o_r, o.date AS o_date "
     "FROM shadow_trades s LEFT JOIN outcomes o ON o.journal_ref = s.host_ref "
     "WHERE s.resolved = 1 AND s.host_ref IS NOT NULL AND "
     "(o.journal_ref IS NULL OR s.result != o.result OR s.r_multiple != o.r_multiple)")


def plan(conn) -> list:
    conn.row_factory = sqlite3.Row
    out = []
    for r in conn.execute(Q).fetchall():
        if r["o_result"] is None:
            out.append({"ref": r["journal_ref"], "host": r["host_ref"], "action": "unresolve",
                        "was": (r["result"], r["r_multiple"])})
        else:
            out.append({"ref": r["journal_ref"], "host": r["host_ref"], "action": "re-resolve",
                        "was": (r["result"], r["r_multiple"]), "now": (r["o_result"], r["o_r"], r["o_date"])})
    return out


def apply(conn, rows: list) -> int:
    n = 0
    for p in rows:
        if p["action"] == "unresolve":
            conn.execute("UPDATE shadow_trades SET resolved = 0, result = NULL, r_multiple = NULL, "
                         "resolution_date = NULL WHERE journal_ref = ?", (p["ref"],))
        else:
            conn.execute("UPDATE shadow_trades SET result = ?, r_multiple = ?, resolution_date = ? "
                         "WHERE journal_ref = ?", (p["now"][0], p["now"][1], p["now"][2], p["ref"]))
        n += 1
    conn.execute("INSERT INTO account_events (ts, event_type, detail) VALUES (?, 'k2_shadow_repair', ?)",
                 (datetime.now().replace(microsecond=0).isoformat(), json.dumps({"rows": n, "refs": [p["ref"] for p in rows]})))
    conn.commit()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "brain_map.db"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(a.db)
    rows = plan(conn)
    for p in rows:
        print(json.dumps(p))
    print(f"{len(rows)} shadow row(s) disagree with their host outcome" + ("" if a.apply else " (dry-run; add --apply)"))
    if a.apply and rows:
        print(f"applied {apply(conn, rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

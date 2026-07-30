#!/usr/bin/env python3
# MANUAL OFFLINE TOOL — not on any cron/systemd path; keep out of dead-code
# sweeps (same marker convention as src/tuner.py, CLAUDE.md Rule 5).
"""One-shot repair: restore the `concentrates_in` affinity edges that the
first edge-decay sweep expired on 2026-07-30, and put them on the slower
structural clock (λ=0.002, ~347-day half-life).

WHY A SETTING-λ-AND-CLEARING-invalid_at SCRIPT IS NOT ENOUGH
------------------------------------------------------------
The obvious repair — `UPDATE graph_edges SET decay_lambda=0.002,
invalid_at=NULL WHERE relation='concentrates_in'` — undoes itself within
24 hours and is a no-op in the meantime. Two reasons, both verified
against the live DB before this script was written:

  1. The sweep already REWROTE `confidence_score` down to the decayed
     value (observed as low as 1.19e-98). Clearing `invalid_at` alone
     produces an ACTIVE edge with ~zero confidence — worse than an
     expired one, because consumers now see it and weight it at nothing.
     The next sweep then re-stamps `invalid_at` (w < 0.1 still), so the
     resurrection silently reverts tonight's work tomorrow night.
  2. The sweep also OVERWROTE `valid_from` with the sweep timestamp, so
     the decay anchor — the "age from the deal date, not from today"
     seam in `graph_engine.add_edge` — was consumed. Left as-is, every
     resurrected edge would read as born-today: a 12-year-dormant
     affinity would look maximally fresh, which is the exact failure the
     backfill seam exists to prevent.

So this script REBUILDS each edge from its source of truth instead of
patching the damaged row: concentration is recomputed by
`entity_affinity._client_concentration` (the `entity_affinity`
accumulation table, untouched by decay — 19,819 rows), and the anchor is
restored from that table's own `last_seen`. The write goes through
`graph_engine.add_edge`, which already resets `valid_from`, clears
`invalid_at`, updates confidence and honours an explicit λ — no raw SQL
surgery, no new machinery (owner constraint: no new engines).

HONEST CONSEQUENCE, STATED UP FRONT: this does NOT resurrect everything.
An edge whose last deal is older than roughly 3 years is still below the
0.1 threshold at λ=0.002 and will expire again on the next sweep — as it
should; that affinity really is stale. Only the PREMATURELY expired ones
come back. The dry run prints both counts so the split is visible before
anything is written.

An entity that no longer clears MIN_GROUP_DEALS / MIN_CONCENTRATION is
NOT resurrected either: the edge asserts a fact, and if the fact is no
longer true the row should stay dead rather than be revived by fiat.

Usage (dry run is the DEFAULT — nothing is written without --apply):

    python3 scripts/resurrect_affinity.py
    python3 scripts/resurrect_affinity.py --apply
"""

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, decay_engine, graph_engine          # noqa: E402
from src.knowledge_graph import entity_affinity as ea          # noqa: E402

RELATION = "concentrates_in"


def _survives(confidence: float, age_days: float, lam: float) -> bool:
    """Would this edge clear the decay threshold on the next sweep?"""
    return confidence * math.exp(-lam * max(0.0, age_days)) >= decay_engine.DECAY_THRESHOLD


def plan(conn, today_iso: str) -> dict:
    """Read-only. Returns the full repair plan, decided per edge."""
    from datetime import date
    today = date.fromisoformat(today_iso)
    rows = conn.execute(
        "SELECT source_node, target_node, confidence_score, invalid_at "
        "FROM graph_edges WHERE relation = ?", (RELATION,)).fetchall()

    out = {"total": len(rows), "restore": [], "skip_not_a_fact": [],
           "skip_too_old": []}
    for r in rows:
        client, grp = r["source_node"], r["target_node"]
        top_group, concentration, group_deals = ea._client_concentration(conn, client)
        if (top_group != grp or group_deals < ea.MIN_GROUP_DEALS
                or concentration < ea.MIN_CONCENTRATION):
            out["skip_not_a_fact"].append((client, grp, concentration))
            continue
        seen = conn.execute(
            "SELECT last_seen FROM entity_affinity WHERE client = ? AND grp = ?",
            (client, grp)).fetchone()
        last_seen = seen["last_seen"] if seen else None
        if not last_seen:
            out["skip_not_a_fact"].append((client, grp, concentration))
            continue
        try:
            age = (today - date.fromisoformat(str(last_seen)[:10])).days
        except (ValueError, TypeError):
            out["skip_not_a_fact"].append((client, grp, concentration))
            continue
        item = {"client": client, "grp": grp, "confidence": concentration,
                "deals": group_deals, "last_seen": str(last_seen)[:10],
                "age_days": age,
                "was_expired": r["invalid_at"] is not None}
        if _survives(concentration, age, ea.CONCENTRATION_DECAY_LAMBDA):
            out["restore"].append(item)
        else:
            out["skip_too_old"].append(item)
    return out


def apply(conn, items: list) -> int:
    """Rewrite each edge through the existing add_edge writer seam."""
    for it in items:
        graph_engine.add_edge(
            conn, it["client"], RELATION, it["grp"],
            confidence_score=it["confidence"],
            context=f"{it['deals']} deals; {int(it['confidence']*100)}% concentration",
            valid_from=it["last_seen"],
            decay_lambda=ea.CONCENTRATION_DECAY_LAMBDA,
            source="affinity_projected")
    return len(items)


def main() -> int:
    from datetime import date
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--db", default=None,
                    help="path to a brain_map.db (default: the real one). "
                         "Lets the dry run be validated against a COPY of "
                         "live data before anything is deployed.")
    args = ap.parse_args()

    conn = brain_map.connect(args.db)
    try:
        p = plan(conn, args.today)
        expired_before = sum(1 for i in p["restore"] if i["was_expired"])
        print(f"{RELATION} edges in graph: {p['total']}")
        print(f"  RESTORE (clears the threshold at "
              f"lambda={ea.CONCENTRATION_DECAY_LAMBDA}): {len(p['restore'])}"
              f"  [of which currently expired: {expired_before}]")
        print(f"  leave dead — last deal too old (>~3y): {len(p['skip_too_old'])}")
        print(f"  leave dead — no longer meets the concentration bar: "
              f"{len(p['skip_not_a_fact'])}")
        for i in p["restore"][:5]:
            print(f"    e.g. {i['client']} -> {i['grp']}  conf {i['confidence']} "
                  f"last_seen {i['last_seen']} ({i['age_days']}d)")
        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            return 0
        n = apply(conn, p["restore"])
        active = conn.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE relation = ? "
            "AND invalid_at IS NULL", (RELATION,)).fetchone()[0]
        print(f"\nRestored {n} edge(s). {RELATION} now active: {active}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

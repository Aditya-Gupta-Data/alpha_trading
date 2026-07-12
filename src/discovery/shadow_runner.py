"""
src/discovery/shadow_runner.py — live shadow-firing of registered patterns
==========================================================================

Owner concerns #1/#2 (2026-07-11): "once a pattern is seen we need paper
trades to be sure of it" + "I'm skeptical about what it finds / how it
triggers trades." This module is both answers:

  FIRE    At every REAL proposal entry, the entry's full tag picture (its
          own signal/pattern tags + today's ctx: market-frame tags + the
          lagged antecedent tags) is matched against every registered
          pattern; each match records a SHADOW fire in brain_map's
          shadow_trades table, tied to the host trade by `host_ref`.
  RESOLVE The nightly sweep (Sleep-Phase Task I) copies each host trade's
          resolved outcome onto its shadow fires — the same result the
          tracker computed, no parallel price arithmetic. Those
          resolutions are the pattern's out-of-sample evidence stream
          (trial.evaluate_trial / monitor.check_pattern consume it).

WHAT THIS CAN NEVER DO (the skeptic's guarantee, testable): it never
creates a journal entry, never proposes, never approves, never touches
portfolio state. A shadow fire is a bookkeeping row that says "the
pattern CLAIMED this entry" — the pattern's score comes from whether the
trades it claimed actually won. The live pipeline is byte-identical with
or without this module (fail-open at every seam).

Matching semantics per kind:
  cooccurrence  definition tags ⊆ (entry tags ∪ today's ctx tags)
  sequence      definition tags ⊆ lagged antecedent tags for the entry day
Statuses matched: CANDIDATE / TRIAL / INSUFFICIENT_N (evidence gathering)
plus VALIDATED / LIVE_ADVISORY (the drift monitor's live stream). DEAD and
QUARANTINED patterns never fire.

Observability: `python3 -m src.discovery.inspect <auto-tag|id-prefix>`
shows any pattern's definition, audit trail, and every shadow fire+result.
"""

import json
import re

from src.validation import registry as rg
from src.validation import trial

FIREABLE_STATUSES = ("CANDIDATE", "TRIAL", "INSUFFICIENT_N",
                     "VALIDATED", "LIVE_ADVISORY")


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


def entry_own_tags(entry: dict) -> set:
    """The tags a journal entry itself carries, in the brain_map tag
    vocabulary (signal archetype + normalized pattern tags)."""
    tags = set()
    signal = entry.get("signal") or ""
    if signal:
        from src.brain_map import _archetype_for
        arch = ((entry.get("spread") or {}).get("strategy")
                or _archetype_for(signal))
        tags.add(arch if arch != "other" else _normalize(signal))
    for raw in entry.get("pattern_tags") or []:
        tags.add(_normalize(raw))
    return {t for t in tags if t}


def day_tag_picture(conn, day: str) -> dict:
    """{ctx, lagged}: today's market-frame ctx tags and the lagged
    antecedent tags as-of `day`, from daily_context. Empty sets when the
    frames aren't there — honest, never guessed."""
    from src import daily_context as dc
    from src.discovery import cooccurrence_miner as cm
    from src.discovery import sequence_miner as sm
    dc.ensure_schema(conn)
    frames = {r["date"]: r for r in conn.execute("SELECT * FROM daily_context")}
    ctx_dates = sorted(frames)
    today_frame = frames.get(day)
    # The as-of frame for ctx tags: today's own frame only (a stale frame
    # is not "today's market"); lagged tags use the whole series.
    return {"ctx": cm.context_tags(today_frame) if today_frame else set(),
            "lagged": sm.lagged_antecedent_tags(day, ctx_dates, frames)}


def match_patterns(conn, ctx_union: set, lagged: set) -> list:
    """Every fireable registered pattern whose frozen definition matches
    the tag picture. Returns [{pattern_id, kind, tags}]."""
    rg.ensure_schema(conn)
    rows = conn.execute(
        "SELECT pattern_id, kind, definition FROM candidate_patterns "
        "WHERE kind IN ('cooccurrence', 'sequence') AND status IN "
        f"({', '.join('?' * len(FIREABLE_STATUSES))})",
        FIREABLE_STATUSES).fetchall()
    matches = []
    for row in rows:
        try:
            tags = set((json.loads(row["definition"]) or {}).get("tags") or [])
        except (ValueError, TypeError):
            continue
        if not tags:
            continue
        haystack = lagged if row["kind"] == "sequence" else ctx_union
        if tags <= haystack:
            matches.append({"pattern_id": row["pattern_id"],
                            "kind": row["kind"], "tags": sorted(tags)})
    return matches


def on_entry(conn, entry: dict, day: str = None) -> list:
    """THE fire hook, called at proposal time (via the evidence stamp).
    Matches the entry's full tag picture and records one shadow fire per
    matching pattern, host-linked. Idempotent per (pattern, day, ticker).
    Fail-open: any failure returns [] and the proposal is untouched."""
    try:
        day = day or entry.get("date")
        ticker = entry.get("ticker")
        if not day or not ticker:
            return []
        picture = day_tag_picture(conn, day)
        ctx_union = entry_own_tags(entry) | picture["ctx"]
        matches = match_patterns(conn, ctx_union, picture["lagged"])
        if not matches:
            return []
        from src.brain_map import journal_ref_for
        host = journal_ref_for(entry)
        fired = []
        for m in matches:
            res = trial.record_shadow_fire(conn, m["pattern_id"], day, ticker)
            conn.execute(
                "UPDATE shadow_trades SET host_ref = ? "
                "WHERE journal_ref = ? AND host_ref IS NULL",
                (host, res["ref"]))
            fired.append({**m, "shadow_ref": res["ref"], "host_ref": host,
                          "created": res["created"]})
        conn.commit()
        return fired
    except Exception as exc:
        print(f"  (shadow runner: fire failed [{exc}] — proposal unaffected)")
        return []


def resolve_from_outcomes(conn) -> int:
    """Sleep-Phase Task I: every unresolved, host-linked shadow fire whose
    host trade has since resolved inherits the host's outcome (result +
    R + exit date). Returns shadows resolved. Never raises."""
    try:
        trial.ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT s.journal_ref AS ref, o.result, o.r_multiple,
                   o.date AS resolution_date
            FROM shadow_trades s
            JOIN outcomes o ON o.journal_ref = s.host_ref
            WHERE s.resolved = 0 AND s.host_ref IS NOT NULL
            """).fetchall()
        done = 0
        for r in rows:
            if trial.resolve_shadow(conn, r["ref"], r["result"],
                                    r["r_multiple"], r["resolution_date"]):
                done += 1
        return done
    except Exception as exc:
        print(f"  (shadow runner: resolve sweep failed [{exc}])")
        return 0


def run_sweep(db_path=None) -> dict:
    """Task I entry: resolve host-linked shadows. VM-safe, no LLM."""
    from src import brain_map
    conn = brain_map.connect(db_path)
    try:
        resolved = resolve_from_outcomes(conn)
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM shadow_trades WHERE resolved = 0"
        ).fetchone()["n"]
    finally:
        conn.close()
    print(f"  (shadow runner: {resolved} shadow(s) resolved, "
          f"{pending} still open)")
    return {"resolved": resolved, "pending": pending}

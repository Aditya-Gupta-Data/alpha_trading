"""
Alpha Trading — Phase 6E: Temporal Financial Signal Decay
=========================================================

An autonomous background sweep that applies exponential decay to the
knowledge graph's edge weights (graph_edges.confidence_score) so stale
causal signals fade out of the reasoning layer without being deleted.

The decay formula is the same discrete exponential used by the Sleep Phase's
semantic-node decay (src/sleep_phase.py Task C):

    w(t) = w₀ · exp(−λ · t)

where:
  w₀ = confidence_score currently stored in the row (from last write/sweep)
  λ  = decay_lambda (per-edge rate, days⁻¹; default 0.05)
  t  = days elapsed since valid_from (the last write or sweep timestamp)

After applying the formula the row is updated:
  confidence_score ← new decayed weight
  valid_from       ← now  (resets delta clock for next sweep)

When a new weight falls below DECAY_THRESHOLD (0.1) the edge is NOT deleted
— history is kept — but invalid_at is stamped so GraphEngine.load() ignores
it. If add_edge() is later called for the same triple (the pattern
re-appears), invalid_at is cleared and valid_from resets (decision #37).

SCHEMA — three additive columns on graph_edges (migration idempotent):
  valid_from   TEXT   ISO-8601 UTC datetime; creation/last-sweep timestamp
  invalid_at   TEXT   ISO-8601 UTC datetime; NULL means still active
  decay_lambda REAL   per-edge λ (defaults to DEFAULT_LAMBDA on first sweep)

STRICTLY ADDITIVE: brain_map.py and graph_engine.py are unchanged in their
core logic; this module only writes the three temporal columns. Decision #30
holds: no market data, no trading, no network I/O.

Run manually or via cron (after the Sleep Phase):

    python3 -m src.decay_engine

"""

import math
from datetime import datetime, timezone
from pathlib import Path

from src import brain_map

DECAY_THRESHOLD = 0.1    # below this weight, mark invalid_at
DEFAULT_LAMBDA = 0.05    # matches sleep_phase.DEFAULT_DECAY_LAMBDA


def migrate_schema(conn) -> None:
    """Idempotently add valid_from, invalid_at, decay_lambda to graph_edges.

    Safe to call on a fresh DB (graph_edges table may not exist yet — in
    that case there is nothing to migrate and we return quietly) or on an
    already-migrated one (ALTER TABLE is skipped if columns exist)."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "graph_edges" not in tables:
        return
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(graph_edges)")}
    if "valid_from" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN valid_from TEXT")
    if "invalid_at" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN invalid_at TEXT")
    if "decay_lambda" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN decay_lambda REAL")
    conn.commit()


def apply_decay_sweep(conn, default_lambda: float = DEFAULT_LAMBDA) -> dict:
    """Sweep all active graph_edges rows and apply one step of exponential
    decay. Call this once per day (e.g. after the Sleep Phase cron job).

    Per-edge logic:
    - If valid_from or confidence_score is NULL: stamp valid_from = now and
      set decay_lambda = default_lambda; no weight change on first touch.
    - Otherwise: t = days since valid_from; new_weight = w₀ · exp(−λ·t).
      Update confidence_score, reset valid_from = now.
    - If new_weight < DECAY_THRESHOLD: also set invalid_at = now.

    Returns {"swept": int, "decayed": int, "expired": int}:
      swept   — edges with decay actually applied (t > 0, confidence known)
      decayed — same as swept (every swept edge is decayed by definition)
      expired — edges that crossed the 0.1 threshold this run
    """
    migrate_schema(conn)
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    rows = conn.execute(
        "SELECT rowid, confidence_score, valid_from, decay_lambda "
        "FROM graph_edges WHERE invalid_at IS NULL"
    ).fetchall()

    swept = 0
    expired = 0

    for row in rows:
        rowid = row["rowid"]
        w0 = row["confidence_score"]
        lambda_ = (row["decay_lambda"] if row["decay_lambda"] is not None
                   else default_lambda)

        if w0 is None or row["valid_from"] is None:
            conn.execute(
                "UPDATE graph_edges SET valid_from = ?, decay_lambda = ? "
                "WHERE rowid = ?",
                (now_str, lambda_, rowid),
            )
            continue

        try:
            last_dt = datetime.fromisoformat(row["valid_from"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            conn.execute(
                "UPDATE graph_edges SET valid_from = ?, decay_lambda = ? "
                "WHERE rowid = ?",
                (now_str, lambda_, rowid),
            )
            continue

        t_days = (now - last_dt).total_seconds() / 86400.0
        if t_days < 1e-9:
            continue

        new_weight = w0 * math.exp(-lambda_ * t_days)
        swept += 1

        if new_weight < DECAY_THRESHOLD:
            conn.execute(
                "UPDATE graph_edges "
                "SET confidence_score = ?, valid_from = ?, invalid_at = ?, decay_lambda = ? "
                "WHERE rowid = ?",
                (new_weight, now_str, now_str, lambda_, rowid),
            )
            expired += 1
        else:
            conn.execute(
                "UPDATE graph_edges "
                "SET confidence_score = ?, valid_from = ?, decay_lambda = ? "
                "WHERE rowid = ?",
                (new_weight, now_str, lambda_, rowid),
            )

    conn.commit()
    return {"swept": swept, "decayed": swept, "expired": expired}


if __name__ == "__main__":
    db_path = Path(__file__).resolve().parent.parent / "data" / "brain_map.db"
    conn = brain_map.connect(str(db_path))
    try:
        result = apply_decay_sweep(conn)
        print(
            f"Decay sweep complete: {result['swept']} edges decayed, "
            f"{result['expired']} expired (below {DECAY_THRESHOLD})."
        )
    finally:
        conn.close()

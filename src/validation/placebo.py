"""
src/validation/placebo.py — seeded nulls that must NEVER validate
=================================================================

Phase 4 of docs/HOLY_GRAIL_PLAN.md (§7.7). Benjamini-Hochberg and the
stability battery have assumptions (independence, stationarity) this
data violates in unknown degree. The only way to know the harness's REAL
false-discovery rate is to measure it: salt every mining era with
information-free hypotheses that flow through the ENTIRE harness
indistinguishably, and watch how often they wrongly validate.

Doctrine (the panel's arithmetic fixes):
  * Placebos run in a PARALLEL corrected batch — they never inflate the
    real hypotheses' BH denominator (which would silently tighten
    thresholds on real patterns).
  * The response to a placebo validating is COMPARE-TO-BUDGET, never
    void-the-batch: BH at q is DESIGNED to pass ~q false discoveries, so
    an occasional placebo validation is the harness performing to spec.
    Only a placebo pass-rate whose Wilson LOWER bound exceeds the designed
    q is real evidence the gates are too loose — surfaced for human
    review, never auto-acted.
  * Placebos are STRUCTURALLY hidden from miners: their pattern_ids carry
    the placebo: ref-style prefix and stat_gates excludes them, and a
    guard test asserts the mining path never reads the placebo flag.

The realized placebo pass-rate is a first-class dashboard number (with its
own CI and an honest "insufficient placebo n" state).
"""

import hashlib
import json
from datetime import datetime, timezone

from src.validation import registry as rg
from src.validation import stat_gates as sg

PLACEBO_TAG_PREFIX = "placebo:"


def ensure_schema(conn) -> None:
    """Additive placebo_ledger: which registered patterns are placebos and
    how each resolved. Kept separate so the real registry stays clean."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS placebo_ledger (
            pattern_id TEXT PRIMARY KEY,
            batch TEXT,
            resolved TEXT,
            resolved_at TEXT
        )
    """)
    conn.commit()


def make_placebo_defn(seed_tag_pool: list, rng) -> dict:
    """An information-free but real-LOOKING predicate: a random pair of
    tags drawn from the live vocabulary + a random regime band. It matches
    real data by luck alone — its expected edge is zero."""
    pool = list(seed_tag_pool) or ["noise_a", "noise_b", "noise_c"]
    a = pool[rng.randrange(len(pool))]
    b = pool[rng.randrange(len(pool))]
    band = ("low", "mid", "high")[rng.randrange(3)]
    salt = rng.randrange(1_000_000)
    return {"kind": "itemset", "tags": sorted({a, b}),
            "regime_vix": band, "_placebo_salt": salt}


def seed_batch(conn, batch: str, seed_tag_pool: list, rng,
               count: int = 10) -> list:
    """Register `count` placebos for one mining era. Their pattern_ids go
    in placebo_ledger; the registry sees them as ordinary CANDIDATEs (the
    harness MUST be blind to their nature). Returns the placebo ids."""
    ensure_schema(conn)
    ids = []
    for _ in range(count):
        defn = make_placebo_defn(seed_tag_pool, rng)
        reg = rg.register(conn, "itemset", defn,
                          description="[seeded null]", mining_run=batch)
        pid = reg["pattern_id"]
        conn.execute("INSERT OR IGNORE INTO placebo_ledger (pattern_id, "
                     "batch) VALUES (?, ?)", (pid, batch))
        ids.append(pid)
    conn.commit()
    return ids


def is_placebo(conn, pattern_id: str) -> bool:
    ensure_schema(conn)
    return conn.execute("SELECT 1 FROM placebo_ledger WHERE pattern_id = ?",
                        (pattern_id,)).fetchone() is not None


def record_placebo_outcome(conn, pattern_id: str) -> None:
    """Snapshot a placebo's registry status into the ledger (called by the
    auditor AFTER the harness has run — the auditor is the ONLY code that
    reads placebo-ness)."""
    ensure_schema(conn)
    row = rg.get(conn, pattern_id)
    status = (row or {}).get("status", "UNKNOWN")
    resolved = "validated" if status in ("VALIDATED", "LIVE_ADVISORY") else "held"
    conn.execute("UPDATE placebo_ledger SET resolved = ?, resolved_at = ? "
                 "WHERE pattern_id = ?",
                 (resolved, datetime.now(timezone.utc).isoformat(
                     timespec="seconds"), pattern_id))
    conn.commit()


def realized_fdr(conn) -> dict:
    """The measured false-discovery rate: fraction of RESOLVED placebos that
    wrongly reached VALIDATED, with a Wilson lower bound and an honest
    insufficient-n state. `alarm` is True only when the Wilson LOWER bound
    of the placebo pass-rate exceeds the designed FDR q — real evidence the
    gates are too loose (compare-to-budget, never auto-act)."""
    ensure_schema(conn)
    rows = conn.execute("SELECT resolved FROM placebo_ledger "
                        "WHERE resolved IS NOT NULL").fetchall()
    n = len(rows)
    validated = sum(1 for r in rows if r["resolved"] == "validated")
    q = sg.configured_floors()["fdr_q"]
    if n < 20:
        return {"n": n, "validated": validated, "rate": None,
                "wilson_lb": None, "designed_q": q,
                "state": "insufficient placebo n", "alarm": False}
    lb = sg.wilson_lower_bound(validated, n)
    return {"n": n, "validated": validated, "rate": round(validated / n, 4),
            "wilson_lb": round(lb, 4), "designed_q": q,
            "state": "measured", "alarm": lb > q}


def audit_batch(conn, batch: str) -> dict:
    """After a mining era's harness run: record every placebo's outcome and
    return the batch summary. The ONLY place placebo-ness is read — mining/
    trial code stays structurally blind."""
    ensure_schema(conn)
    ids = [r["pattern_id"] for r in conn.execute(
        "SELECT pattern_id FROM placebo_ledger WHERE batch = ?", (batch,))]
    for pid in ids:
        record_placebo_outcome(conn, pid)
    return {"batch": batch, "placebos": len(ids), "fdr": realized_fdr(conn)}

"""
src/validation/registry.py — the pattern registry & lifecycle state machine
===========================================================================

Phase 4 of docs/HOLY_GRAIL_PLAN.md (§7.2). Every hypothesis the discovery
brain ever mints — a tag cluster, a sequence, an affinity rule, a
strategy-selection rule — is REGISTERED here with a frozen definition and
a governed status. Nothing unregistered can ever be surfaced by the new
discovery outputs; nothing transitions except through this module, and
every transition writes an audit row. Without a counted hypothesis
denominator, FDR correction is mathematically impossible — the registry
IS the denominator.

Lifecycle:

    CANDIDATE ──► TRIAL ──► VALIDATED ──► LIVE_ADVISORY
        │            │           │              │
        ▼            ▼           ▼              ▼
    INSUFFICIENT_N / DEAD    QUARANTINED ◄──────┘
                                  │  (re-trial allowed once;
                                  ▼   a SECOND quarantine = DEAD)
                              TRIAL or DEAD

Rules baked in (panel verdicts):
  * Definitions are FROZEN at registration (canonical JSON; the
    pattern_id is its hash) — re-discovering the same predicate is a
    no-op, so dead ends are never re-litigated (the lineage rule, #49).
  * All transitions are soft — rows are never deleted (the
    flag-don't-delete doctrine, #37; DEAD patterns are memory too).
  * INSUFFICIENT_N is distinct from tried-and-failed: "never had enough
    validation data" and "failed the trial" must read differently.
  * Validated patterns mint an `auto:<hash8>` tag; stat_gates excludes
    the auto: namespace from miner inputs (no tautological rediscovery).
  * Only VALIDATED / LIVE_ADVISORY patterns are citable by any NEW
    discovery-brain surface (existing #26/#33 surfaces keep rendering,
    stamped with registry state — never silenced).

Additive table in brain_map.db (#25 discipline); pure Python.
"""

import hashlib
import json
from datetime import datetime, timezone

STATES = ("CANDIDATE", "TRIAL", "VALIDATED", "LIVE_ADVISORY",
          "QUARANTINED", "INSUFFICIENT_N", "DEAD")

# from-status -> allowed to-statuses. DEAD is terminal.
LEGAL_TRANSITIONS = {
    "CANDIDATE": {"TRIAL", "INSUFFICIENT_N", "DEAD"},
    "TRIAL": {"VALIDATED", "CANDIDATE", "INSUFFICIENT_N", "DEAD"},
    "VALIDATED": {"LIVE_ADVISORY", "QUARANTINED", "CANDIDATE"},
    "LIVE_ADVISORY": {"QUARANTINED", "CANDIDATE"},
    "QUARANTINED": {"TRIAL", "DEAD"},
    "INSUFFICIENT_N": {"CANDIDATE", "TRIAL", "DEAD"},
    "DEAD": set(),
}

CITABLE_STATES = ("VALIDATED", "LIVE_ADVISORY")


def ensure_schema(conn) -> None:
    """Additive candidate_patterns + pattern_audit tables. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            definition TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'CANDIDATE',
            mining_run TEXT,
            discovery_window TEXT,
            support_n INTEGER,
            fdr_q REAL,
            insample_stats TEXT,
            oos_stats TEXT,
            discovered_at TEXT NOT NULL,
            promoted_at TEXT,
            retired_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pattern_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_id TEXT NOT NULL,
            at TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT
        )
    """)
    conn.commit()


def canonical_definition(definition: dict) -> str:
    """The frozen form: sorted-key compact JSON. The pattern IS this
    string; any change to it is a NEW hypothesis with a new id."""
    return json.dumps(definition, sort_keys=True, separators=(",", ":"))


def pattern_id_for(definition: dict) -> str:
    return hashlib.sha1(canonical_definition(definition).encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(conn, pattern_id, from_status, to_status, reason) -> None:
    conn.execute(
        "INSERT INTO pattern_audit (pattern_id, at, from_status, to_status, "
        "reason) VALUES (?, ?, ?, ?, ?)",
        (pattern_id, _now(), from_status, to_status, str(reason or "")[:300]))


def register(conn, kind: str, definition: dict, description: str = "",
             mining_run: str = "", discovery_window: str = "",
             support_n: int = None, fdr_q: float = None,
             insample_stats: dict = None) -> dict:
    """Mint (or find) the hypothesis for a frozen predicate. Idempotent on
    the definition hash: re-discovery returns the EXISTING row untouched —
    including a DEAD one, so dead ends stay dead. Returns
    {pattern_id, status, created(bool)}."""
    ensure_schema(conn)
    pid = pattern_id_for(definition)
    row = conn.execute("SELECT status FROM candidate_patterns "
                       "WHERE pattern_id = ?", (pid,)).fetchone()
    if row is not None:
        return {"pattern_id": pid, "status": row["status"], "created": False}
    conn.execute(
        "INSERT INTO candidate_patterns (pattern_id, kind, definition, "
        "description, status, mining_run, discovery_window, support_n, "
        "fdr_q, insample_stats, discovered_at) "
        "VALUES (?, ?, ?, ?, 'CANDIDATE', ?, ?, ?, ?, ?, ?)",
        (pid, kind, canonical_definition(definition), description,
         mining_run, discovery_window, support_n, fdr_q,
         json.dumps(insample_stats) if insample_stats else None, _now()))
    _audit(conn, pid, None, "CANDIDATE",
           f"registered by {mining_run or 'unknown run'}")
    conn.commit()
    return {"pattern_id": pid, "status": "CANDIDATE", "created": True}


def get(conn, pattern_id: str) -> dict | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM candidate_patterns WHERE pattern_id = ?",
                       (pattern_id,)).fetchone()
    return dict(row) if row else None


def transition(conn, pattern_id: str, to_status: str, reason: str) -> dict:
    """The ONLY way a status changes. Enforces the legal map, the
    quarantine-twice-is-DEAD rule, and stamps promoted_at/retired_at.
    Returns {ok, status, reason}."""
    ensure_schema(conn)
    if to_status not in STATES:
        return {"ok": False, "status": None,
                "reason": f"unknown status {to_status}"}
    row = get(conn, pattern_id)
    if row is None:
        return {"ok": False, "status": None, "reason": "unregistered pattern"}
    frm = row["status"]
    if to_status not in LEGAL_TRANSITIONS.get(frm, set()):
        return {"ok": False, "status": frm,
                "reason": f"illegal transition {frm} -> {to_status}"}
    # Quarantine-twice-is-DEAD (lineage rule): a second quarantine within
    # the pattern's lifetime hard-lands it.
    if to_status == "QUARANTINED":
        prior = conn.execute(
            "SELECT COUNT(*) AS n FROM pattern_audit WHERE pattern_id = ? "
            "AND to_status = 'QUARANTINED'", (pattern_id,)).fetchone()["n"]
        if prior >= 1:
            to_status = "DEAD"
            reason = f"second quarantine ({reason}) — pattern is DEAD"
    stamps = {"VALIDATED": ("promoted_at", _now()),
              "DEAD": ("retired_at", _now()),
              "QUARANTINED": ("retired_at", _now())}
    extra_sql, extra_val = "", ()
    if to_status in stamps:
        col, val = stamps[to_status]
        extra_sql, extra_val = f", {col} = ?", (val,)
    conn.execute(
        f"UPDATE candidate_patterns SET status = ?{extra_sql} "
        "WHERE pattern_id = ?",
        (to_status,) + extra_val + (pattern_id,))
    _audit(conn, pattern_id, frm, to_status, reason)
    conn.commit()
    return {"ok": True, "status": to_status, "reason": reason}


def update_oos_stats(conn, pattern_id: str, oos_stats: dict) -> bool:
    """Merge new out-of-sample evidence onto the row (stats accumulate
    monotonically across trial cycles — the panel's re-trial rule)."""
    row = get(conn, pattern_id)
    if row is None:
        return False
    merged = {}
    try:
        merged = json.loads(row["oos_stats"]) if row["oos_stats"] else {}
    except ValueError:
        merged = {}
    merged.update(oos_stats or {})
    conn.execute("UPDATE candidate_patterns SET oos_stats = ? "
                 "WHERE pattern_id = ?", (json.dumps(merged), pattern_id))
    conn.commit()
    return True


def list_by_status(conn, status: str) -> list:
    ensure_schema(conn)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM candidate_patterns WHERE status = ? "
        "ORDER BY discovered_at", (status,))]


def citable(conn, pattern_id: str) -> bool:
    """May a NEW discovery-brain surface cite this pattern? Only after it
    has paid rent out-of-sample."""
    row = get(conn, pattern_id)
    return bool(row and row["status"] in CITABLE_STATES)


def mint_tag(pattern_id: str) -> str:
    """The auto: tag a validated pattern publishes under —
    structurally excluded from miner inputs via stat_gates."""
    return f"auto:{pattern_id[:8]}"


def states_for_tags(conn, tags: list) -> dict:
    """For the existing #26/#33 surfaces (HOLY_GRAIL §7.2: "stamped with
    registry state inline — shipped functionality is not silenced"): given
    the pattern tags active on a proposal/forecast, return {tag: status}
    for every tag some registered pattern's frozen definition names.
    Newest registration wins per tag. A rendering nicety, never
    load-bearing: nothing here gates, and ANY problem (no table yet, a
    malformed definition, a closed conn) degrades to {} rather than
    raising — a forecast must never depend on the registry existing."""
    if not tags:
        return {}
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT definition, status FROM candidate_patterns "
            "ORDER BY id DESC").fetchall()
    except Exception:
        return {}
    wanted, out = set(tags), {}
    for row in rows:
        try:
            definition = json.loads(row["definition"])
        except (ValueError, TypeError):
            continue
        def_tags = list(definition.get("tags") or [])
        if definition.get("tag"):
            def_tags.append(definition["tag"])
        for t in def_tags:
            if t in wanted and t not in out:
                out[t] = row["status"]
    return out


def audit_trail(conn, pattern_id: str) -> list:
    ensure_schema(conn)
    return [dict(r) for r in conn.execute(
        "SELECT at, from_status, to_status, reason FROM pattern_audit "
        "WHERE pattern_id = ? ORDER BY id", (pattern_id,))]

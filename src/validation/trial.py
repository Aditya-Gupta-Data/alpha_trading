"""
src/validation/trial.py — walk-forward trials + shadow tracking
===============================================================

Phase 4 of docs/HOLY_GRAIL_PLAN.md (§7.3-7.4). The gate between a
DISCOVERED pattern and a VALIDATED one: a pattern mined on a discovery
window must pay rent on data it was NOT mined from before it earns a
card. Two evidence streams, both keyed by MARKET date (never run date):

  RETROSPECTIVE — the pattern's outcomes in a DISJOINT later window
                  (mine on the first X%, validate on the last Y%), with an
                  embargo gap so a trade proposed in-window but resolved
                  out-of-window can't leak across the boundary.
  PROSPECTIVE   — SHADOW firings: when a discovered pattern's matcher
                  fires on live data, a `shadow:` row is journaled to
                  brain_map's `shadow_trades` table (NEVER journal.jsonl —
                  a shadow entry there would arm the cooldown and block
                  real proposals) and resolved by the same pure arithmetic
                  the simulator/tracker use.

Promotion (via stat_gates.promotable, the locked policy): combined real+sim
out-of-discovery evidence clears the floor, its Wilson LOWER bound beats
the STRUCTURAL breakeven null, and at least one REAL resolution exists
(sim supports, never solely justifies). The registry (src/validation/
registry.py) records every transition.

Self-poisoning guard: shadow:/sim:/trial: refs are excluded from every
learning corpus by stat_gates.EXCLUDED_REF_PREFIXES — enforced here and
testable. Nothing in this module writes journal.jsonl or portfolio.json.
"""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from src.validation import registry as rg
from src.validation import stat_gates as sg

EMBARGO_DAYS = 5


# ------------------------------------------------------------- schema

def ensure_schema(conn) -> None:
    """Additive shadow_trades table in brain_map.db (#25). Keyed by a
    deterministic shadow: ref so a re-fire of the same pattern on the same
    day/ticker is idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            journal_ref TEXT PRIMARY KEY,
            pattern_id TEXT NOT NULL,
            fire_date TEXT NOT NULL,
            ticker TEXT,
            direction TEXT,
            resolved INTEGER NOT NULL DEFAULT 0,
            result TEXT,
            r_multiple REAL,
            resolution_date TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_pattern "
                 "ON shadow_trades (pattern_id)")
    conn.commit()


def shadow_ref(pattern_id: str, fire_date: str, ticker: str) -> str:
    """Deterministic shadow: ref — idempotent per (pattern, day, ticker)."""
    key = f"{pattern_id}|{fire_date}|{ticker or ''}"
    return "shadow:" + hashlib.sha1(key.encode()).hexdigest()[:14]


# ------------------------------------------------------- split + embargo

def split_windows(days: list, discovery_frac: float = 0.6,
                  embargo_days: int = EMBARGO_DAYS) -> dict:
    """Sorted market days -> {discovery_end, validation_start, embargo}.
    The validation window begins `embargo_days` AFTER discovery_end so a
    trade opened in-discovery but resolving days later can't contaminate
    the out-of-sample read. Returns None-bounds when there isn't room."""
    days = sorted(set(days))
    if len(days) < 4:
        return {"discovery_end": None, "validation_start": None,
                "embargo_days": embargo_days}
    cut = days[max(0, min(len(days) - 1, int(len(days) * discovery_frac)))]
    cut_d = date.fromisoformat(cut)
    val_start = (cut_d + timedelta(days=embargo_days)).isoformat()
    return {"discovery_end": cut, "validation_start": val_start,
            "embargo_days": embargo_days}


def in_validation(day: str, windows: dict) -> bool:
    """Is a market day genuinely out-of-discovery (>= validation_start)?"""
    vs = windows.get("validation_start")
    return bool(vs and day and day >= vs)


# ------------------------------------------------------- shadow tracking

def record_shadow_fire(conn, pattern_id: str, fire_date: str, ticker: str,
                       direction: str = None) -> dict:
    """A discovered pattern's matcher fired live -> a shadow: row (NOT a
    journal entry). Idempotent per (pattern, day, ticker). Returns
    {ref, created}."""
    ensure_schema(conn)
    ref = shadow_ref(pattern_id, fire_date, ticker)
    cur = conn.execute(
        "INSERT INTO shadow_trades (journal_ref, pattern_id, fire_date, "
        "ticker, direction, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (journal_ref) DO NOTHING",
        (ref, pattern_id, fire_date, ticker, direction,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    return {"ref": ref, "created": bool(cur.rowcount)}


def resolve_shadow(conn, ref: str, result: str, r_multiple: float,
                   resolution_date: str) -> bool:
    """Fill a shadow firing's outcome (result in win/loss/scratch), using
    whatever pure resolver the caller ran (the same _resolve_spread /
    forward-return math the tracker uses). Idempotent — first resolution
    wins. Never writes journal/portfolio."""
    ensure_schema(conn)
    row = conn.execute("SELECT resolved FROM shadow_trades "
                       "WHERE journal_ref = ?", (ref,)).fetchone()
    if row is None or row["resolved"]:
        return False
    conn.execute(
        "UPDATE shadow_trades SET resolved = 1, result = ?, r_multiple = ?, "
        "resolution_date = ? WHERE journal_ref = ?",
        (result, r_multiple, resolution_date, ref))
    conn.commit()
    return True


def shadow_evidence(conn, pattern_id: str, windows: dict = None) -> dict:
    """Resolved shadow outcomes for a pattern, restricted to the validation
    window when given (out-of-discovery ONLY). Returns {n, wins}."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT fire_date, result FROM shadow_trades WHERE pattern_id = ? "
        "AND resolved = 1", (pattern_id,)).fetchall()
    n = wins = 0
    for r in rows:
        if windows and not in_validation(r["fire_date"], windows):
            continue
        n += 1
        if r["result"] == "win":
            wins += 1
    return {"n": n, "wins": wins}


# ------------------------------------------------------------- the trial

def evaluate_trial(conn, pattern_id: str, windows: dict,
                   sim_evidence: dict = None,
                   avg_win_r: float = 1.5, avg_loss_r: float = 1.0,
                   base_rate: float = None) -> dict:
    """Run the promotion decision for ONE registered pattern from its
    out-of-discovery evidence: real (shadow) + optional sim strata, against
    the structural breakeven null (or an explicit matched base_rate).
    Drives the registry: promote -> VALIDATED, else INSUFFICIENT_N when
    starved / not-yet, and records oos_stats. Returns the verdict dict."""
    ensure_schema(conn)
    real = shadow_evidence(conn, pattern_id, windows)
    sim = sim_evidence or {"n": 0, "wins": 0}
    null_rate = (base_rate if base_rate is not None
                 else sg.breakeven_win_rate(avg_win_r, avg_loss_r))
    verdict = sg.promotable(real["wins"], real["n"], sim["wins"], sim["n"],
                            null_rate=null_rate)
    verdict["windows"] = windows
    rg.update_oos_stats(conn, pattern_id, {
        "real": real, "sim": sim, "null_rate": round(null_rate, 4),
        "wilson_lb": verdict["wilson_lb"], "evaluated": True})

    row = rg.get(conn, pattern_id)
    if row and row["status"] in ("CANDIDATE", "TRIAL", "INSUFFICIENT_N",
                                 "QUARANTINED"):
        if row["status"] in ("CANDIDATE", "QUARANTINED", "INSUFFICIENT_N"):
            rg.transition(conn, pattern_id, "TRIAL", "trial evaluation begins")
        if verdict["promote"]:
            rg.transition(conn, pattern_id, "VALIDATED",
                          f"OOS Wilson LB {verdict['wilson_lb']} beats null "
                          f"{null_rate:.2f}")
        elif "insufficient" in verdict["reason"] or real["n"] == 0:
            rg.transition(conn, pattern_id, "INSUFFICIENT_N",
                          f"gathering evidence: {verdict['reason']}")
        # else: stays TRIAL (had evidence, failed the bar — re-triable)
    verdict["final_status"] = (rg.get(conn, pattern_id) or {}).get("status")
    return verdict


def learning_corpus_filter(refs) -> list:
    """The self-poisoning guard as a reusable filter: drop every sim:/
    shadow:/trial:/placebo: ref before any tuner/skeptic/miner consumes a
    ref list. Enforced + testable."""
    return [r for r in (refs or []) if sg.is_learnable_ref(r)]

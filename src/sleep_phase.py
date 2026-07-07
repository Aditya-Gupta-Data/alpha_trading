"""
Alpha Trading — Phase 10B (part 2): the "Sleep Phase" consolidation loop
========================================================================

An offline, off-market-hours batch job — the memory housekeeping pass.
Runs three tasks in sequence against data/brain_map.db:

  A. INGESTION    unstructured journal text (the user's "why" + signal
                  line) -> local LLM Episodic Event Frames via
                  local_parser.process_unstructured_input(). Deduped by
                  content hash in the `ingest_log` table, which also
                  stores the provenance pointer (journal_ref) back to
                  the source row.
  B. CONSOLIDATION  the last 24h of raw events -> ONE local LLM call
                  that clusters overlapping themes into higher-level
                  semantic nodes (`semantic_nodes` table), each linked
                  to its underlying episodic events through
                  `semantic_event_link`. Re-observed themes are
                  REINFORCED (confidence back to 1.0) instead of
                  duplicated.
  C. DECAY        every active semantic node's confidence decays as
                  score_new = score_current * e^(-lambda * dt), dt in
                  days since the node was last reinforced or decayed.
                  Below the prune threshold the node is FLAGGED inactive
                  (active=0) — never deleted, so history survives and a
                  re-observed theme reactivates.

STRICT DECOUPLING (DECISIONS.md #30): this script never invokes trading
actions and never touches dhan_client or any market price feed. Its only
network I/O is the local Ollama endpoint (through local_parser). It is
purely a database + text optimization job.

Schema note: the three tables above are created and owned HERE (additive,
same .db file) — src/brain_map.py and its core events/outcomes/link
tables stay byte-for-byte untouched, per the keep-the-store-pure rule.

Config (all optional, read straight from config.json with in-script
defaults so older config copies keep working):
    "sleep_decay_lambda":        0.05   exponential decay coefficient
    "sleep_prune_threshold":     0.20   flag inactive below this score
    "sleep_consolidation_hours": 24     event window for task B

Run manually (or cron it for off-market hours):

    python3 -m src.sleep_phase
"""

import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path

from src import brain_map
from src.local_parser import LocalExtractor, process_unstructured_input

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_DECAY_LAMBDA = 0.05
DEFAULT_PRUNE_THRESHOLD = 0.20
DEFAULT_CONSOLIDATION_HOURS = 24

# Owned by this module — additive to brain_map's core tables, same file.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_log (
    content_hash TEXT PRIMARY KEY,
    journal_ref  TEXT,
    event_id     INTEGER REFERENCES events (id),
    ingested_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_nodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tag              TEXT NOT NULL UNIQUE,
    summary          TEXT,
    sentiment        INTEGER NOT NULL DEFAULT 0,
    confidence_score REAL NOT NULL DEFAULT 1.0,
    created_at       TEXT NOT NULL,
    last_reinforced  TEXT NOT NULL,
    last_decayed     TEXT,
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS semantic_event_link (
    semantic_id INTEGER NOT NULL REFERENCES semantic_nodes (id),
    event_id    INTEGER NOT NULL REFERENCES events (id),
    PRIMARY KEY (semantic_id, event_id)
);
"""

_CONSOLIDATION_PROMPT = (
    "You are a memory consolidator for a trading journal. The user gives "
    "you a numbered list of episodic market events from the last day. "
    "Group events that share an overlapping theme into clusters and name "
    "each cluster as ONE higher-level macro pattern. Return ONLY a JSON "
    "object — no prose, no markdown fences — of this exact shape:\n"
    '{"clusters": [{"tag": string, "summary": string, '
    '"sentiment": integer, "members": [integer, ...]}]}\n'
    "Rules:\n"
    "- tag: short snake_case name of the macro theme (e.g. it_sector_strength).\n"
    "- summary: one plain sentence generalizing the theme.\n"
    "- sentiment: -1, 0, or 1 for the theme overall.\n"
    "- members: the NUMBERS of the input events belonging to the cluster "
    "(an event may appear in at most one cluster).\n"
    "- Only cluster genuinely related events; singletons with no relation "
    "to anything else are simply left out. If nothing clusters, return "
    '{"clusters": []}.'
)


def load_settings(config_path=CONFIG_PATH) -> dict:
    """Sleep-phase knobs from config.json, with hard fallbacks so a
    missing file or missing keys can never break the run."""
    raw = {}
    try:
        with open(config_path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        pass
    return {
        "decay_lambda": float(raw.get("sleep_decay_lambda", DEFAULT_DECAY_LAMBDA)),
        "prune_threshold": float(raw.get("sleep_prune_threshold", DEFAULT_PRUNE_THRESHOLD)),
        "consolidation_hours": float(raw.get("sleep_consolidation_hours",
                                             DEFAULT_CONSOLIDATION_HOURS)),
    }


def ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _content_hash(journal_ref: str, text: str) -> str:
    return hashlib.sha256(f"{journal_ref}|{text}".encode()).hexdigest()


def _journal_text(entry: dict) -> str:
    """The UNSTRUCTURED part of a journal row — the user's reasoning and
    the signal line. The structured fields (prices, outcomes) already
    flow in via brain_map.ingest_existing(); no LLM needed for those."""
    parts = [entry.get("signal"), entry.get("why")]
    return " — ".join(str(p).strip() for p in parts if p and str(p).strip())


# ------------------------------------------------------------ A. ingest

def ingest_journal(conn, journal_entries=None, extractor=None,
                   today: str = None) -> dict:
    """Task A: journal.jsonl free text -> EEF events, hash-deduped.
    Returns {"ingested", "skipped_duplicate", "skipped_empty", "failed"}."""
    if journal_entries is None:
        journal_entries = brain_map._read_journal_file()
    extractor = extractor or LocalExtractor()
    today = today or date.today().isoformat()
    stats = {"ingested": 0, "skipped_duplicate": 0, "skipped_empty": 0, "failed": 0}

    for entry in journal_entries:
        text = _journal_text(entry)
        if not text:
            stats["skipped_empty"] += 1
            continue
        ref = brain_map.journal_ref_for(entry)
        h = _content_hash(ref, text)
        if conn.execute("SELECT 1 FROM ingest_log WHERE content_hash = ?",
                        (h,)).fetchone():
            stats["skipped_duplicate"] += 1
            continue
        event_id = process_unstructured_input(
            conn, text, ticker=entry.get("ticker") or "MARKET",
            event_date=entry.get("date"), extractor=extractor)
        if event_id is None:
            stats["failed"] += 1  # Ollama down / unusable output — retried next run
            continue
        conn.execute(
            "INSERT INTO ingest_log (content_hash, journal_ref, event_id, ingested_at) "
            "VALUES (?, ?, ?, ?)",
            (h, ref, event_id, today),
        )
        conn.commit()
        stats["ingested"] += 1
    return stats


# ------------------------------------------------------ B. consolidation

def consolidate_recent(conn, extractor=None, window_hours: float = None,
                       today: date = None) -> dict:
    """Task B: cluster the recent window's events into semantic nodes.
    Returns {"clusters_created", "clusters_reinforced", "links_added",
    "events_considered"}."""
    extractor = extractor or LocalExtractor()
    today = today or date.today()
    if window_hours is None:
        window_hours = load_settings()["consolidation_hours"]
    cutoff = (today - timedelta(days=max(1, round(window_hours / 24)))).isoformat()

    rows = conn.execute(
        "SELECT id, ticker, event_type, tag, sentiment FROM events "
        "WHERE date >= ? ORDER BY id", (cutoff,)).fetchall()
    stats = {"clusters_created": 0, "clusters_reinforced": 0,
             "links_added": 0, "events_considered": len(rows)}
    if len(rows) < 2:
        return stats  # nothing to cluster

    numbered = "\n".join(
        f"{i + 1}. [{r['ticker']}] {r['event_type']}: {r['tag']} "
        f"(sentiment: {r['sentiment'] or 'neutral'})"
        for i, r in enumerate(rows))
    raw = extractor.chat_json(_CONSOLIDATION_PROMPT, numbered)
    if not isinstance(raw, dict) or not isinstance(raw.get("clusters"), list):
        if raw is not None:
            print("  (sleep phase: consolidator returned an unusable shape)")
        return stats

    today_iso = today.isoformat()
    for cluster in raw["clusters"]:
        if not isinstance(cluster, dict):
            continue
        tag = brain_map._normalize_tag(cluster.get("tag") or "")
        members = cluster.get("members") or []
        member_ids = [rows[m - 1]["id"] for m in members
                      if isinstance(m, int) and 1 <= m <= len(rows)]
        if not tag or len(member_ids) < 2:
            continue  # a theme needs a name and at least two events
        try:
            sentiment = max(-1, min(1, int(cluster.get("sentiment", 0))))
        except (ValueError, TypeError):
            sentiment = 0
        summary = str(cluster.get("summary") or "")[:300]

        existing = conn.execute("SELECT id FROM semantic_nodes WHERE tag = ?",
                                (tag,)).fetchone()
        if existing:
            # Reinforcement: seeing the theme again restores full
            # confidence and reactivates a previously pruned node.
            node_id = existing["id"]
            conn.execute(
                "UPDATE semantic_nodes SET confidence_score = 1.0, "
                "last_reinforced = ?, last_decayed = NULL, active = 1, "
                "summary = ?, sentiment = ? WHERE id = ?",
                (today_iso, summary, sentiment, node_id))
            stats["clusters_reinforced"] += 1
        else:
            cur = conn.execute(
                "INSERT INTO semantic_nodes (tag, summary, sentiment, "
                "confidence_score, created_at, last_reinforced) "
                "VALUES (?, ?, ?, 1.0, ?, ?)",
                (tag, summary, sentiment, today_iso, today_iso))
            node_id = cur.lastrowid
            stats["clusters_created"] += 1
        for event_id in member_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO semantic_event_link (semantic_id, event_id) "
                "VALUES (?, ?)", (node_id, event_id))
            stats["links_added"] += cur.rowcount
        conn.commit()
    return stats


# --------------------------------------------------------------- C. decay

def apply_decay(conn, decay_lambda: float = None, prune_threshold: float = None,
                today: date = None) -> dict:
    """Task C: score_new = score_current * e^(-lambda * dt), dt = days
    since the node was last reinforced OR last decayed (so repeated runs
    never double-count the same days — the decay is exact over any run
    cadence). Below the threshold the node is flagged inactive, never
    deleted. Returns {"decayed", "flagged_inactive", "unchanged"}."""
    settings = load_settings()
    if decay_lambda is None:
        decay_lambda = settings["decay_lambda"]
    if prune_threshold is None:
        prune_threshold = settings["prune_threshold"]
    today = today or date.today()

    stats = {"decayed": 0, "flagged_inactive": 0, "unchanged": 0}
    rows = conn.execute(
        "SELECT id, confidence_score, last_reinforced, last_decayed "
        "FROM semantic_nodes WHERE active = 1").fetchall()
    for row in rows:
        anchor = row["last_decayed"] or row["last_reinforced"]
        try:
            dt_days = (today - date.fromisoformat(anchor)).days
        except (ValueError, TypeError):
            dt_days = 0
        if dt_days <= 0:
            stats["unchanged"] += 1
            continue
        new_score = row["confidence_score"] * math.exp(-decay_lambda * dt_days)
        flagged = new_score < prune_threshold
        conn.execute(
            "UPDATE semantic_nodes SET confidence_score = ?, last_decayed = ?, "
            "active = ? WHERE id = ?",
            (round(new_score, 6), today.isoformat(), 0 if flagged else 1, row["id"]))
        stats["decayed"] += 1
        if flagged:
            stats["flagged_inactive"] += 1
    conn.commit()
    return stats


# --------------------------------------------------------------- runner

def run_sleep_phase(db_path=None, extractor=None, today: date = None) -> dict:
    """The full A -> B -> C pass. Each task is fail-safe on its own; one
    failing never blocks the next. Returns the combined stats dict."""
    today = today or date.today()
    conn = brain_map.connect(db_path)
    ensure_schema(conn)
    extractor = extractor or LocalExtractor()
    results = {}

    print(f"Sleep phase — {today.isoformat()} (offline memory pass)")
    if not extractor.is_reachable():
        print(f"  note: Ollama not reachable at {extractor.base_url} — "
              "ingestion/consolidation will skip; decay still runs.")

    try:
        results["ingestion"] = ingest_journal(conn, extractor=extractor,
                                              today=today.isoformat())
        print(f"  A. ingestion:     {results['ingestion']}")
    except Exception as e:
        print(f"  A. ingestion failed: {e}")
        results["ingestion"] = None
    try:
        results["consolidation"] = consolidate_recent(conn, extractor=extractor,
                                                      today=today)
        print(f"  B. consolidation: {results['consolidation']}")
    except Exception as e:
        print(f"  B. consolidation failed: {e}")
        results["consolidation"] = None
    try:
        results["decay"] = apply_decay(conn, today=today)
        print(f"  C. decay:         {results['decay']}")
    except Exception as e:
        print(f"  C. decay failed: {e}")
        results["decay"] = None

    conn.close()
    return results


if __name__ == "__main__":
    run_sleep_phase()

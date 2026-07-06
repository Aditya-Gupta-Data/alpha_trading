"""
Alpha Trading -- Phase 6: the Brain Map
========================================

A relational event-pattern memory that upgrades the flat
data/brain_weights.json nudge (which only scores two BUY archetypes) into
a store that can answer the question the tuner can't:

    "Has this cluster of events happened before, and did it make money?"

It is a standalone SQLite database at data/brain_map.db, built on Python's
native sqlite3 module only -- no Postgres/Mongo/cloud DB (DECISIONS.md
decision #19 and the VISION_PLAN guardrail). Three small tables:

  events              one row per observation: a news item, a strategy
                      signal, a chart pattern tag... `tag` is the
                      normalized pattern key that clustering matches on.
  outcomes            one row per resolved trade, keyed by a stable
                      `journal_ref` (journal rows have no id yet, so the
                      caller passes a deterministic composite key like
                      "date|ticker|action|price").
  event_outcome_link  many-to-many glue recording which events were "in
                      the air" when a trade resolved -- this link table is
                      what makes cluster queries possible.

STRICTLY ADDITIVE (non-negotiable, per DECISIONS.md "Phase 6" section):
tuner.py, brain_weights.json and forecast.py are untouched and keep
running exactly as before; forecast.py is NOT wired to query this store
yet. The Brain Map only records and reads history -- it never touches
execution, data/portfolio.json, or the JSON ledgers.

Every public function takes an open sqlite3 connection as its first
argument (get one from connect()), so tests can run against ':memory:'
without ever touching the real data/brain_map.db.

Run a quick health summary from the project folder with:

    python3 -m src.brain_map
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "brain_map.db"

# How many linked outcomes query_similar_events() returns as examples.
MAX_EXAMPLES = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    tag         TEXT NOT NULL,
    sentiment   TEXT,
    entities    TEXT,               -- JSON-encoded dict/list, or NULL
    source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_tag ON events (tag);
CREATE INDEX IF NOT EXISTS idx_events_ticker_date ON events (ticker, date);

CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    journal_ref TEXT NOT NULL UNIQUE,
    date        TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    archetype   TEXT,
    r_multiple  REAL,
    result      TEXT NOT NULL CHECK (result IN ('win', 'loss', 'scratch'))
);

CREATE TABLE IF NOT EXISTS event_outcome_link (
    event_id    INTEGER NOT NULL REFERENCES events (id),
    outcome_id  INTEGER NOT NULL REFERENCES outcomes (id),
    PRIMARY KEY (event_id, outcome_id)
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    """Open (creating if needed) the Brain Map database and make sure the
    three tables exist. Pass ':memory:' for a throwaway in-test database;
    the default is the real data/brain_map.db."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def record_event(conn, date, ticker, event_type, tag,
                 sentiment=None, entities=None, source=None) -> int:
    """Insert one observation and return its event id. `tag` is the
    normalized pattern key clustering matches on (e.g. "earnings_beat",
    "golden_cross"); `entities` may be a dict/list and is stored as JSON."""
    if entities is not None and not isinstance(entities, str):
        entities = json.dumps(entities)
    cur = conn.execute(
        "INSERT INTO events (date, ticker, event_type, tag, sentiment, entities, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (date, ticker, event_type, tag, sentiment, entities, source),
    )
    conn.commit()
    return cur.lastrowid


def record_outcome(conn, journal_ref, date, ticker, archetype=None,
                   r_multiple=None, result=None) -> int:
    """Insert one resolved trade and return its outcome id. `journal_ref`
    is the stable key back to the journal entry; re-recording the same ref
    is a no-op that returns the existing row's id, so future backfills can
    re-run safely. If `result` isn't given it's derived from r_multiple
    (positive -> win, negative -> loss, zero -> scratch)."""
    if result is None:
        if r_multiple is None:
            raise ValueError("record_outcome needs `result` or an r_multiple to derive it from")
        result = "win" if r_multiple > 0 else ("loss" if r_multiple < 0 else "scratch")
    cur = conn.execute(
        "INSERT INTO outcomes (journal_ref, date, ticker, archetype, r_multiple, result) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (journal_ref) DO NOTHING",
        (journal_ref, date, ticker, archetype, r_multiple, result),
    )
    conn.commit()
    if cur.lastrowid and cur.rowcount:
        return cur.lastrowid
    row = conn.execute("SELECT id FROM outcomes WHERE journal_ref = ?", (journal_ref,)).fetchone()
    return row["id"]


def link_event_outcome(conn, event_id, outcome_id) -> None:
    """Record that an event was "in the air" when a trade resolved.
    Idempotent -- linking the same pair twice is a no-op."""
    conn.execute(
        "INSERT OR IGNORE INTO event_outcome_link (event_id, outcome_id) VALUES (?, ?)",
        (event_id, outcome_id),
    )
    conn.commit()


def query_similar_events(conn, tags) -> dict:
    """The core question-answerer: across every outcome linked to an event
    carrying any of `tags`, how often did the trade pay?

    Returns {count, win_rate, avg_r_multiple, examples}:
      count           distinct linked outcomes (an outcome linked to two
                      matching events still counts once)
      win_rate        wins / count -- scratches count against the rate,
                      the same way review.py scores the user's calls
      avg_r_multiple  mean R over outcomes that carry an r_multiple
      examples        up to MAX_EXAMPLES most recent outcomes, each with
                      the matched tags for context
    win_rate / avg_r_multiple are None when there's no history yet."""
    if isinstance(tags, str):
        tags = [tags]
    tags = list(tags)
    if not tags:
        return {"count": 0, "win_rate": None, "avg_r_multiple": None, "examples": []}

    placeholders = ", ".join("?" for _ in tags)
    rows = conn.execute(
        f"""
        SELECT o.id, o.journal_ref, o.date, o.ticker, o.archetype, o.r_multiple, o.result,
               GROUP_CONCAT(DISTINCT e.tag) AS matched_tags
        FROM outcomes o
        JOIN event_outcome_link l ON l.outcome_id = o.id
        JOIN events e ON e.id = l.event_id
        WHERE e.tag IN ({placeholders})
        GROUP BY o.id
        ORDER BY o.date DESC, o.id DESC
        """,
        tags,
    ).fetchall()

    count = len(rows)
    if count == 0:
        return {"count": 0, "win_rate": None, "avg_r_multiple": None, "examples": []}

    wins = sum(1 for r in rows if r["result"] == "win")
    r_values = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    return {
        "count": count,
        "win_rate": round(wins / count, 2),
        "avg_r_multiple": round(sum(r_values) / len(r_values), 2) if r_values else None,
        "examples": [
            {
                "date": r["date"],
                "ticker": r["ticker"],
                "archetype": r["archetype"],
                "r_multiple": r["r_multiple"],
                "result": r["result"],
                "matched_tags": sorted((r["matched_tags"] or "").split(",")),
            }
            for r in rows[:MAX_EXAMPLES]
        ],
    }


def _summary(conn) -> dict:
    """Row counts per table, for the CLI health check below."""
    return {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("events", "outcomes", "event_outcome_link")
    }


if __name__ == "__main__":
    connection = connect()
    counts = _summary(connection)
    print(f"Brain Map at {DEFAULT_DB_PATH}:")
    for table, n in counts.items():
        print(f"  {table}: {n} row(s)")
    connection.close()

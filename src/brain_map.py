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

ingest_existing() seeds the map from the data the engine already produces
(resolved journal.jsonl trades + news_sentiment.json) and is idempotent —
safe to re-run any time to pick up newly resolved trades. It reads the
journal file directly rather than importing src.journal, keeping this
module standalone (no src.config dependency), same modularity call as the
per-entry-point _load_env pattern (see DECISIONS.md).

Run a quick health summary, or seed the real database, from the project
folder with:

    python3 -m src.brain_map            # row counts only
    python3 -m src.brain_map ingest     # seed/refresh from journal + news
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "brain_map.db"
JOURNAL_PATH = ROOT / "data" / "journal.jsonl"
NEWS_SENTIMENT_PATH = ROOT / "data" / "news_sentiment.json"

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
    result      TEXT NOT NULL CHECK (result IN ('win', 'loss', 'scratch')),
    post_mortem TEXT                -- JSON post-mortem from src/analyst.py, or NULL
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
    # In-place upgrade for databases created before the post_mortem column
    # existed -- CREATE TABLE IF NOT EXISTS can't add columns to an
    # already-created table.
    outcome_cols = {row["name"] for row in conn.execute("PRAGMA table_info(outcomes)")}
    if "post_mortem" not in outcome_cols:
        conn.execute("ALTER TABLE outcomes ADD COLUMN post_mortem TEXT")
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
                   r_multiple=None, result=None, post_mortem=None) -> int:
    """Insert one resolved trade and return its outcome id. `journal_ref`
    is the stable key back to the journal entry; re-recording the same ref
    is a no-op that returns the existing row's id, so future backfills can
    re-run safely. If `result` isn't given it's derived from r_multiple
    (positive -> win, negative -> loss, zero -> scratch). `post_mortem` is
    the analyst's comparative breakdown (dict, stored as JSON); when the
    row already exists without one, a provided post_mortem is backfilled
    rather than dropped."""
    if result is None:
        if r_multiple is None:
            raise ValueError("record_outcome needs `result` or an r_multiple to derive it from")
        result = "win" if r_multiple > 0 else ("loss" if r_multiple < 0 else "scratch")
    if post_mortem is not None and not isinstance(post_mortem, str):
        post_mortem = json.dumps(post_mortem)
    cur = conn.execute(
        "INSERT INTO outcomes (journal_ref, date, ticker, archetype, r_multiple, result, post_mortem) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (journal_ref) DO NOTHING",
        (journal_ref, date, ticker, archetype, r_multiple, result, post_mortem),
    )
    conn.commit()
    if cur.lastrowid and cur.rowcount:
        return cur.lastrowid
    if post_mortem is not None:
        conn.execute(
            "UPDATE outcomes SET post_mortem = ? WHERE journal_ref = ? AND post_mortem IS NULL",
            (post_mortem, journal_ref),
        )
        conn.commit()
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


def journal_ref_for(entry: dict) -> str:
    """The stable key back to a journal row: its Phase 6 `short_id` when
    present, else the deterministic composite fallback for older lines
    that predate short_ids (date|ticker|action|price)."""
    if entry.get("short_id"):
        return entry["short_id"]
    return f"{entry.get('date')}|{entry.get('ticker')}|{entry.get('action')}|{entry.get('price')}"


# Same signal-text -> archetype mapping tuner.py uses. Duplicated on
# purpose rather than imported: the Brain Map must stay standalone and
# strictly additive, touching nothing on the live tuner/forecast path.
def _archetype_for(signal: str) -> str:
    if "Cross" in signal:
        return "fresh_cross"
    if "RSI" in signal:
        return "rsi_oversold"
    return "other"


def _normalize_tag(text: str) -> str:
    """Free text -> the normalized pattern key clustering matches on:
    "Golden Cross" -> "golden_cross", "earnings miss" -> "earnings_miss"."""
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def _read_journal_file(path=JOURNAL_PATH) -> list:
    if not Path(path).exists():
        return []
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _read_news_file(path=NEWS_SENTIMENT_PATH) -> dict:
    if not Path(path).exists():
        return {}
    with open(path, "r") as f:
        return json.load(f)


def _get_or_create_event(conn, date, ticker, event_type, tag,
                         sentiment=None, entities=None, source=None) -> int:
    """record_event, but idempotent on (date, ticker, event_type, tag,
    source) -- what makes re-running ingest_existing() safe. Direct
    record_event callers who *want* repeat rows are unaffected."""
    row = conn.execute(
        "SELECT id FROM events WHERE date = ? AND ticker = ? AND event_type = ? "
        "AND tag = ? AND IFNULL(source, '') = IFNULL(?, '')",
        (date, ticker, event_type, tag, source),
    ).fetchone()
    if row:
        return row["id"]
    return record_event(conn, date, ticker, event_type, tag,
                        sentiment=sentiment, entities=entities, source=source)


def record_resolved_entry(conn, entry, post_mortem=None):
    """One resolved journal entry -> its `outcomes` row plus the signal /
    pattern-tag events that were "in the air" at entry, all linked. This is
    the single write path shared by ingest_existing() (backfill sweeps)
    and plan_tracker (live, at the moment of resolution, with the
    analyst's post-mortem attached). Keyed by journal_ref_for(entry) --
    the short_id when present -- and idempotent like everything else here.
    Returns the outcome id, or None if the entry isn't resolved with an
    r_multiple yet."""
    outcome = entry.get("outcome")
    if not outcome or outcome.get("r_multiple") is None:
        return None
    signal = entry.get("signal", "")
    archetype = _archetype_for(signal)
    outcome_id = record_outcome(
        conn,
        journal_ref=journal_ref_for(entry),
        date=outcome.get("exit_date") or entry["date"],
        ticker=entry["ticker"],
        archetype=archetype,
        r_multiple=outcome["r_multiple"],
        post_mortem=post_mortem,
    )
    # One event for the strategy signal (tagged by archetype when it's a
    # known one, else by the normalized signal text) + one per user-chosen
    # pattern tag.
    events = []
    if signal:
        sig_tag = archetype if archetype != "other" else _normalize_tag(signal)
        events.append(("signal", sig_tag, {"signal": signal}))
    for raw_tag in entry.get("pattern_tags") or []:
        events.append(("pattern", _normalize_tag(raw_tag), {"pattern_tag": raw_tag}))
    for event_type, tag, entities in events:
        event_id = _get_or_create_event(conn, entry["date"], entry["ticker"],
                                        event_type, tag, entities=entities,
                                        source="journal")
        link_event_outcome(conn, event_id, outcome_id)
    return outcome_id


def build_episode_snapshot(entry: dict, news=None) -> dict:
    """The "Trade Episode" context snapshot for one *resolved* journal
    entry: what the market felt like (news sentiment), what prices did
    (entry/exit), and which plan rule fired (stop/target/time stop).

    Pure and read-only by design — no network, no DB writes — per the
    strictly-additive rule (DECISIONS.md #25): the Brain Map only records
    and reads history. The caller (plan_tracker -> src/api.py's background
    loop) decides where to send it (Discord, via notifier). Returns None
    for entries that haven't resolved yet."""
    outcome = entry.get("outcome")
    if not outcome:
        return None
    if news is None:
        news = _read_news_file()
    info = (news.get("tickers") or {}).get(entry.get("ticker")) or {}
    return {
        "journal_ref": journal_ref_for(entry),
        "ticker": entry.get("ticker"),
        "entry_date": entry.get("date"),
        "entry_price": entry.get("price"),
        "exit_date": outcome.get("exit_date"),
        "exit_price": outcome.get("price"),
        "resolution": outcome.get("resolution"),
        "rules_breached": [r for r in [outcome.get("resolution")] if r],
        "r_multiple": outcome.get("r_multiple"),
        "pnl_rs": outcome.get("pnl_rs"),
        "verdict": outcome.get("verdict"),
        "signal": entry.get("signal"),
        "pattern_tags": entry.get("pattern_tags") or [],
        "market_sentiment": {
            "score": info.get("sentiment_score"),
            "headline_focus": info.get("headline_focus"),
        },
    }


def ingest_existing(conn, journal_entries=None, news=None) -> dict:
    """Seed the Brain Map from data the engine already produces.

    Journal (data/journal.jsonl): every *resolved* trade -- outcome present
    with an r_multiple -- becomes one `outcomes` row keyed by
    journal_ref_for(), plus one `events` row for its strategy signal and
    one per pattern_tag, all linked as "in the air" at entry. Unresolved
    trades (outcome null, or r_multiple null) are skipped and picked up by
    a later re-run once the plan tracker resolves them.

    News (data/news_sentiment.json): each ticker's current sentiment
    snapshot becomes one `events` row (tag = normalized headline_focus).
    Not linked to outcomes here -- news-to-trade linking needs the
    "same ticker, around the trade date" window logic, a later step.

    Idempotent: outcomes dedupe on journal_ref, events via
    _get_or_create_event, links via INSERT OR IGNORE -- a second run adds
    nothing. Pass journal_entries/news directly in tests; defaults read
    the real data files. Returns a summary of what this run added."""
    if journal_entries is None:
        journal_entries = _read_journal_file()
    if news is None:
        news = _read_news_file()

    before = _summary(conn)
    skipped_unresolved = 0

    for entry in journal_entries:
        if record_resolved_entry(conn, entry) is None:
            skipped_unresolved += 1

    for ticker, info in (news.get("tickers") or {}).items():
        score = info.get("sentiment_score")
        sentiment = None if score is None else (
            "positive" if score > 0 else "negative" if score < 0 else "neutral")
        focus = info.get("headline_focus") or "news"
        event_date = (info.get("last_updated") or news.get("generated") or "")[:10]
        _get_or_create_event(conn, event_date, ticker, "news", _normalize_tag(focus),
                             sentiment=sentiment,
                             entities={"sentiment_score": score, "headline_focus": focus},
                             source="news_sentiment")

    after = _summary(conn)
    return {
        "events_added": after["events"] - before["events"],
        "outcomes_added": after["outcomes"] - before["outcomes"],
        "links_added": after["event_outcome_link"] - before["event_outcome_link"],
        "journal_rows_skipped_unresolved": skipped_unresolved,
    }


def _summary(conn) -> dict:
    """Row counts per table, for the CLI health check below."""
    return {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("events", "outcomes", "event_outcome_link")
    }


if __name__ == "__main__":
    connection = connect()
    if sys.argv[1:] == ["ingest"]:
        added = ingest_existing(connection)
        print("Brain Map ingest complete:")
        print(f"  events added:   {added['events_added']}")
        print(f"  outcomes added: {added['outcomes_added']}")
        print(f"  links added:    {added['links_added']}")
        print(f"  journal rows skipped (not resolved yet): "
              f"{added['journal_rows_skipped_unresolved']}")
    counts = _summary(connection)
    print(f"Brain Map at {DEFAULT_DB_PATH}:")
    for table, n in counts.items():
        print(f"  {table}: {n} row(s)")
    connection.close()

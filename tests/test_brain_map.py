"""
Tests for the Phase 6 Brain Map (src/brain_map.py), using in-memory SQLite
databases so they run instantly, need no internet, and never touch the
real data/brain_map.db.

Run either of these from the project folder:
    python tests/test_brain_map.py      (simple, no extra installs)
    python -m pytest tests/             (if you have pytest)
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map


def fresh_db():
    return brain_map.connect(":memory:")


def seed_outcome_with_event(conn, tag="earnings_beat", r_multiple=1.0,
                            ticker="TCS.NS", date="2026-07-01", ref=None):
    """One event linked to one resolved trade -- the minimal cluster unit."""
    event_id = brain_map.record_event(conn, date, ticker, "news", tag)
    ref = ref or f"{date}|{ticker}|BUY|{r_multiple}"
    outcome_id = brain_map.record_outcome(conn, ref, date, ticker,
                                          archetype="fresh_cross", r_multiple=r_multiple)
    brain_map.link_event_outcome(conn, event_id, outcome_id)
    return event_id, outcome_id


def test_connect_creates_all_three_tables():
    conn = fresh_db()
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"events", "outcomes", "event_outcome_link"} <= tables


def test_connect_creates_missing_data_directory_on_disk():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "not_there_yet" / "brain_map.db"
        conn = brain_map.connect(db_path)
        conn.close()
        assert db_path.exists()


def test_record_event_inserts_and_serializes_entities():
    conn = fresh_db()
    event_id = brain_map.record_event(
        conn, "2026-07-01", "TCS.NS", "news", "earnings_beat",
        sentiment="positive", entities={"quarter": "Q1"}, source="google_news")
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["ticker"] == "TCS.NS"
    assert row["tag"] == "earnings_beat"
    assert json.loads(row["entities"]) == {"quarter": "Q1"}


def test_record_outcome_derives_result_from_r_multiple():
    conn = fresh_db()
    for ref, r, expected in [("a", 1.5, "win"), ("b", -0.8, "loss"), ("c", 0.0, "scratch")]:
        outcome_id = brain_map.record_outcome(conn, ref, "2026-07-01", "INFY.NS",
                                              r_multiple=r)
        row = conn.execute("SELECT result FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
        assert row["result"] == expected


def test_record_outcome_requires_result_or_r_multiple():
    conn = fresh_db()
    try:
        brain_map.record_outcome(conn, "ref", "2026-07-01", "INFY.NS")
        assert False, "expected a ValueError"
    except ValueError:
        pass


def test_record_outcome_rejects_invalid_result():
    conn = fresh_db()
    try:
        brain_map.record_outcome(conn, "ref", "2026-07-01", "INFY.NS", result="maybe")
        assert False, "expected the CHECK constraint to reject 'maybe'"
    except sqlite3.IntegrityError:
        pass


def test_record_outcome_is_idempotent_on_journal_ref():
    conn = fresh_db()
    first = brain_map.record_outcome(conn, "2026-07-01|TCS.NS|BUY|3500",
                                     "2026-07-01", "TCS.NS", r_multiple=2.0)
    second = brain_map.record_outcome(conn, "2026-07-01|TCS.NS|BUY|3500",
                                      "2026-07-01", "TCS.NS", r_multiple=2.0)
    assert first == second
    assert conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"] == 1


def test_link_event_outcome_is_idempotent():
    conn = fresh_db()
    event_id, outcome_id = seed_outcome_with_event(conn)
    brain_map.link_event_outcome(conn, event_id, outcome_id)  # same pair again
    assert conn.execute("SELECT COUNT(*) AS n FROM event_outcome_link").fetchone()["n"] == 1


def test_query_with_no_history_returns_empty_cluster():
    conn = fresh_db()
    result = brain_map.query_similar_events(conn, ["earnings_beat"])
    assert result == {"count": 0, "win_rate": None, "avg_r_multiple": None, "examples": []}


def test_query_calculates_win_rate_and_avg_r():
    conn = fresh_db()
    seed_outcome_with_event(conn, r_multiple=2.0, ref="t1")
    seed_outcome_with_event(conn, r_multiple=1.0, ref="t2")
    seed_outcome_with_event(conn, r_multiple=-1.0, ref="t3")
    result = brain_map.query_similar_events(conn, ["earnings_beat"])
    assert result["count"] == 3
    assert result["win_rate"] == round(2 / 3, 2)
    assert result["avg_r_multiple"] == round((2.0 + 1.0 - 1.0) / 3, 2)


def test_scratches_count_against_the_win_rate():
    conn = fresh_db()
    seed_outcome_with_event(conn, r_multiple=1.0, ref="t1")
    seed_outcome_with_event(conn, r_multiple=0.0, ref="t2")  # scratch
    result = brain_map.query_similar_events(conn, ["earnings_beat"])
    assert result["count"] == 2
    assert result["win_rate"] == 0.5


def test_outcome_linked_to_two_matching_events_counts_once():
    conn = fresh_db()
    event_a = brain_map.record_event(conn, "2026-07-01", "TCS.NS", "news", "earnings_beat")
    event_b = brain_map.record_event(conn, "2026-07-01", "TCS.NS", "signal", "golden_cross")
    outcome_id = brain_map.record_outcome(conn, "t1", "2026-07-02", "TCS.NS", r_multiple=2.0)
    brain_map.link_event_outcome(conn, event_a, outcome_id)
    brain_map.link_event_outcome(conn, event_b, outcome_id)
    result = brain_map.query_similar_events(conn, ["earnings_beat", "golden_cross"])
    assert result["count"] == 1
    assert result["win_rate"] == 1.0
    assert result["examples"][0]["matched_tags"] == ["earnings_beat", "golden_cross"]


def test_query_only_matches_the_requested_tags():
    conn = fresh_db()
    seed_outcome_with_event(conn, tag="earnings_beat", r_multiple=2.0, ref="t1")
    seed_outcome_with_event(conn, tag="rate_hike", r_multiple=-1.0, ref="t2")
    result = brain_map.query_similar_events(conn, ["earnings_beat"])
    assert result["count"] == 1
    assert result["win_rate"] == 1.0
    assert result["avg_r_multiple"] == 2.0


def test_query_accepts_a_single_tag_string_and_empty_tags():
    conn = fresh_db()
    seed_outcome_with_event(conn, tag="earnings_beat", ref="t1")
    assert brain_map.query_similar_events(conn, "earnings_beat")["count"] == 1
    assert brain_map.query_similar_events(conn, [])["count"] == 0


def test_examples_are_capped_and_newest_first():
    conn = fresh_db()
    for day in range(1, 8):  # 7 linked outcomes across a week
        seed_outcome_with_event(conn, date=f"2026-07-0{day}", ref=f"t{day}")
    result = brain_map.query_similar_events(conn, ["earnings_beat"])
    assert result["count"] == 7
    assert len(result["examples"]) == brain_map.MAX_EXAMPLES
    assert result["examples"][0]["date"] == "2026-07-07"


def test_avg_r_is_none_when_no_outcome_carries_an_r_multiple():
    conn = fresh_db()
    event_id = brain_map.record_event(conn, "2026-07-01", "TCS.NS", "news", "earnings_beat")
    outcome_id = brain_map.record_outcome(conn, "t1", "2026-07-02", "TCS.NS", result="win")
    brain_map.link_event_outcome(conn, event_id, outcome_id)
    result = brain_map.query_similar_events(conn, ["earnings_beat"])
    assert result["count"] == 1
    assert result["win_rate"] == 1.0
    assert result["avg_r_multiple"] is None


# --- ingest_existing() -------------------------------------------------
# Mock journal entries mirror the real journal.jsonl shape (see
# src/journal.new_entry and the outcome dict plan_tracker.run_tracker
# writes); ingest tests pass them in directly so the real data files are
# never read.

def make_journal_entry(short_id=None, resolved=True, r_multiple=1.5,
                       signal="Fresh Golden Cross", pattern_tags=None,
                       ticker="TCS.NS", date="2026-07-01", action="BUY",
                       price=3500.0):
    entry = {
        "date": date, "action": action, "ticker": ticker, "shares": 5,
        "price": price, "signal": signal, "decision": "approved",
        "why": "test", "pattern_tags": pattern_tags or [],
        "plan": {"stop_loss": {"pct": 3.0, "price": price * 0.97}},
        "outcome": ({"resolution": "target_hit", "exit_date": "2026-07-05",
                     "r_multiple": r_multiple} if resolved else None),
    }
    if short_id:
        entry["short_id"] = short_id
    return entry


def test_ingest_records_resolved_trades_keyed_by_short_id():
    conn = fresh_db()
    added = brain_map.ingest_existing(
        conn, journal_entries=[make_journal_entry(short_id="abc12345")], news={})
    assert added["outcomes_added"] == 1
    row = conn.execute("SELECT * FROM outcomes").fetchone()
    assert row["journal_ref"] == "abc12345"
    assert row["r_multiple"] == 1.5
    assert row["result"] == "win"
    assert row["archetype"] == "fresh_cross"
    assert row["date"] == "2026-07-05"  # exit date, not entry date


def test_ingest_falls_back_to_composite_key_for_old_entries():
    conn = fresh_db()
    brain_map.ingest_existing(conn, journal_entries=[make_journal_entry()], news={})
    row = conn.execute("SELECT journal_ref FROM outcomes").fetchone()
    assert row["journal_ref"] == "2026-07-01|TCS.NS|BUY|3500.0"


def test_ingest_skips_unresolved_and_r_less_trades():
    conn = fresh_db()
    unresolved = make_journal_entry(resolved=False)
    no_r = make_journal_entry(short_id="norr")
    no_r["outcome"]["r_multiple"] = None
    added = brain_map.ingest_existing(conn, journal_entries=[unresolved, no_r], news={})
    assert added["outcomes_added"] == 0
    assert added["journal_rows_skipped_unresolved"] == 2


def test_ingest_creates_and_links_signal_and_pattern_events():
    conn = fresh_db()
    entry = make_journal_entry(short_id="abc12345", r_multiple=2.0,
                               pattern_tags=["Golden Cross", "Breakout"])
    added = brain_map.ingest_existing(conn, journal_entries=[entry], news={})
    assert added["events_added"] == 3   # 1 signal + 2 pattern tags
    assert added["links_added"] == 3
    # The cluster query answers off the ingested data straight away:
    result = brain_map.query_similar_events(conn, ["breakout"])
    assert result["count"] == 1
    assert result["win_rate"] == 1.0
    assert result["avg_r_multiple"] == 2.0
    # ...and the signal event is tagged by its tuner archetype:
    assert brain_map.query_similar_events(conn, ["fresh_cross"])["count"] == 1


def test_ingest_tags_unknown_signals_by_normalized_text():
    conn = fresh_db()
    entry = make_journal_entry(short_id="x1", signal="Volume Breakout — Cup & Handle")
    brain_map.ingest_existing(conn, journal_entries=[entry], news={})
    row = conn.execute("SELECT tag, event_type FROM events").fetchone()
    assert row["event_type"] == "signal"
    assert row["tag"] == "volume_breakout_cup_handle"


def test_ingest_is_idempotent_on_a_second_run():
    conn = fresh_db()
    entries = [
        make_journal_entry(short_id="abc12345", pattern_tags=["Golden Cross"]),
        make_journal_entry(r_multiple=-1.0, ticker="INFY.NS",
                           signal="uptrend with a dip (RSI 28)"),
    ]
    news = {"tickers": {"TCS.NS": {"sentiment_score": -5,
                                   "headline_focus": "sharp price crash",
                                   "last_updated": "2026-07-05T12:02:44+00:00"}}}
    first = brain_map.ingest_existing(conn, journal_entries=entries, news=news)
    assert first["outcomes_added"] == 2
    second = brain_map.ingest_existing(conn, journal_entries=entries, news=news)
    assert second == {"events_added": 0, "outcomes_added": 0, "links_added": 0,
                      "journal_rows_skipped_unresolved": 0}


def test_ingest_seeds_news_sentiment_events():
    conn = fresh_db()
    news = {"tickers": {
        "TCS.NS": {"sentiment_score": -5, "headline_focus": "sharp price crash",
                   "last_updated": "2026-07-05T12:02:44+00:00"},
        "ITC.NS": {"sentiment_score": 1, "headline_focus": "dividend resilience",
                   "last_updated": "2026-07-05T12:02:44+00:00"},
    }}
    added = brain_map.ingest_existing(conn, journal_entries=[], news=news)
    assert added["events_added"] == 2
    row = conn.execute(
        "SELECT * FROM events WHERE ticker = 'TCS.NS'").fetchone()
    assert row["event_type"] == "news"
    assert row["tag"] == "sharp_price_crash"
    assert row["sentiment"] == "negative"
    assert row["date"] == "2026-07-05"
    assert json.loads(row["entities"])["sentiment_score"] == -5


def test_journal_ref_for_prefers_short_id():
    entry = make_journal_entry(short_id="abc12345")
    assert brain_map.journal_ref_for(entry) == "abc12345"
    del entry["short_id"]
    assert brain_map.journal_ref_for(entry) == "2026-07-01|TCS.NS|BUY|3500.0"


def test_new_journal_entries_carry_a_short_id():
    from src import journal
    proposal = {"action": "BUY", "ticker": "TCS.NS", "shares": 5,
                "price": 3500.0, "signal": "Fresh Golden Cross"}
    entry = journal.new_entry(proposal, "approved", "test")
    assert len(entry["short_id"]) == 8
    int(entry["short_id"], 16)  # valid hex, raises if not
    another = journal.new_entry(proposal, "approved", "test")
    assert entry["short_id"] != another["short_id"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

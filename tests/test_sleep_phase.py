"""
Tests for the Phase 10B "Sleep Phase" loop (src/sleep_phase.py):
hash-deduped journal ingestion, LLM-clustered consolidation with graph
links, the exponential decay engine, config fallbacks, and the
decision-#30 decoupling guard.

Offline — Ollama is always mocked (a fake LocalExtractor); everything
runs against an in-memory Brain Map.

Run either of these from the project folder:
    python tests/test_sleep_phase.py      (simple, no extra installs)
    python -m pytest tests/                (if you have pytest)
"""

import math
import sys
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import sleep_phase
from src.local_parser import LocalExtractor


TODAY = date(2026, 7, 6)

JOURNAL_ENTRIES = [
    {"short_id": "aaaa1111", "date": "2026-07-06", "ticker": "TCS.NS",
     "action": "BUY", "price": 4000.0, "signal": "Fresh Golden Cross",
     "why": "IT sector momentum looks strong after earnings"},
    {"short_id": "bbbb2222", "date": "2026-07-06", "ticker": "INFY.NS",
     "action": "BUY", "price": 1500.0, "signal": "uptrend with a dip (RSI 28)",
     "why": "same IT strength, buying the dip"},
    {"short_id": "cccc3333", "date": "2026-07-06", "ticker": "ONGC.NS",
     "action": "SELL", "price": 250.0, "signal": "", "why": ""},  # no text
]


def fresh_conn():
    conn = brain_map.connect(":memory:")
    sleep_phase.ensure_schema(conn)
    return conn


def fake_extractor(frames=None, clusters=None, reachable=True):
    """A LocalExtractor stand-in: extract_event_json pops from `frames`
    per call; chat_json returns the given consolidation payload."""
    ex = mock.Mock(spec=LocalExtractor)
    ex.base_url = "http://localhost:11434/v1"
    ex.is_reachable.return_value = reachable
    frames = list(frames or [])
    ex.extract_event_json.side_effect = (
        lambda text: frames.pop(0) if frames else None)
    ex.chat_json.return_value = clusters
    return ex


FRAME_TCS = {"event_type": "journal", "tag": "it_momentum",
             "sentiment": 1, "entities": ["TCS"]}
FRAME_INFY = {"event_type": "journal", "tag": "it_dip_buy",
              "sentiment": 1, "entities": ["INFY"]}


# ---------------------------------------------------------- A. ingestion

def test_ingest_from_dummy_journal_without_double_inserting():
    conn = fresh_conn()
    ex = fake_extractor(frames=[dict(FRAME_TCS), dict(FRAME_INFY)])
    stats = sleep_phase.ingest_journal(conn, JOURNAL_ENTRIES, extractor=ex,
                                       today=TODAY.isoformat())
    assert stats == {"ingested": 2, "skipped_duplicate": 0,
                     "skipped_empty": 1, "failed": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 2

    # Provenance pointers land in ingest_log, keyed back to the journal row:
    logged = {r["journal_ref"] for r in conn.execute("SELECT * FROM ingest_log")}
    assert logged == {"aaaa1111", "bbbb2222"}

    # Re-run: everything hash-dedupes, the LLM isn't even called again:
    ex2 = fake_extractor(frames=[])
    stats2 = sleep_phase.ingest_journal(conn, JOURNAL_ENTRIES, extractor=ex2,
                                        today=TODAY.isoformat())
    assert stats2["ingested"] == 0 and stats2["skipped_duplicate"] == 2
    ex2.extract_event_json.assert_not_called()
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 2
    conn.close()


def test_ingest_defers_quietly_when_no_extractor_host():
    """VM case (2026-07-12 ops-card noise fix): no reachable Ollama ->
    rows DEFER with an explicit reason instead of counting as failures,
    and nothing is hash-logged so the Mac's pass ingests them."""
    conn = fresh_conn()
    ex = fake_extractor(frames=[dict(FRAME_TCS)], reachable=False)
    stats = sleep_phase.ingest_journal(conn, JOURNAL_ENTRIES, extractor=ex,
                                       today=TODAY.isoformat())
    assert stats["failed"] == 0
    assert stats["skipped_no_llm"] == 2   # the 3rd fixture row has no text
    assert conn.execute("SELECT COUNT(*) AS n FROM ingest_log").fetchone()["n"] == 0
    ex.extract_event_json.assert_not_called()   # zero doomed calls
    conn.close()


def test_ingest_failure_is_retryable_not_logged():
    conn = fresh_conn()
    ex = fake_extractor(frames=[])  # extractor always returns None (Ollama down)
    stats = sleep_phase.ingest_journal(conn, JOURNAL_ENTRIES[:1], extractor=ex,
                                       today=TODAY.isoformat())
    assert stats["failed"] == 1
    # NOT hash-logged, so the next run (Ollama back up) retries it:
    assert conn.execute("SELECT COUNT(*) AS n FROM ingest_log").fetchone()["n"] == 0
    conn.close()


# ------------------------------------------------------ B. consolidation

def _seed_events(conn, n=3, day=TODAY):
    ids = []
    for i in range(n):
        ids.append(brain_map.record_event(
            conn, day.isoformat(), f"T{i}.NS", "journal", f"tag_{i}",
            sentiment="positive", source="local_parser"))
    return ids


def test_consolidation_creates_semantic_node_and_graph_links():
    conn = fresh_conn()
    ids = _seed_events(conn, 3)
    clusters = {"clusters": [{"tag": "IT Sector Strength",
                              "summary": "Broad IT momentum on earnings.",
                              "sentiment": 1, "members": [1, 3]}]}
    stats = sleep_phase.consolidate_recent(conn, extractor=fake_extractor(clusters=clusters),
                                           today=TODAY)
    assert stats["clusters_created"] == 1 and stats["links_added"] == 2

    node = conn.execute("SELECT * FROM semantic_nodes").fetchone()
    assert node["tag"] == "it_sector_strength"       # normalized
    assert node["confidence_score"] == 1.0 and node["active"] == 1
    linked = {r["event_id"] for r in conn.execute(
        "SELECT event_id FROM semantic_event_link WHERE semantic_id = ?",
        (node["id"],))}
    assert linked == {ids[0], ids[2]}                # members 1 and 3
    conn.close()


def test_consolidation_reinforces_existing_theme_instead_of_duplicating():
    conn = fresh_conn()
    _seed_events(conn, 2)
    clusters = {"clusters": [{"tag": "it_sector_strength", "summary": "s",
                              "sentiment": 1, "members": [1, 2]}]}
    sleep_phase.consolidate_recent(conn, extractor=fake_extractor(clusters=clusters),
                                   today=TODAY)
    # Decay it down and flag it out, then see the theme again:
    conn.execute("UPDATE semantic_nodes SET confidence_score = 0.1, active = 0")
    conn.commit()
    stats = sleep_phase.consolidate_recent(conn, extractor=fake_extractor(clusters=clusters),
                                           today=TODAY)
    assert stats["clusters_reinforced"] == 1 and stats["clusters_created"] == 0
    node = conn.execute("SELECT * FROM semantic_nodes").fetchone()
    assert node["confidence_score"] == 1.0 and node["active"] == 1  # reactivated
    assert conn.execute("SELECT COUNT(*) AS n FROM semantic_nodes").fetchone()["n"] == 1
    conn.close()


def test_consolidation_survives_llm_garbage_and_bad_members():
    conn = fresh_conn()
    _seed_events(conn, 2)
    # LLM down -> nothing happens, nothing raises:
    stats = sleep_phase.consolidate_recent(conn, extractor=fake_extractor(clusters=None),
                                           today=TODAY)
    assert stats["clusters_created"] == 0
    # Out-of-range members, singleton clusters, and missing tags are all dropped:
    bad = {"clusters": [{"tag": "x", "members": [99, -1]},
                        {"tag": "singleton", "members": [1]},
                        {"tag": "", "members": [1, 2]},
                        "not-a-dict"]}
    stats = sleep_phase.consolidate_recent(conn, extractor=fake_extractor(clusters=bad),
                                           today=TODAY)
    assert stats["clusters_created"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM semantic_nodes").fetchone()["n"] == 0
    conn.close()


def test_consolidation_skips_when_fewer_than_two_recent_events():
    conn = fresh_conn()
    _seed_events(conn, 1)
    ex = fake_extractor(clusters={"clusters": []})
    stats = sleep_phase.consolidate_recent(conn, extractor=ex, today=TODAY)
    assert stats["events_considered"] == 1 and stats["clusters_created"] == 0
    ex.chat_json.assert_not_called()  # no pointless LLM call
    conn.close()


# --------------------------------------------------------------- C. decay

def _seed_node(conn, tag, score, last_reinforced, last_decayed=None):
    conn.execute(
        "INSERT INTO semantic_nodes (tag, summary, confidence_score, "
        "created_at, last_reinforced, last_decayed) VALUES (?, '', ?, ?, ?, ?)",
        (tag, score, last_reinforced, last_reinforced, last_decayed))
    conn.commit()


def test_decay_math_over_multi_day_gaps():
    conn = fresh_conn()
    _seed_node(conn, "ten_days", 1.0, "2026-06-26")    # dt = 10
    _seed_node(conn, "forty_days", 1.0, "2026-05-27")  # dt = 40
    _seed_node(conn, "fresh", 1.0, TODAY.isoformat())  # dt = 0

    stats = sleep_phase.apply_decay(conn, decay_lambda=0.05,
                                    prune_threshold=0.20, today=TODAY)
    assert stats == {"decayed": 2, "flagged_inactive": 1, "unchanged": 1}

    rows = {r["tag"]: r for r in conn.execute("SELECT * FROM semantic_nodes")}
    # Score_new = Score_current * e^(-lambda * dt):
    assert abs(rows["ten_days"]["confidence_score"] - math.exp(-0.5)) < 1e-6
    assert rows["ten_days"]["active"] == 1             # 0.6065 stays active
    assert abs(rows["forty_days"]["confidence_score"] - math.exp(-2.0)) < 1e-6
    assert rows["forty_days"]["active"] == 0           # 0.1353 < 0.20 -> flagged
    assert rows["fresh"]["confidence_score"] == 1.0    # nothing to decay yet
    conn.close()


def test_decay_never_double_counts_the_same_days():
    conn = fresh_conn()
    _seed_node(conn, "theme", 1.0, "2026-06-26")       # dt = 10
    sleep_phase.apply_decay(conn, decay_lambda=0.05, prune_threshold=0.20,
                            today=TODAY)
    first = conn.execute("SELECT confidence_score FROM semantic_nodes").fetchone()[0]
    # Second run the SAME day: anchor moved to last_decayed, dt=0 -> no change:
    stats = sleep_phase.apply_decay(conn, decay_lambda=0.05, prune_threshold=0.20,
                                    today=TODAY)
    assert stats["decayed"] == 0 and stats["unchanged"] == 1
    again = conn.execute("SELECT confidence_score FROM semantic_nodes").fetchone()[0]
    assert again == first
    # Five days later: decays exactly e^(-0.05*5) further — total e^(-0.75):
    sleep_phase.apply_decay(conn, decay_lambda=0.05, prune_threshold=0.20,
                            today=date(2026, 7, 11))
    final = conn.execute("SELECT confidence_score FROM semantic_nodes").fetchone()[0]
    assert abs(final - math.exp(-0.75)) < 1e-6
    conn.close()


# ------------------------------------------------- config & decoupling

def test_settings_fall_back_when_config_missing_or_bare():
    s = sleep_phase.load_settings(config_path="/nonexistent/config.json")
    assert s == {"decay_lambda": 0.05, "prune_threshold": 0.20,
                 "consolidation_hours": 24, "causal_window_days": 30}
    # The real config.json (no sleep_* keys yet) also yields the defaults:
    s2 = sleep_phase.load_settings()
    assert s2["decay_lambda"] == 0.05 and s2["prune_threshold"] == 0.20


def test_no_market_data_or_trading_imports():
    """Decision #30 decoupling: strictly an offline DB + text job."""
    source = Path(sleep_phase.__file__).read_text()
    import_lines = [l.strip() for l in source.splitlines()
                    if l.strip().startswith(("import ", "from "))]
    for line in import_lines:
        assert "dhan" not in line, f"market-data import found: {line}"
        assert "portfolio" not in line and "strategy" not in line, line
        assert "trade" not in line and "notifier" not in line, line
        assert "data_fetcher" not in line and "rules" not in line, line


# --------------------------------------------------------------- runner

def test_full_run_is_fail_safe_end_to_end():
    ex = fake_extractor(frames=[dict(FRAME_TCS), dict(FRAME_INFY)],
                        clusters={"clusters": [{"tag": "it_strength",
                                                "summary": "s", "sentiment": 1,
                                                "members": [1, 2]}]})
    with mock.patch.object(brain_map, "_read_journal_file",
                           return_value=JOURNAL_ENTRIES):
        results = sleep_phase.run_sleep_phase(db_path=":memory:", extractor=ex,
                                              today=TODAY)
    assert results["ingestion"]["ingested"] == 2
    assert results["consolidation"]["clusters_created"] == 1
    assert results["decay"]["decayed"] == 0  # everything created today


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}  {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

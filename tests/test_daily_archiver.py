"""
Tests for the daily perishables archiver (Phase 0). Fully offline.

Run either of these from the project folder:
    python tests/test_daily_archiver.py
    python -m pytest tests/test_daily_archiver.py
"""

import json
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import lake
from src.ingestion import daily_archiver as da


def test_news_snapshot_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        news = tmp / "news_sentiment.json"
        payload = {"generated": "2026-07-10T19:00:00+00:00", "source": "gemini",
                   "tickers": {"TCS.NS": {"sentiment_score": 3,
                                          "headline_focus": "strong quarter",
                                          "stale": False}}}
        news.write_text(json.dumps(payload))
        # Archive-time clock pinned 30 min after generation (the normal
        # 19:10-news/19:45-archive cadence).
        assert da.archive_news(date(2026, 7, 10), news_path=news,
                               lake_root=tmp / "lake",
                               now=datetime(2026, 7, 10, 19, 30,
                                            tzinfo=timezone.utc)) is True
        rows = lake.read_day("news_daily", "2026-07-10", root=tmp / "lake")
        assert rows == [payload]


def test_stale_news_file_gets_a_hole_not_a_duplicate():
    """A file NOT regenerated since the last cycle must not be re-archived
    under a new date= key (the 2026-07-05→16 fabricated-history bug)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        news = tmp / "news_sentiment.json"
        payload = {"generated": "2026-07-05T12:02:44+00:00", "source": "gemini",
                   "tickers": {"TCS.NS": {"sentiment_score": -5,
                                          "headline_focus": "old read",
                                          "stale": False}}}
        news.write_text(json.dumps(payload))
        assert da.archive_news(date(2026, 7, 11), news_path=news,
                               lake_root=tmp / "lake",
                               now=datetime(2026, 7, 11, 14, 15,
                                            tzinfo=timezone.utc)) is False
        assert lake.read_day("news_daily", "2026-07-11", root=tmp / "lake") == []
        # No `generated` stamp at all (pre-stamp format) — fail-open, archive.
        news.write_text(json.dumps({"source": "gemini", "tickers": {}}))
        assert da.archive_news(date(2026, 7, 11), news_path=news,
                               lake_root=tmp / "lake") is True


def test_missing_or_broken_news_skips_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        assert da.archive_news(date(2026, 7, 10), news_path=tmp / "ghost.json",
                               lake_root=tmp / "lake") is False
        broken = tmp / "broken.json"
        broken.write_text("{not json")
        assert da.archive_news(date(2026, 7, 10), news_path=broken,
                               lake_root=tmp / "lake") is False
        assert lake.read_day("news_daily", "2026-07-10", root=tmp / "lake") == []


def test_macro_snapshot_with_injected_matrix():
    with tempfile.TemporaryDirectory() as tmp:
        matrix = {"as_of": "2026-07-10", "source": "snapshot",
                  "metrics": {"CRUDE": {"horizons": {"SHORT": "rising"}}}}
        assert da.archive_macro(date(2026, 7, 10), matrix=matrix,
                                lake_root=tmp) is True
        rows = lake.read_day("macro_daily", "2026-07-10", root=tmp)
        assert rows[0]["metrics"]["CRUDE"]["horizons"]["SHORT"] == "rising"


def test_run_is_independent_per_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # News missing, macro injected -> macro still archives.
        results = da.run(today=date(2026, 7, 10), news_path=tmp / "ghost.json",
                         macro_matrix={"as_of": "2026-07-10", "source": "none",
                                       "metrics": {}},
                         lake_root=tmp / "lake")
        assert results == {"news": False, "macro": True}
        assert lake.read_day("macro_daily", "2026-07-10", root=tmp / "lake")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

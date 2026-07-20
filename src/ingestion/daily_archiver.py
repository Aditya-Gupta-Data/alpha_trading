"""
src/ingestion/daily_archiver.py — archive the perishable daily artifacts
========================================================================

Phase 0 of docs/HOLY_GRAIL_PLAN.md. Two of the engine's most valuable
signal artifacts are OVERWRITTEN every day and therefore have no history:

  * data/news_sentiment.json   (Gemini sentiment — rewritten per run)
  * the macro directional matrix (computed on demand, never persisted)

The cross-layer join surface (daily_context, Phase 2) and every future
miner need their day-by-day history; without this archiver those columns
stay forever thin. One tiny nightly job fixes it: snapshot each artifact
into the lake as one row per day.

    data/lake/news_daily/date=YYYY-MM-DD/part.jsonl.gz    (the sentiment JSON)
    data/lake/macro_daily/date=YYYY-MM-DD/part.jsonl.gz   (the macro matrix)

Fail-open per artifact: a missing news file, a Dhan-less macro build, an
unwritable lake — each is logged and skipped; the others still archive.
Never raises, never touches trade state. Cron: daily 19:45 IST (after the
19:30 deals pull; before the 20:00 sleep phase).

Manual check:  python3 -m src.ingestion.daily_archiver
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import lake

ROOT = Path(__file__).resolve().parent.parent.parent
NEWS_PATH = ROOT / "data" / "news_sentiment.json"

# A news file older than this at archive time was NOT regenerated today —
# re-copying it would mint a partition whose date= key lies about its
# content (2026-07-05→16: five partitions all held the same July-5 read
# because news_processor was unscheduled while this job kept copying).
# A missing partition is honest history ("no read that day"); a re-copy
# is fabricated history. 24h passes the normal cadence (news 19:10 IST,
# archive 19:45 IST — 35 min) and fails the first missed refresh.
NEWS_SNAPSHOT_MAX_AGE_HOURS = 24


def archive_news(today: date, news_path=None, lake_root=None,
                 now: datetime = None) -> bool:
    """Snapshot data/news_sentiment.json into the lake. False (logged) when
    the file is absent/unreadable — a fallback-neutral day still archives
    (stale=true rows are honest history: 'the engine had no read that day').
    Also False when the file's `generated` stamp is older than
    NEWS_SNAPSHOT_MAX_AGE_HOURS: the day gets a hole, not a duplicate.
    `now` is injectable for tests; default is the real clock. A payload
    with no parseable `generated` archives as before (fail-open — old
    formats shouldn't lose their history)."""
    path = Path(news_path) if news_path is not None else NEWS_PATH
    if not path.exists():
        print("  (daily archiver: no news_sentiment.json — skipped)")
        return False
    try:
        payload = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        print(f"  (daily archiver: unreadable news file [{exc}] — skipped)")
        return False
    generated = payload.get("generated") if isinstance(payload, dict) else None
    if generated:
        try:
            written_at = datetime.fromisoformat(str(generated))
            if written_at.tzinfo is None:
                written_at = written_at.replace(tzinfo=timezone.utc)
            age = (now or datetime.now(timezone.utc)) - written_at
            if age > timedelta(hours=NEWS_SNAPSHOT_MAX_AGE_HOURS):
                print(f"  (daily archiver: news file generated {generated} — "
                      f"{age.days}d{age.seconds // 3600}h old, not today's "
                      f"read; skipped so the lake gets an honest hole, not "
                      f"a duplicate)")
                return False
        except ValueError:
            pass  # unparseable stamp — archive as before
    written = lake.write_partition("news_daily", today.isoformat(),
                                   [payload], root=lake_root)
    return written is not None


def archive_macro(today: date, matrix: dict = None, lake_root=None) -> bool:
    """Snapshot the macro directional matrix into the lake. `matrix` is
    injectable for tests; by default it's built live (build_macro_matrix is
    itself fail-open — offline it degrades to snapshot/'none' sources,
    which is still an honest record of what the engine believed that day)."""
    if matrix is None:
        try:
            from src.ingestion.macro_tracker import build_macro_matrix
            matrix = build_macro_matrix(today=today)
        except Exception as exc:
            print(f"  (daily archiver: macro matrix build failed [{exc}])")
            return False
    if not isinstance(matrix, dict):
        return False
    written = lake.write_partition("macro_daily", today.isoformat(),
                                   [matrix], root=lake_root)
    return written is not None


def run(today: date = None, news_path=None, macro_matrix: dict = None,
        lake_root=None) -> dict:
    """Archive every perishable. Returns {artifact: bool} — the printed
    summary is this job's heartbeat line."""
    today = today or date.today()
    results = {
        "news": archive_news(today, news_path=news_path, lake_root=lake_root),
        "macro": archive_macro(today, matrix=macro_matrix,
                               lake_root=lake_root),
    }
    print(f"(daily archiver: {today.isoformat()} — "
          + ", ".join(f"{k}={'ok' if v else 'skip'}"
                      for k, v in results.items()) + ")")
    return results


if __name__ == "__main__":
    run()

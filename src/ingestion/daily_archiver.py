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
from datetime import date
from pathlib import Path

from src import lake

ROOT = Path(__file__).resolve().parent.parent.parent
NEWS_PATH = ROOT / "data" / "news_sentiment.json"


def archive_news(today: date, news_path=None, lake_root=None) -> bool:
    """Snapshot data/news_sentiment.json into the lake. False (logged) when
    the file is absent/unreadable — a fallback-neutral day still archives
    (stale=true rows are honest history: 'the engine had no read that day')."""
    path = Path(news_path) if news_path is not None else NEWS_PATH
    if not path.exists():
        print("  (daily archiver: no news_sentiment.json — skipped)")
        return False
    try:
        payload = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        print(f"  (daily archiver: unreadable news file [{exc}] — skipped)")
        return False
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

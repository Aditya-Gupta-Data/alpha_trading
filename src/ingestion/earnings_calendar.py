"""
src/ingestion/earnings_calendar.py — deterministic results-date calendar
========================================================================

Phase 1 of docs/HOLY_GRAIL_PLAN.md. Results scheduling is the metronome of
Indian single-stock volatility, and `days_to_results` is exactly the kind
of entry-filter-ORTHOGONAL feature decision #50's post-mortem said the
learning stack needs — deterministic, no LLM, cheap. It also reframes the
smart-money layer: an entity distributing three days before results is a
different fact than distributing into silence.

NSE's corporate event calendar lists board-meeting purposes per symbol;
this module keeps only the results-shaped ones. Postponements are handled
by construction: every run overwrites the whole calendar with NSE's
current dates (latest fetch wins), and the lake keeps each day's copy so
date drift is visible history.

Same discipline as every sibling: live fetch → hand-editable snapshot →
"none"; advisory-only; fail-open; never a trade path.

Artifacts:
  * data/earnings_calendar.json              {as_of, source, events:{TICKER: date}}
  * data/lake/earnings/date=YYYY-MM-DD/      per-run history
  * data/earnings_snapshot.json              hand-editable fallback

Cron: daily 19:20 IST. Manual check: python3 -m src.ingestion.earnings_calendar
"""

import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from src import lake
from src.ingestion.deals_tracker import (_NSE_HEADERS, _NSE_HOME, HTTP_TIMEOUT,
                                         _nse_opener, normalize_symbol,
                                         parse_report_date)

ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_PATH = ROOT / "data" / "earnings_calendar.json"
SNAPSHOT_PATH = ROOT / "data" / "earnings_snapshot.json"

_NSE_EVENTS_API = "https://www.nseindia.com/api/event-calendar"

# A board meeting counts as "results" when its stated purpose says so.
_RESULTS_MARKERS = ("financial result", "financial results", "results",
                    "quarterly result", "audited result", "unaudited result")


def _is_results_purpose(purpose) -> bool:
    p = str(purpose or "").lower()
    return any(marker in p for marker in _RESULTS_MARKERS)


def normalize_calendar(rows: list) -> dict:
    """NSE event rows -> {TICKER: next results date (ISO)}. When a symbol
    carries several results dates, the EARLIEST upcoming one wins (the next
    scheduled meeting is the volatility event). Junk rows are dropped."""
    events = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if not _is_results_purpose(row.get("purpose") or row.get("bm_desc")):
            continue
        ticker = normalize_symbol(row.get("symbol"))
        day = parse_report_date(row.get("date") or row.get("bm_date"))
        if not ticker or not day:
            continue
        if ticker not in events or day < events[ticker]:
            events[ticker] = day
    return events


def _fetch_nse_calendar(timeout: int = HTTP_TIMEOUT):
    """Live path -> (rows, raw_bytes) or None. Never raises."""
    opener = _nse_opener()
    try:
        opener.open(urllib.request.Request(_NSE_HOME, headers=_NSE_HEADERS),
                    timeout=timeout).read()
        req = urllib.request.Request(_NSE_EVENTS_API, headers=_NSE_HEADERS)
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError, TimeoutError) as exc:
        print(f"  (earnings calendar: NSE fetch failed [{exc}] — "
              "falling open to the local snapshot)")
        return None
    return (payload, raw) if isinstance(payload, list) else None


def _load_snapshot(path=None) -> list:
    path = Path(path) if path is not None else SNAPSHOT_PATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        print(f"  (earnings calendar: unreadable snapshot {path})")
        return []
    rows = raw.get("rows") if isinstance(raw, dict) else raw
    return rows if isinstance(rows, list) else []


def run(output_path=None, snapshot_path=None, lake_root=None,
        today: date = None, use_live: bool = True) -> dict:
    """Fetch, filter to results events, persist. Whole-calendar overwrite
    per run = postponements self-heal. Never raises."""
    today = today or date.today()
    rows, raw, source = None, None, "none"
    if use_live:
        fetched = _fetch_nse_calendar()
        if fetched is not None:
            rows, raw = fetched
            source = "nse"
    if rows is None:
        rows = _load_snapshot(snapshot_path)
        source = "snapshot" if rows else "none"

    calendar = {
        "as_of": today.isoformat(),
        "source": source if rows else "none",
        "events": normalize_calendar(rows),
    }
    out = Path(output_path) if output_path is not None else OUTPUT_PATH
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(calendar, indent=2))
    except OSError as exc:
        print(f"  (earnings calendar: could not write {out} [{exc}])")
    if calendar["events"]:
        lake.write_partition("earnings", today.isoformat(), [calendar],
                             root=lake_root)
        if raw:
            lake.archive_blob("earnings_raw", today.isoformat(), "events",
                              raw, ext="json", root=lake_root)
    print(f"(earnings calendar: {today.isoformat()} [{calendar['source']}] — "
          f"{len(calendar['events'])} results date(s))")
    return calendar


def load_calendar(path=None) -> dict:
    """{TICKER: ISO date} from the persisted calendar, {} on any problem."""
    path = Path(path) if path is not None else OUTPUT_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    events = raw.get("events") if isinstance(raw, dict) else None
    return events if isinstance(events, dict) else {}


def days_to_results(ticker, today: date = None, calendar: dict = None):
    """Trading-agnostic calendar-day distance to the ticker's next known
    results date: 0 = results day, positive = upcoming, None = unknown or
    already past (an absent reading, never a guess). The capture-at-
    proposal feature (#50 pattern) consumers stamp onto entries."""
    if calendar is None:
        calendar = load_calendar()
    today = today or date.today()
    ticker = str(ticker or "").strip().upper()
    day = calendar.get(ticker)
    if not day:
        return None
    try:
        delta = (date.fromisoformat(day) - today).days
    except ValueError:
        return None
    return delta if delta >= 0 else None


if __name__ == "__main__":
    run()

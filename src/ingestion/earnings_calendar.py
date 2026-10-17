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
  * data/earnings_calendar.json              {as_of, source, events:{TICKER: next date},
                                              past:{TICKER: past date seen in this fetch},
                                              last:{TICKER: most recent held results}}
  * data/lake/earnings/date=YYYY-MM-DD/      per-run history (the source of `last`)
  * data/earnings_snapshot.json              hand-editable fallback
  * config/index_heavyweights.json           index -> the heavyweights whose results move it

Earnings Awareness S1 (docs/earnings_awareness_spec.md): `last` is rebuilt from
the lake every run, so "results were N days ago" survives the ticker dropping
out of NSE's forward calendar.

Cron: daily 19:20 IST. Manual check: python3 -m src.ingestion.earnings_calendar
"""

import json
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from src import lake
from src.ingestion.deals_tracker import (_NSE_HEADERS, _NSE_HOME, HTTP_TIMEOUT,
                                         _nse_opener, normalize_symbol,
                                         parse_report_date)

ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_PATH = ROOT / "data" / "earnings_calendar.json"
SNAPSHOT_PATH = ROOT / "data" / "earnings_snapshot.json"
HEAVYWEIGHTS_PATH = ROOT / "config" / "index_heavyweights.json"
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_NSE_EVENTS_API = "https://www.nseindia.com/api/event-calendar"

# A board meeting counts as "results" when its stated purpose says so.
_RESULTS_MARKERS = ("financial result", "financial results", "results",
                    "quarterly result", "audited result", "unaudited result")


def _is_results_purpose(purpose) -> bool:
    p = str(purpose or "").lower()
    return any(marker in p for marker in _RESULTS_MARKERS)


def _results_dates(rows: list) -> dict:
    """NSE event rows -> {TICKER: set of results dates (ISO)}; junk rows dropped."""
    dates = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if not _is_results_purpose(row.get("purpose") or row.get("bm_desc")):
            continue
        ticker = normalize_symbol(row.get("symbol"))
        day = parse_report_date(row.get("date") or row.get("bm_date"))
        if ticker and day:
            dates.setdefault(ticker, set()).add(day)
    return dates


def normalize_calendar(rows: list, today: date = None) -> dict:
    """NSE event rows -> {TICKER: next results date (ISO)}: the EARLIEST date wins,
    and with `today` only dates on or after it count (a past one never masks the next)."""
    floor = today.isoformat() if today else ""
    events = {}
    for ticker, days in _results_dates(rows).items():
        upcoming = sorted(d for d in days if d >= floor)
        if upcoming:
            events[ticker] = upcoming[0]
    return events


def past_results(rows: list, today: date) -> dict:
    """NSE event rows -> {TICKER: latest results date before `today`} (held meetings NSE still lists)."""
    floor = today.isoformat()
    past = {}
    for ticker, days in _results_dates(rows).items():
        held = sorted(d for d in days if d < floor)
        if held:
            past[ticker] = held[-1]
    return past


# Per-endpoint Referer (ledger 2026-07-12, same 403 class as flows): the
# event-calendar API expects its own page as Referer, not the deals page.
_EVENTS_HEADERS = dict(
    _NSE_HEADERS,
    Referer="https://www.nseindia.com/companies-listing/corporate-filings-event-calendar")


def _fetch_nse_calendar(timeout: int = HTTP_TIMEOUT):
    """Live path -> (rows, raw_bytes) or None. Never raises. Warm-up
    failure tolerated separately (deals_tracker discipline)."""
    opener = _nse_opener()
    try:
        opener.open(urllib.request.Request(_NSE_HOME, headers=_EVENTS_HEADERS),
                    timeout=timeout).read()
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        pass  # cookies may already exist / API may still answer
    try:
        req = urllib.request.Request(_NSE_EVENTS_API, headers=_EVENTS_HEADERS)
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
        "events": normalize_calendar(rows, today=today),
        "past": past_results(rows, today),
    }
    if calendar["events"]:
        lake.write_partition("earnings", today.isoformat(), [calendar],
                             root=lake_root)
        if raw:
            lake.archive_blob("earnings_raw", today.isoformat(), "events",
                              raw, ext="json", root=lake_root)
    calendar["last"] = rebuild_last(lake_root, today, extra=calendar["past"])
    out = Path(output_path) if output_path is not None else OUTPUT_PATH
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(calendar, indent=2))
    except OSError as exc:
        print(f"  (earnings calendar: could not write {out} [{exc}])")
    print(f"(earnings calendar: {today.isoformat()} [{calendar['source']}] — "
          f"{len(calendar['events'])} results date(s), "
          f"{len(calendar['last'])} past results remembered)")
    return calendar


def rebuild_last(lake_root=None, today: date = None, extra: dict = None) -> dict:
    """{TICKER: most recent results date on or before `today`} from the lake's daily copies.
    A date counts only if the latest copy taken BEFORE it still listed it (a postponed
    date never counts); a past date NSE itself still lists counts as held."""
    today_iso = (today or date.today()).isoformat()
    seen, held = {}, {}
    for day, row in lake.scan("earnings", end=today_iso, root=lake_root):
        if not isinstance(row, dict):
            continue
        for ticker, d in (row.get("events") or {}).items():
            if isinstance(d, str) and _ISO_RE.match(d):
                seen.setdefault(ticker, []).append((day, d))
        for ticker, d in (row.get("past") or {}).items():
            if isinstance(d, str) and _ISO_RE.match(d) and d <= today_iso:
                held[ticker] = max(held.get(ticker, d), d)
    for ticker, d in (extra or {}).items():
        if isinstance(d, str) and _ISO_RE.match(d) and d <= today_iso:
            held[ticker] = max(held.get(ticker, d), d)
    for ticker, obs in seen.items():
        for d in {d for _, d in obs if d <= today_iso}:
            before = [o for o in obs if o[0] < d]
            confirmed = (before[-1][1] == d if before
                         else any(o == (d, d) for o in obs))
            if confirmed and d > held.get(ticker, ""):
                held[ticker] = d
    return dict(sorted(held.items()))


def load_doc(path=None) -> dict:
    """The whole persisted calendar {as_of, source, events, past, last}, {} on any problem."""
    path = Path(path) if path is not None else OUTPUT_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_calendar(path=None) -> dict:
    """{TICKER: ISO date} from the persisted calendar, {} on any problem."""
    events = load_doc(path).get("events")
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


def _lookup(mapping, ticker):
    t = str(ticker or "").strip().upper()
    mapping = mapping if isinstance(mapping, dict) else {}
    if t in mapping:
        return mapping[t]
    return mapping.get(t + ".NS") if t and "." not in t else None


def _as_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def next_results(ticker, today: date = None, doc: dict = None):
    """The ticker's next announced results date (ISO) on or after `today`, else None."""
    doc = load_doc() if doc is None else doc
    floor = (today or date.today()).isoformat()
    d = _lookup(doc.get("events"), ticker)
    return d if isinstance(d, str) and _ISO_RE.match(d) and d >= floor else None


def last_results(ticker, today: date = None, doc: dict = None):
    """The ticker's most recent results date strictly before `today` (ISO), else None."""
    doc = load_doc() if doc is None else doc
    floor = (today or date.today()).isoformat()
    found = [d for d in (_lookup(doc.get(k), ticker) for k in ("last", "past", "events"))
             if isinstance(d, str) and _ISO_RE.match(d) and d < floor]
    return max(found) if found else None


def days_since_results(ticker, today: date = None, doc: dict = None):
    """Calendar days since the ticker's most recent results: 1 = yesterday, None = unknown."""
    today = today or date.today()
    d = last_results(ticker, today, doc)
    return (today - date.fromisoformat(d)).days if d else None


def stale_reason(doc: dict, today: date = None, max_age_days: int = None):
    """Why the calendar cannot speak for `today` (missing, empty, stale, from the future), or None."""
    if max_age_days is None:
        from src.config import EARNINGS_CALENDAR_MAX_AGE_DAYS as max_age_days
    today = today or date.today()
    if not isinstance(doc, dict) or not doc:
        return "earnings calendar missing"
    as_of = _as_date(doc.get("as_of"))
    if as_of is None:
        return "earnings calendar has no as_of date"
    age = (today - as_of).days
    if age < 0:
        return f"earnings calendar as_of {as_of} is after {today}"
    if age > max_age_days:
        return f"earnings calendar stale: as_of {as_of} is {age} days old (max {max_age_days})"
    if doc.get("source") in (None, "none") or not doc.get("events"):
        return f"earnings calendar empty (source {doc.get('source')}, as_of {as_of})"
    if not isinstance(doc.get("last"), dict):
        return "earnings calendar has no past-results memory yet (filled by the next 19:20 run)"
    return None


def results_context(ticker, today: date = None, doc: dict = None) -> dict:
    """{next_results, days_to_results, last_results, days_since_results, source_as_of, reason}
    for one ticker; every date is None, with the reason, when the calendar cannot speak."""
    today = today or date.today()
    doc = load_doc() if doc is None else doc
    out = {"next_results": None, "days_to_results": None, "last_results": None,
           "days_since_results": None, "source_as_of": (doc or {}).get("as_of"),
           "reason": stale_reason(doc, today)}
    if out["reason"]:
        return out
    nxt, last = next_results(ticker, today, doc), last_results(ticker, today, doc)
    out.update(next_results=nxt, last_results=last,
               days_to_results=(date.fromisoformat(nxt) - today).days if nxt else None,
               days_since_results=(today - date.fromisoformat(last)).days if last else None)
    return out


def _quarter_ends(year: int) -> list:
    return [date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)]


def _quarter_end_on_or_after(d: date) -> date:
    return min(q for q in _quarter_ends(d.year) + _quarter_ends(d.year + 1) if q >= d)


def _quarter_end_before(d: date) -> date:
    return max(q for q in _quarter_ends(d.year - 1) + _quarter_ends(d.year) if q < d)


def earliest_unannounced_results(ticker, today: date = None, doc: dict = None):
    """The first day an UNANNOUNCED next results date could fall on (ISO): the day after the
    next quarter end when the last ended quarter is already reported, else None (any day)."""
    today = today or date.today()
    last = last_results(ticker, today, doc)
    if not last or last <= _quarter_end_before(today).isoformat():
        return None
    return (_quarter_end_on_or_after(today) + timedelta(days=1)).isoformat()


def load_heavyweights(path=None) -> dict:
    """{INDEX: [TICKER, ...]} from config/index_heavyweights.json, {} on any problem."""
    path = Path(path) if path is not None else HEAVYWEIGHTS_PATH
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    indices = raw.get("indices") if isinstance(raw, dict) else None
    if not isinstance(indices, dict):
        return {}
    return {str(k).strip().upper(): [str(t).strip().upper() for t in v]
            for k, v in indices.items() if isinstance(v, list)}


def index_constituent_results(index, start, end, doc: dict = None,
                              heavyweights: dict = None):
    """The index's heavyweights with a results date in [start, end], as [{ticker, results_date}]
    in date order; None when the map has no names for the index."""
    heavy = load_heavyweights() if heavyweights is None else heavyweights
    names = heavy.get(str(index or "").strip().upper())
    start_d, end_d = _as_date(start), _as_date(end)
    if not names or start_d is None or end_d is None:
        return None
    doc = load_doc() if doc is None else doc
    lo, hi = start_d.isoformat(), end_d.isoformat()
    hits = set()
    for t in names:
        for key in ("events", "past", "last"):
            d = _lookup(doc.get(key), t)
            if isinstance(d, str) and _ISO_RE.match(d) and lo <= d <= hi:
                hits.add((d, t))
    return [{"ticker": t, "results_date": d} for d, t in sorted(hits)]


if __name__ == "__main__":
    run()

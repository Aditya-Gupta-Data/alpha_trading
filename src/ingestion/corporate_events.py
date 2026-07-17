"""
src/ingestion/corporate_events.py — NSE corporate-announcements vacuum
======================================================================

Data-lake capture of NSE corporate filings/announcements, keyword-flagged for
the event-driven layer (Phase 3 gating): CATALYST (order win / tender /
contract), EXPANSION (merger / acquisition), STRUCTURAL_RISK (demerger /
scheme), LEGAL_RISK (SEBI / subpoena / fraud / NCLT / default / auditor exit).

Same ingestion idioms as deals_tracker/flows_tracker: NSE cookie handshake +
own Referer, fail-open, injectable fetch seam, NULL-honest (a row that parses
to nothing is skipped, never guessed). Advisory/capture-only, NOT wired into
the engine. Stores one partition per announcement day at data/lake/events/.

Endpoint: /api/corporate-announcements (equities). It serves recent
announcements and accepts from_date/to_date (dd-mm-yyyy) windows.

CLI:  python3 -m src.ingestion.corporate_events [--from dd-mm-yyyy --to dd-mm-yyyy]
"""
import json
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from src import lake
from src.ingestion.deals_tracker import (_NSE_HEADERS, _NSE_HOME, HTTP_TIMEOUT,
                                         _nse_opener, parse_report_date)

_ANN_API = "https://www.nseindia.com/api/corporate-announcements?index=equities"
_HEADERS = dict(_NSE_HEADERS,
                Referer="https://www.nseindia.com/companies-listing/"
                        "corporate-filings-announcements")

# keyword -> event class. Substrings, case-insensitive; matched against the
# announcement subject + attachment text.
CATEGORIES = {
    "CATALYST": ["order win", "bags order", "bagged", "secures order",
                 "order worth", "work order", "letter of award", " loa ",
                 "tender", "contract", "awarded", "wins order", "new order",
                 "purchase order", "supply order"],
    "EXPANSION": ["merger", "amalgamation", "acquisition", "acquire",
                  "acquires", "stake acquisition", "joint venture"],
    "STRUCTURAL_RISK": ["demerger", "scheme of arrangement", "spin-off",
                        "spin off", "hive off", "slump sale"],
    "LEGAL_RISK": ["subpoena", "litigation", "sebi order", "sebi penalty",
                   "penalty", "fraud", "investigation", "insolvency", "nclt",
                   "auditor resign", "resignation of auditor", "default",
                   "search and seizure", "income tax raid", "gst demand"],
}


def classify(text: str) -> list:
    """Event classes whose keywords appear in `text` (may be multiple)."""
    t = f" {(text or '').lower()} "
    return [cat for cat, kws in CATEGORIES.items()
            if any(kw in t for kw in kws)]


def _first(row: dict, *keys):
    for k in keys:
        v = row.get(k)
        if v:
            return v
    return None


def normalize(row: dict) -> dict | None:
    """One NSE announcement row -> a normalized, flagged event, or None when
    it has neither a symbol nor a date (nothing to anchor on)."""
    if not isinstance(row, dict):
        return None
    symbol = _first(row, "symbol", "sm_symbol", " symbol")
    subject = _first(row, "desc", "attchmntText", "sm_name", "smIndustry") or ""
    detail = _first(row, "attchmntText", "desc") or ""
    raw_dt = _first(row, "an_dt", "sort_date", "exchdisstime", "date")
    as_of = None
    if raw_dt:
        try:
            as_of = str(raw_dt)[:10]
            if "-" in as_of and as_of[2] == "-":      # dd-mm-yyyy
                as_of = parse_report_date(raw_dt) or datetime.strptime(
                    str(raw_dt)[:10], "%d-%m-%Y").date().isoformat()
            else:
                as_of = str(raw_dt)[:10]
        except Exception:
            as_of = None
    if not symbol and not as_of:
        return None
    flags = classify(f"{subject} {detail}")
    return {"as_of": as_of, "symbol": symbol,
            "ticker": f"{symbol}.NS" if symbol else None,
            "subject": subject[:400], "flags": flags,
            "attachment": _first(row, "attchmntFile"),
            "captured_at": datetime.now().isoformat(timespec="seconds")}


def fetch(from_date: date = None, to_date: date = None,
          opener=None, timeout: int = HTTP_TIMEOUT, fetch_fn=None):
    """Live NSE pull -> list of raw announcement rows (or []). Never raises.
    `fetch_fn` injectable for offline tests."""
    if fetch_fn is not None:
        return fetch_fn(from_date, to_date)
    opener = opener or _nse_opener()
    try:
        opener.open(urllib.request.Request(_NSE_HOME, headers=_HEADERS),
                    timeout=timeout).read()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass
    url = _ANN_API
    if from_date and to_date:
        url += (f"&from_date={from_date.strftime('%d-%m-%Y')}"
                f"&to_date={to_date.strftime('%d-%m-%Y')}")
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError, TimeoutError) as exc:
        print(f"  (corporate_events: NSE fetch failed [{exc}])")
        return []
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("rows") or []
    return payload if isinstance(payload, list) else []


def run(from_date: date = None, to_date: date = None, lake_root=None,
        fetch_fn=None, flagged_only: bool = False) -> dict:
    """Fetch -> normalize -> flag -> lake (grouped by announcement day).
    Returns a summary incl. per-class counts. Fail-open."""
    raw = fetch(from_date, to_date, fetch_fn=fetch_fn)
    by_day, counts = {}, {c: 0 for c in CATEGORIES}
    kept = 0
    for r in raw:
        ev = normalize(r)
        if ev is None:
            continue
        if flagged_only and not ev["flags"]:
            continue
        for f in ev["flags"]:
            counts[f] += 1
        day = ev["as_of"] or date.today().isoformat()
        by_day.setdefault(day, []).append(ev)
        kept += 1
    for day, rows in by_day.items():
        lake.write_partition("events", day, rows, root=lake_root)
    return {"raw_rows": len(raw), "events_stored": kept,
            "days": len(by_day), "flag_counts": counts,
            "span": (min(by_day), max(by_day)) if by_day else None}


def backfill(start: date, end: date = None, throttle: float = 6.0,
             window_days: int = 30, lake_root=None, sleep_fn=None,
             flagged_only: bool = True) -> dict:
    """Windowed historical vacuum: crawl [start, end] in `window_days` chunks,
    THROTTLED (NSE bot-block guard — extra-conservative here since the deals
    backfill may be crawling concurrently), fail-open per window. Each window
    reuses run() (which writes lake partitions per announcement day).
    `flagged_only=True` keeps only keyword-matched events (the event-gating
    substrate) so the lake stays lean over years of filings."""
    import time as _t
    from datetime import timedelta
    sleep_fn = sleep_fn or _t.sleep
    end = end or date.today()
    total = {"windows": 0, "windows_failed": 0, "raw_rows": 0,
             "events_stored": 0, "flag_counts": {c: 0 for c in CATEGORIES},
             "span": [start.isoformat(), end.isoformat()]}
    cursor, first = start, True
    while cursor <= end:
        w_end = min(cursor + timedelta(days=window_days - 1), end)
        if not first:
            sleep_fn(throttle)
        first = False
        try:
            res = run(cursor, w_end, lake_root=lake_root, flagged_only=flagged_only)
            total["windows"] += 1
            total["raw_rows"] += res["raw_rows"]
            total["events_stored"] += res["events_stored"]
            for c, v in res["flag_counts"].items():
                total["flag_counts"][c] += v
            print(f"(corporate_events backfill: {cursor}..{w_end} "
                  f"raw={res['raw_rows']} flagged_stored={res['events_stored']})")
        except Exception as exc:
            total["windows_failed"] += 1
            print(f"(corporate_events backfill: {cursor}..{w_end} FAILED [{exc}])")
        cursor = w_end + timedelta(days=1)
    print(f"(corporate_events backfill DONE: {total['events_stored']} flagged "
          f"events across {total['windows']} windows, "
          f"{total['windows_failed']} failed; flags {total['flag_counts']})")
    return total


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default=None)
    ap.add_argument("--to", dest="to", default=None)
    ap.add_argument("--backfill", metavar="YYYY-MM-DD", default=None,
                    help="historical windowed vacuum from this date to --to/today")
    ap.add_argument("--throttle", type=float, default=6.0)
    args = ap.parse_args()
    if args.backfill:
        s = date.fromisoformat(args.backfill)
        e = date.fromisoformat(args.to) if args.to else None
        print(json.dumps(backfill(s, e, throttle=args.throttle), indent=2))
    else:
        f = datetime.strptime(args.frm, "%d-%m-%Y").date() if args.frm else None
        t = datetime.strptime(args.to, "%d-%m-%Y").date() if args.to else None
        print(json.dumps(run(f, t), indent=2))

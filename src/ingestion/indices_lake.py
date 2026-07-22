"""
src/ingestion/indices_lake.py — the NSE indices leg of the macro lake
=====================================================================

Macro Regime Engine M1-2 (docs/macro_regime_engine_spec.md §1): India VIX,
NIFTY and the sector indices, from NSE's daily all-indices archive —

    nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv

one static file per trading day carrying EVERY index's OHLC/close (format
verified live 2026-07-23; archive depth reaches Oct-2019 and beyond).

Storage: the SAME macro lake as the FRED leg — `data/lake/macro/<KEY>.csv`
(`date,value` — closing value only; the featurizer's z-scores want closes).
INDIAVIX and NIFTY land exactly where `macro_features.SERIES` already
looks; sector indices land beside them for M3's playbook tables.

Laws inherited from macro_lake (whose helpers this module REUSES, not
re-implements): append-only via the max-date rule, atomic tmp+rename,
NULL-honest values, fail-open sweeps with NAMED failures.

One consequence of append-only: the historical **backfill walks FORWARD**
(oldest -> newest). A backward walk would offer each series older-than-
stored dates and the max-date rule would rightly refuse them.

Holidays are honest IL-404 no_file days, never interpolated. Outages ->
`logs/indices_lake.jsonl` (IL-404 / IL-408 / IL-500).

Read-only on all trade state. Lives on the Mac lane (bhavcopy_clerk
precedent); ONE NSE-hitting job at a time — never run the backfill while
another NSE crawler owns the lane (dev_workflow §3).

CLI:  python3 -m src.ingestion.indices_lake [--backfill N] [--date YYYY-MM-DD]
      [--dry-run]
"""
import json
import random
import time
from datetime import date, timedelta
from pathlib import Path

from src.ingestion import macro_lake as ML

ROOT = Path(__file__).resolve().parent.parent.parent
OUTAGE_LOG = ROOT / "logs" / "indices_lake.jsonl"

URL_TMPL = ("https://nsearchives.nseindia.com/content/indices/"
            "ind_close_all_{ddmmyyyy}.csv")
HTTP_TIMEOUT = 30
THROTTLE_RANGE = (3.0, 6.0)          # the bhavcopy_clerk's polite walk

# canonical lake key -> the archive's "Index Name" (matched case-insensitive)
INDEX_MAP = {
    "NIFTY": "Nifty 50",
    "INDIAVIX": "India VIX",
    "NIFTY_BANK": "Nifty Bank",
    "NIFTY_IT": "Nifty IT",
    "NIFTY_FMCG": "Nifty FMCG",
    "NIFTY_PHARMA": "Nifty Pharma",
    "NIFTY_AUTO": "Nifty Auto",
    "NIFTY_METAL": "Nifty Metal",
    "NIFTY_ENERGY": "Nifty Energy",
    "NIFTY_REALTY": "Nifty Realty",
    "NIFTY_PSU_BANK": "Nifty PSU Bank",
    "NIFTY_FIN_SERVICE": "Nifty Financial Services",
    "NIFTY_MEDIA": "Nifty Media",
    "NIFTY_INFRA": "Nifty Infrastructure",
}
_NAME_TO_KEY = {v.lower(): k for k, v in INDEX_MAP.items()}

_CLOSE_COL = "closing index value"
_NAME_COL = "index name"


def _log_outage(day: str, code: str, detail: str, log_path=None) -> None:
    path = Path(log_path) if log_path else OUTAGE_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": ML._now_iso(), "day": day,
                                 "code": code, "detail": detail[:300]}) + "\n")
    except OSError:
        pass


def fetch_day(day: date, fetch_bytes_fn=None) -> bytes:
    """Raw all-indices CSV for one day. Injectable fetch; the caller
    interprets an HTTP 404 (holiday) — this just raises what the fetch
    raises."""
    fn = fetch_bytes_fn or ML._fetch_bytes
    return fn(URL_TMPL.format(ddmmyyyy=day.strftime("%d%m%Y")))


def parse_day(raw) -> dict:
    """All-indices CSV -> {canonical_key: close|None} for MAPPED indices
    only. Header located by name (column order is NSE's to shuffle);
    a '-'/empty close is a NULL-honest None. An HTML error body or a
    shapeless payload -> {} (the caller treats it as no_file)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(
        raw, (bytes, bytearray)) else str(raw)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or "<" in lines[0][:1]:
        return {}
    header = [c.strip().lower() for c in lines[0].split(",")]
    try:
        name_i = header.index(_NAME_COL)
        close_i = header.index(_CLOSE_COL)
    except ValueError:
        return {}
    out = {}
    for ln in lines[1:]:
        cols = ln.split(",")
        if len(cols) <= max(name_i, close_i):
            continue
        key = _NAME_TO_KEY.get(cols[name_i].strip().lower())
        if key is None:
            continue                      # an index we don't track
        val = cols[close_i].strip()
        out[key] = ML._value(val if val != "-" else "")
    return out


def ingest_day(day: date, fetch_bytes_fn=None, lake_dir=None,
               dry_run: bool = False, log_path=None) -> dict:
    """One day's closes -> one appended row per mapped series (only where
    `day` is strictly newer than that series' stored history — the
    macro-lake append-only law, reused not re-implemented).

    Returns {"day", "no_file", "rows_added", "skipped_not_newer",
    "missing_from_file"} — everything named, nothing silent."""
    day_iso = day.isoformat()
    lake = Path(lake_dir) if lake_dir else ML.MACRO_LAKE
    try:
        raw = fetch_day(day, fetch_bytes_fn)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        if "404" in str(e):
            return {"day": day_iso, "no_file": True, "rows_added": {},
                    "skipped_not_newer": [], "missing_from_file": []}
        code = "IL-408" if isinstance(e, TimeoutError) else "IL-500"
        _log_outage(day_iso, code, detail, log_path)
        raise
    closes = parse_day(raw)
    if not closes:
        _log_outage(day_iso, "IL-404", "payload parsed to zero mapped "
                    "indices", log_path)
        return {"day": day_iso, "no_file": True, "rows_added": {},
                "skipped_not_newer": [], "missing_from_file": []}

    rows_added, skipped, missing = {}, [], []
    for key in INDEX_MAP:
        if key not in closes:
            missing.append(key)           # absent from this day's file
            continue
        path = lake / f"{key}.csv"
        existing = path.read_text() if path.is_file() else ""
        have = ML.parse_lake_csv(existing) if existing else []
        last = ML._last_stored_day(have)
        if last is not None and day_iso <= last:
            skipped.append(key)           # append-only: never rewrite
            continue
        if not dry_run:
            ML._append_atomically(path, existing, [(day_iso, closes[key])])
        rows_added[key] = closes[key]
    return {"day": day_iso, "no_file": False, "rows_added": rows_added,
            "skipped_not_newer": skipped, "missing_from_file": missing}


def backfill(start: date, end: date = None, fetch_bytes_fn=None,
             sleep_fn=None, lake_dir=None, dry_run: bool = False,
             log_path=None, throttle=THROTTLE_RANGE) -> dict:
    """FORWARD walk start -> end (weekends skipped): per-day fail-open —
    a holiday is an honest no_file, one dead day never aborts the walk.
    Days where every mapped series already has newer-or-equal history
    are skipped without a fetch (the free idempotent re-run)."""
    end = end or date.today()
    sleep = sleep_fn or time.sleep
    lake = Path(lake_dir) if lake_dir else ML.MACRO_LAKE
    # the walk can skip fetches only below the OLDEST per-series frontier:
    # a day newer than ANY series' last row still needs the fetch.
    frontiers = []
    for key in INDEX_MAP:
        path = lake / f"{key}.csv"
        rows = ML.parse_lake_csv(path.read_text()) if path.is_file() else []
        frontiers.append(ML._last_stored_day(rows))
    all_after = None
    if frontiers and all(f is not None for f in frontiers):
        all_after = min(frontiers)

    summary = {"attempted": 0, "captured": 0, "no_file": 0,
               "already_have": 0, "failed": 0}
    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        if all_after is not None and day.isoformat() <= all_after:
            summary["already_have"] += 1
            day += timedelta(days=1)
            continue
        summary["attempted"] += 1
        try:
            r = ingest_day(day, fetch_bytes_fn=fetch_bytes_fn,
                           lake_dir=lake_dir, dry_run=dry_run,
                           log_path=log_path)
            if r["no_file"]:
                summary["no_file"] += 1
            elif r["rows_added"]:
                summary["captured"] += 1
            else:
                summary["already_have"] += 1
        except Exception:
            summary["failed"] += 1        # already outage-logged
        sleep(random.uniform(*throttle))
        day += timedelta(days=1)
    return {"as_of": ML._now_iso(), "start": start.isoformat(),
            "end": end.isoformat(), "summary": summary,
            "dry_run": bool(dry_run)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=None,
                    help="walk this many calendar days FORWARD to today")
    ap.add_argument("--date", type=str, default=None,
                    help="ingest one day (YYYY-MM-DD); default today")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.backfill:
        out = backfill(date.today() - timedelta(days=args.backfill),
                       dry_run=args.dry_run)
    else:
        d = date.fromisoformat(args.date) if args.date else date.today()
        out = ingest_day(d, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))

"""
src/ingestion/macro_lake.py — the cross-asset macro lake (Dept 1 clerk)
=======================================================================

M1 of the Macro Regime & Pattern Engine (docs/macro_regime_engine_spec.md
§1): the FRED leg of the lake. Four free, decades-deep EOD series —

    Brent crude   DCOILBRENTEU     broad dollar  DTWEXBGS
    USD/INR       DEXINUS          US 10Y        DGS10

— none of which can ever be delisted, which is exactly why the macro
engine trains on 25+ years while stock-level work stays capped at the
bhavcopy floor. Kilobyte-scale storage, ₹0 cost, EOD cadence.

Source: FRED's official REST API (`api.stlouisfed.org`, free API key —
the public fredgraph.csv scrape endpoint answers scripted clients with
HTTP/2 resets, verified 2026-07-23; the API answers instantly). The key
lives in `.env` as FRED_API_KEY (os.environ wins if set). A missing key
is a NAMED per-series failure (ML-401), never a crash. One polite GET
per series, jittered 2-4s pause between series — the bhavcopy_clerk
mold, gentler because these are four small payloads.

Storage (append-only, one row per day):
    data/lake/macro/<KEY>.csv      header: date,value

Only dates strictly NEWER than the last stored row are appended, so a
second run adds zero rows and history is never rewritten. The write goes
temp-file + rename, so a killed run can never leave half a lake.
DROP-FOLDER: an owner-supplied `<KEY>.csv` with the same two columns
just works — the reader doesn't care who fetched it (the flows_backfill
precedent).

NULL-honest parsing: FRED marks a missing day '.' — that becomes None
and is STORED as an empty value, never 0.0 and never a silently dropped
row. A hole must stay visible to the featurizer (spec law 3); a zero
would read as "Brent went to nothing" and quietly poison every z-score.

Fail-open per series: one dead series never aborts the sweep, and the
summary NAMES the failures rather than counting them.

Outages: logs/macro_lake.jsonl   ML-401 no/rejected API key
                                 ML-404 empty/shapeless payload
                                 ML-408 timeout
                                 ML-500 unexpected

Read-only on all trade state — no journal, no portfolio, no brain_map.

CLI (Mac):  python3 -m src.ingestion.macro_lake [--series KEY] [--dry-run]
"""
import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MACRO_LAKE = ROOT / "data" / "lake" / "macro"
OUTAGE_LOG = ROOT / "logs" / "macro_lake.jsonl"
ENV_PATH = ROOT / ".env"

IST = timezone(timedelta(hours=5, minutes=30))
URL_TMPL = ("https://api.stlouisfed.org/fred/series/observations"
            "?series_id={fred_id}&api_key={api_key}&file_type=json")
API_KEY_ENV = "FRED_API_KEY"
HTTP_TIMEOUT = 30
THROTTLE_RANGE = (2.0, 4.0)       # small json payloads; polite anyway
HEADER = "date,value"

# our key -> FRED series id (spec §1)
SERIES = {"BRENT": "DCOILBRENTEU",
          "DXY": "DTWEXBGS",
          "USDINR": "DEXINUS",
          "US10Y": "DGS10"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _now_iso() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def _log_outage(key: str, code: str, detail: str, log_path=None) -> None:
    path = Path(log_path) if log_path else OUTAGE_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": _now_iso(), "series": key,
                                 "code": code, "detail": detail[:300]}) + "\n")
    except OSError:
        pass


def _fetch_bytes(url: str) -> bytes:
    """Plain GET behind a browser profile. The API answers a keyed
    request directly; no cookie warm-up, no session, no retry games."""
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT,
                                context=ctx) as resp:
        return resp.read()


def _api_key():
    """FRED_API_KEY from the environment, else the repo `.env` (the
    token_provider parse idiom, inlined — this clerk stays dependency-
    free). None when absent; the caller turns that into a NAMED ML-401."""
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if key:
        return key
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, val = line.split("=", 1)
            if name.strip() != API_KEY_ENV:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1].strip()
            return val or None
    except OSError:
        pass
    return None


def fetch_observations(fred_id: str, api_key=None, fetch_bytes_fn=None) -> bytes:
    """Raw JSON bytes for one FRED series id via the official API.
    `fetch_bytes_fn` is injectable so tests never touch the network; a
    missing key raises the sentinel the fail-open layer names ML-401."""
    key = api_key or _api_key()
    if not key:
        # No exemption for an injected fetch: "no key" must mean the same
        # thing in a test as it does at 20:05 on the cron.
        raise PermissionError(f"no_api_key: set {API_KEY_ENV} in .env")
    fn = fetch_bytes_fn or _fetch_bytes
    return fn(URL_TMPL.format(fred_id=fred_id, api_key=key))


def parse_observations(raw: bytes) -> list:
    """API JSON -> [(iso_date, float|None)] in payload order. FRED's
    null is the string '.' — that stays a VISIBLE hole (date, None),
    never 0.0 and never a dropped row (same law as the CSV parser)."""
    try:
        doc = json.loads(raw.decode("utf-8", errors="replace")
                         if isinstance(raw, (bytes, bytearray)) else raw)
    except (json.JSONDecodeError, AttributeError):
        return []
    rows = []
    for ob in (doc.get("observations") or []):
        day = _iso_day((ob or {}).get("date", ""))
        if day is None:
            continue
        rows.append((day, _value((ob or {}).get("value", ""))))
    return rows


def _value(raw: str):
    """FRED's null is '.'; anything non-numeric stays None, never 0.0."""
    s = (raw or "").strip()
    if not s or s == ".":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _iso_day(raw: str):
    """A value only if it really is a date — this is what skips a stored
    file's `date,value` header and any junk line."""
    s = (raw or "").strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def parse_lake_csv(raw) -> list:
    """Our OWN `date,value` lake file -> [(iso_date, float|None)] in file
    order. An empty value is a stored hole and comes back None — the
    round-trip that keeps a missing day missing instead of zero."""
    text = raw.decode("utf-8", errors="replace") if isinstance(
        raw, (bytes, bytearray)) else str(raw)
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        day = _iso_day(parts[0])
        if day is None:                 # header or junk line
            continue
        rows.append((day, _value(parts[1])))
    return rows


def read_series(key: str, lake_dir=None) -> list:
    """Stored history for one series, [(iso_date, float|None)] in file
    order. Honest [] for a series we've never captured."""
    root = Path(lake_dir) if lake_dir else MACRO_LAKE
    path = root / f"{key.upper()}.csv"
    if not path.is_file():
        return []
    try:
        return parse_lake_csv(path.read_bytes())
    except OSError:
        return []


def _last_stored_day(rows: list):
    """Max stored date, not the last line — a hand-dropped file need not
    be sorted, and 'strictly newer' has to mean newer than ALL of it."""
    return max((d for d, _ in rows), default=None)


def _append_atomically(path: Path, existing_text: str, new_rows: list) -> None:
    """Temp-file + rename: the lake is either the old file or the old
    file plus the new rows, never a truncated middle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = existing_text if existing_text else HEADER + "\n"
    if not body.endswith("\n"):
        body += "\n"
    body += "".join(f"{d},{'' if v is None else v}\n" for d, v in new_rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def ingest(key: str, api_key=None, fetch_bytes_fn=None, lake_dir=None,
           dry_run: bool = False, log_path=None) -> dict:
    """One series -> data/lake/macro/<KEY>.csv. Append-only, idempotent:
    only dates strictly newer than the last stored row are written, so a
    second run adds zero rows and nothing already on disk is rewritten.

    Raises on a dead fetch — `ingest_all` is the fail-open layer."""
    key = key.upper()
    if key not in SERIES:
        raise ValueError(f"unknown macro series {key!r}; "
                         f"known: {sorted(SERIES)}")
    root = Path(lake_dir) if lake_dir else MACRO_LAKE
    path = root / f"{key}.csv"
    existing_text = path.read_text() if path.is_file() else ""
    have = parse_lake_csv(existing_text) if existing_text else []
    last = _last_stored_day(have)

    raw = fetch_observations(SERIES[key], api_key=api_key,
                             fetch_bytes_fn=fetch_bytes_fn)
    fetched = parse_observations(raw)
    if not fetched:
        _log_outage(key, "ML-404", "payload parsed to zero dated rows",
                    log_path)
        raise ValueError(f"{key}: payload parsed to zero dated rows")

    fresh = [(d, v) for d, v in fetched if last is None or d > last]
    if fresh and not dry_run:
        _append_atomically(path, existing_text, fresh)
    return {"series": key, "fred_id": SERIES[key], "rows_added": len(fresh),
            "rows_fetched": len(fetched),
            "holes_added": sum(1 for _, v in fresh if v is None),
            "last_before": last,
            "last_after": _last_stored_day(fresh) or last,
            "dry_run": bool(dry_run), "as_of": _now_iso()}


def ingest_all(api_key=None, fetch_bytes_fn=None, sleep_fn=None, lake_dir=None,
               dry_run: bool = False, log_path=None,
               throttle=THROTTLE_RANGE) -> dict:
    """All four series, fail-open: one dead series never aborts the
    sweep, and the summary NAMES what failed with a reason (a count
    alone is how a silently-empty lake goes unnoticed for a month).
    NOTHING raises out of here — a cron line gets a dict, always."""
    sleep = sleep_fn or time.sleep
    ok, failed, rows_added, details = [], [], {}, {}
    keys = sorted(SERIES)
    key_value = api_key or _api_key()

    def _fail(series, code, reason, detail):
        failed.append({"series": series, "code": code, "reason": reason,
                       "detail": detail[:200]})
        rows_added[series] = 0

    if not key_value and fetch_bytes_fn is None:
        # No key on the REAL network path = four certain failures; say so
        # instantly instead of sleeping 9 seconds to prove it four times
        # over. An injected fetch needs no auth, so tests never trip this.
        for key in keys:
            _log_outage(key, "ML-401", f"no {API_KEY_ENV}", log_path)
            _fail(key, "ML-401", "no_api_key",
                  f"no_api_key: {API_KEY_ENV} missing or empty "
                  "(.env / environment)")
        return {"as_of": _now_iso(), "ok": ok, "failed": failed,
                "rows_added": rows_added, "dry_run": bool(dry_run),
                "details": details}

    for i, key in enumerate(keys):
        try:
            r = ingest(key, api_key=key_value, fetch_bytes_fn=fetch_bytes_fn,
                       lake_dir=lake_dir, dry_run=dry_run, log_path=log_path)
            ok.append(key)
            rows_added[key] = r["rows_added"]
            details[key] = r
        except PermissionError as e:
            _log_outage(key, "ML-401", str(e), log_path)
            _fail(key, "ML-401", "no_api_key", str(e))
        except TimeoutError as e:
            _log_outage(key, "ML-408", str(e), log_path)
            _fail(key, "ML-408", "timeout", f"{type(e).__name__}: {e}")
        except Exception as e:
            zero_rows = "zero dated rows" in str(e)
            code = "ML-404" if zero_rows else "ML-500"
            reason = "empty_payload" if zero_rows else "fetch_failed"
            if not zero_rows:       # ML-404 was already logged by ingest
                _log_outage(key, code, f"{type(e).__name__}: {e}", log_path)
            _fail(key, code, reason, f"{type(e).__name__}: {e}")
        if i < len(keys) - 1:
            sleep(random.uniform(*throttle))
    return {"as_of": _now_iso(), "ok": ok, "failed": failed,
            "rows_added": rows_added, "dry_run": bool(dry_run),
            "details": details}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--series", type=str, default=None,
                    help=f"one series only ({', '.join(sorted(SERIES))})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what WOULD append; write nothing")
    args = ap.parse_args()
    if args.series:
        try:
            out = ingest(args.series, dry_run=args.dry_run)
        except Exception as e:      # one series, same honesty as the sweep
            out = {"series": args.series.upper(), "rows_added": 0,
                   "failed": f"{type(e).__name__}: {e}"}
    else:
        out = ingest_all(dry_run=args.dry_run)
        out.pop("details", None)
    print(json.dumps(out, indent=2))

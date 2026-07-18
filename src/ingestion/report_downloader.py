"""
src/ingestion/report_downloader.py — annual-report fetcher (Dept 1 clerk)
=========================================================================

Fills data/fundamental_reports/<TICKER>/ with the latest annual-report PDF
for each ticker in data/screening_queue.json (falling back to an explicit
--tickers list). The analysis of those PDFs is Department 8's job
(`analysis/annual_report_analyzer.py`); this clerk only captures.

Source: NSE's own annual-reports archive (the same corporate-filings
service the owner's manual downloads came from — the AR_*.pdf filenames
match). Access follows the house safe-crawl doctrine, the SAME pattern
`deals_tracker` has used in production since July:

  * cookie-warm handshake + browser-profile headers — the documented way
    NSE serves its public JSON endpoints (a bare urllib UA gets a 401);
  * HARD throttle: a jittered 8-15s pause between tickers, one polite
    retry after a longer pause on a transient failure — and if NSE still
    says no, we log an outage code and STOP for that ticker. No identity
    rotation, no block-dodging: public filings, polite pace, honest exit.
  * MAC-ONLY, never the VM (the VM's IP fronts the live engine — a ban
    there blinds trading; ingestion crawls always run from the Mac).

Fail-open per ticker: every failure becomes one honest outage row in
logs/report_downloader.jsonl (codes below) and the loop moves on.
Idempotent: a PDF already on disk for the latest fiscal year is skipped.

Outage codes:  RD-401 handshake/auth refused   RD-404 no reports listed
               RD-408 timeout                  RD-500 unexpected error

CLI, from the project folder (Mac):

    python3 -m src.ingestion.report_downloader [--tickers RELIANCE TCS]
        [--limit N] [--fiscal YYYY] [--dry-run]

`--fiscal YYYY` picks the listing row whose `toYr` matches YYYY instead
of the newest — Dept 8's back-year fetches (the on-disk nine need more
than one year each). Omit it for the default newest-year behaviour.
"""
import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE_PATH = ROOT / "data" / "screening_queue.json"
REPORTS_DIR = ROOT / "data" / "fundamental_reports"
OUTAGE_LOG = ROOT / "logs" / "report_downloader.jsonl"
WATCHLIST_PATH = ROOT / "config" / "watchlist.yaml"

IST = timezone(timedelta(hours=5, minutes=30))
API_URL = "https://www.nseindia.com/api/annual-reports?index=equities&symbol={symbol}"
HOMEPAGE = "https://www.nseindia.com/"
HTTP_TIMEOUT = 30
THROTTLE_RANGE = (8.0, 15.0)      # jittered pause between tickers
RETRY_PAUSE = 30.0                # one polite retry, then honest failure

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/"
               "corporate-filings-annual-reports",
}


def _now_iso() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def _log_outage(ticker: str, code: str, detail: str, log_path=None) -> None:
    path = Path(log_path) if log_path else OUTAGE_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": _now_iso(), "ticker": ticker,
                                 "code": code, "detail": detail[:300]}) + "\n")
    except OSError:
        pass                       # a broken log must not break the loop


def _nse_symbol(ticker: str) -> str:
    """Watchlist tickers carry the Yahoo-style .NS suffix; NSE's API wants
    the bare symbol."""
    return ticker.split(".")[0].strip().upper()


def load_queue(queue_path=None) -> list:
    """data/screening_queue.json ({"tickers": [...]}) — the Step-1
    screener's output. Missing/broken file returns [] (the CLI then
    requires --tickers; nothing is guessed)."""
    path = Path(queue_path) if queue_path else QUEUE_PATH
    try:
        data = json.loads(path.read_text())
        return [t for t in data.get("tickers", []) if isinstance(t, str)]
    except (OSError, ValueError):
        return []


# ----------------------------------------------------- the network seams
# Both are injectable in run() so the whole loop tests offline with fakes.

def _ssl_ctx():
    """urllib doesn't use the system CA store; certifi (already a repo
    dep — the deals_tracker precedent) supplies the bundle."""
    import ssl
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def _fetch_json(url: str):
    """GET url behind a cookie-warmed NSE session -> parsed JSON."""
    import http.cookiejar
    import urllib.request
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_ctx()),
        urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = list(HEADERS.items())
    try:
        opener.open(HOMEPAGE, timeout=HTTP_TIMEOUT)      # cookie warm-up
    except Exception:
        pass    # verified 2026-07-18: NSE 403s the homepage for scripted
                # sessions but still hands the jar its cookies, and the
                # annual-reports API answers fine — warm-up is best-effort
    with opener.open(url, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_bytes(url: str) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT,
                                context=_ssl_ctx()) as resp:
        return resp.read()


def latest_report(listing: dict, fiscal: str = None) -> dict | None:
    """NSE's listing -> the newest usable row: needs a PDF url in
    `fileName` and a `toYr`. Returns None (RD-404 upstream) when the
    listing is empty or shapeless — never a guessed URL.

    `fiscal`: pick the row whose `toYr` matches this year exactly instead
    of the max — the Dept 8 back-year fetch (e.g. --fiscal 2024 for
    eMudhra's FY24 report). No match is honest None, never a fallback to
    the newest row."""
    rows = (listing or {}).get("data") or []
    usable = [r for r in rows
              if isinstance(r, dict) and r.get("fileName")
              and str(r.get("fileName")).lower().endswith(".pdf")
              and str(r.get("toYr", "")).isdigit()]
    if not usable:
        return None
    if fiscal is not None:
        matches = [r for r in usable if str(r["toYr"]) == str(fiscal)]
        return max(matches, key=lambda r: int(r["toYr"])) if matches else None
    return max(usable, key=lambda r: int(r["toYr"]))


def target_path(ticker: str, row: dict, out_dir=None) -> Path:
    root = Path(out_dir) if out_dir else REPORTS_DIR
    sym = _nse_symbol(ticker)
    return root / sym / f"AR_{sym}_{row.get('fromYr', 'x')}_{row['toYr']}.pdf"


def fetch_one(ticker: str, fetch_json_fn=_fetch_json,
              fetch_bytes_fn=_fetch_bytes, out_dir=None,
              log_path=None, sleep_fn=time.sleep, fiscal: str = None) -> dict:
    """One ticker, never raises. Returns {ticker, status, ...} where
    status is downloaded | already_have | outage.

    `fiscal`: fetch that specific fiscal year's row (toYr match) instead
    of the listing's newest — see `latest_report`."""
    sym = _nse_symbol(ticker)

    def _attempt():
        return fetch_json_fn(API_URL.format(symbol=sym))

    try:
        try:
            listing = _attempt()
        except Exception:
            sleep_fn(RETRY_PAUSE)          # one polite retry, then honest
            listing = _attempt()
        row = latest_report(listing, fiscal=fiscal)
        if row is None:
            detail = (f"no report for fiscal {fiscal}" if fiscal
                      else "no usable annual-report rows")
            _log_outage(sym, "RD-404", detail, log_path)
            return {"ticker": sym, "status": "outage", "code": "RD-404"}
        dest = target_path(ticker, row, out_dir)
        if dest.exists():
            return {"ticker": sym, "status": "already_have",
                    "path": str(dest)}
        blob = fetch_bytes_fn(row["fileName"])
        if not blob or not blob[:5].startswith(b"%PDF"):
            _log_outage(sym, "RD-500",
                        f"response is not a PDF ({len(blob or b'')} bytes)",
                        log_path)
            return {"ticker": sym, "status": "outage", "code": "RD-500"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        return {"ticker": sym, "status": "downloaded", "path": str(dest),
                "bytes": len(blob), "fiscal": f"{row.get('fromYr')}-"
                                              f"{row.get('toYr')}"}
    except TimeoutError as e:
        _log_outage(sym, "RD-408", str(e), log_path)
        return {"ticker": sym, "status": "outage", "code": "RD-408"}
    except Exception as e:
        code = "RD-401" if "401" in str(e) or "403" in str(e) else "RD-500"
        _log_outage(sym, code, f"{type(e).__name__}: {e}", log_path)
        return {"ticker": sym, "status": "outage", "code": code}


def run(tickers: list = None, limit: int = None, queue_path=None,
        fetch_json_fn=_fetch_json, fetch_bytes_fn=_fetch_bytes,
        out_dir=None, log_path=None, sleep_fn=time.sleep,
        throttle=THROTTLE_RANGE, fiscal: str = None) -> dict:
    """The loop: queue (or explicit tickers) -> fetch_one each, jittered
    pause between tickers, one summary dict out. Never raises.

    `fiscal`: applied to every ticker in this run — a specific back-year
    batch, not a per-ticker mix."""
    todo = [t for t in (tickers if tickers is not None
                        else load_queue(queue_path))]
    if limit:
        todo = todo[:int(limit)]
    results = []
    for i, ticker in enumerate(todo):
        results.append(fetch_one(ticker, fetch_json_fn, fetch_bytes_fn,
                                 out_dir, log_path, sleep_fn, fiscal=fiscal))
        if i < len(todo) - 1:
            sleep_fn(random.uniform(*throttle))
    by = {}
    for r in results:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"as_of": _now_iso(), "attempted": len(results),
            "summary": by, "results": results}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None,
                    help="override the screening queue")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fiscal", type=str, default=None,
                    help="fetch the report whose toYr matches this year "
                         "(e.g. 2024) instead of the newest")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be fetched; no network")
    args = ap.parse_args()
    queue = args.tickers if args.tickers else load_queue()
    if args.dry_run:
        print(json.dumps({"would_fetch": queue[:args.limit]}, indent=2))
    else:
        out = run(tickers=args.tickers, limit=args.limit, fiscal=args.fiscal)
        print(json.dumps({k: out[k] for k in ("as_of", "attempted",
                                              "summary")}, indent=2))
        for r in out["results"]:
            print(f"  {r['ticker']:14} {r['status']}"
                  + (f" -> {r.get('path', '')}" if r.get("path") else "")
                  + (f" [{r.get('code')}]" if r.get("code") else ""))

"""
src/ingestion/bhavcopy_clerk.py — daily EOD equity bars (Dept 1 clerk)
======================================================================

The Dynamic Execution Layer's fuel line (owner directive 2026-07-19), and
the long-deferred daana for everything that ever wanted daily equity
bars: fetches NSE's FULL BHAVCOPY — one CSV per trading day covering
every listed equity's OHLCV *plus* delivered-quantity/percentage (the
smart-money column the original screener design wanted).

Source file:  sec_bhavdata_full_DDMMYYYY.csv  (NSE archives — a static
public end-of-day product, ~2MB/day). Simple GET behind the house NSE
session (certifi, browser profile); gentle 3-6s pause between days on
backfill — these are static archive files, but polite is polite.
MAC-ONLY like every ingestion clerk (the VM's IP fronts the live engine).

Storage (capture-everything): the RAW csv lands at
    data/lake/bhavcopy/YYYY-MM-DD.csv
and `bars_for(symbol, days)` reads bars back out chronologically for the
pricer. DROP-FOLDER: owner-supplied CSVs named the same way can simply
be placed in that folder — the reader doesn't care who fetched them
(the flows_backfill precedent).

NULL-honest parsing: only SERIES EQ/BE rows; ' -' (NSE's null) -> None,
never 0. A missing day is logged honestly (BC-404 — usually a holiday)
and the loop continues; nothing is interpolated, EVER.

Outages: logs/bhavcopy_clerk.jsonl   BC-401 refused | BC-404 no file |
                                     BC-408 timeout | BC-500 unexpected

CLI (Mac):  python3 -m src.ingestion.bhavcopy_clerk [--backfill N]
            [--date YYYY-MM-DD]
"""
import csv
import io
import json
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BHAVCOPY_LAKE = ROOT / "data" / "lake" / "bhavcopy"
OUTAGE_LOG = ROOT / "logs" / "bhavcopy_clerk.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))
URL_TMPL = ("https://nsearchives.nseindia.com/products/content/"
            "sec_bhavdata_full_{ddmmyyyy}.csv")
THROTTLE_RANGE = (3.0, 6.0)       # static archive files; gentle anyway
KEEP_SERIES = {"EQ", "BE"}

# NSE column -> our bar field (headers carry stray spaces; normalized)
_COLS = {"PREV_CLOSE": "prev_close", "OPEN_PRICE": "open",
         "HIGH_PRICE": "high", "LOW_PRICE": "low", "CLOSE_PRICE": "close",
         "AVG_PRICE": "avg_price", "TTL_TRD_QNTY": "volume",
         "TURNOVER_LACS": "turnover_lacs", "NO_OF_TRADES": "trades",
         "DELIV_QTY": "deliv_qty", "DELIV_PER": "deliv_pct"}


def _now_iso() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def _log_outage(day: str, code: str, detail: str, log_path=None) -> None:
    path = Path(log_path) if log_path else OUTAGE_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": _now_iso(), "day": day, "code": code,
                                 "detail": detail[:300]}) + "\n")
    except OSError:
        pass


def _num(v):
    """' -' is NSE's null; anything non-numeric stays None, never 0."""
    s = str(v or "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _fetch_bytes(url: str) -> bytes:
    from src.ingestion.report_downloader import _fetch_bytes as rd_bytes
    return rd_bytes(url)


def parse_bhavcopy(csv_text: str) -> dict:
    """Raw CSV -> {symbol: bar_dict} for EQ/BE rows, NULL-honest."""
    out = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        r = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if r.get("SERIES") not in KEEP_SERIES:
            continue
        sym = r.get("SYMBOL", "").upper()
        if not sym:
            continue
        bar = {dst: _num(r.get(src)) for src, dst in _COLS.items()}
        bar["date"] = r.get("DATE1")
        out[sym] = bar
    return out


def fetch_day(day: date, fetch_bytes_fn=_fetch_bytes, out_dir=None,
              log_path=None) -> dict:
    """One trading day -> raw CSV in the lake. Never raises.
    status: captured | already_have | no_file | outage."""
    tag = day.isoformat()
    root = Path(out_dir) if out_dir else BHAVCOPY_LAKE
    dest = root / f"{tag}.csv"
    if dest.exists():
        return {"day": tag, "status": "already_have"}
    url = URL_TMPL.format(ddmmyyyy=day.strftime("%d%m%Y"))
    try:
        blob = fetch_bytes_fn(url)
        text = blob.decode("utf-8", errors="replace")
        if "SYMBOL" not in text[:200]:          # HTML error page, not data
            _log_outage(tag, "BC-404", "no bhavcopy (holiday?)", log_path)
            return {"day": tag, "status": "no_file"}
        rows = parse_bhavcopy(text)
        if not rows:
            _log_outage(tag, "BC-404", "csv parsed to zero EQ rows",
                        log_path)
            return {"day": tag, "status": "no_file"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        return {"day": tag, "status": "captured", "symbols": len(rows)}
    except TimeoutError as e:
        _log_outage(tag, "BC-408", str(e), log_path)
        return {"day": tag, "status": "outage", "code": "BC-408"}
    except Exception as e:
        code = ("BC-404" if "404" in str(e)
                else "BC-401" if "401" in str(e) or "403" in str(e)
                else "BC-500")
        detail = "no bhavcopy (holiday?)" if code == "BC-404" else \
            f"{type(e).__name__}: {e}"
        _log_outage(tag, code, detail, log_path)
        status = "no_file" if code == "BC-404" else "outage"
        return {"day": tag, "status": status,
                **({"code": code} if status == "outage" else {})}


def backfill(days: int, end: date = None, fetch_bytes_fn=_fetch_bytes,
             out_dir=None, log_path=None, sleep_fn=time.sleep,
             throttle=THROTTLE_RANGE) -> dict:
    """Walk back `days` CALENDAR days from `end` (default: today, IST),
    fetching every weekday. Holidays come back no_file — honest, logged,
    never interpolated. Idempotent via already_have."""
    end = end or datetime.now(IST).date()
    results = []
    fetched_something = False
    for i in range(days):
        day = end - timedelta(days=i)
        if day.weekday() >= 5:                  # weekend — no session
            continue
        r = fetch_day(day, fetch_bytes_fn, out_dir, log_path)
        results.append(r)
        if r["status"] == "captured":
            fetched_something = True
        if r["status"] in ("captured", "no_file", "outage"):
            sleep_fn(random.uniform(*throttle))
    by = {}
    for r in results:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {"as_of": _now_iso(), "attempted": len(results), "summary": by,
            "results": results, "any_new": fetched_something}


def bars_for(symbol: str, days: int = 90, lake_dir=None) -> list:
    """Chronological (oldest->newest) bars for one symbol from the day
    files on disk. Honest []: unknown symbol or empty lake. Days the
    symbol didn't trade simply aren't there — no gap filling."""
    root = Path(lake_dir) if lake_dir else BHAVCOPY_LAKE
    if not root.is_dir():
        return []
    sym = symbol.split(".")[0].strip().upper()
    bars = []
    for f in sorted(root.glob("????-??-??.csv"))[-days:]:
        try:
            rows = parse_bhavcopy(f.read_text())
        except OSError:
            continue
        bar = rows.get(sym)
        if bar:
            bar["session"] = f.stem
            bars.append(bar)
    return bars


def bars_for_many(symbols: list, days: int = 250, lake_dir=None) -> dict:
    """{symbol: chronological bars} for MANY symbols in ONE pass over the
    day files — the batch loader the dynamic pricer uses (91 darlings x
    218 files re-parsed per symbol would be quadratic; this parses each
    file exactly once). Same honesty rules as bars_for."""
    root = Path(lake_dir) if lake_dir else BHAVCOPY_LAKE
    syms = {s.split(".")[0].strip().upper() for s in symbols or []}
    out = {s: [] for s in syms}
    if not root.is_dir():
        return out
    for f in sorted(root.glob("????-??-??.csv"))[-days:]:
        try:
            rows = parse_bhavcopy(f.read_text())
        except OSError:
            continue
        for s in syms:
            bar = rows.get(s)
            if bar:
                bar = dict(bar)
                bar["session"] = f.stem
                out[s].append(bar)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=None,
                    help="walk back N calendar days")
    ap.add_argument("--date", type=str, default=None,
                    help="fetch one specific day (YYYY-MM-DD)")
    args = ap.parse_args()
    if args.date:
        r = fetch_day(date.fromisoformat(args.date))
        print(json.dumps(r, indent=2))
    else:
        out = backfill(args.backfill or 5)
        print(json.dumps({k: out[k] for k in ("as_of", "attempted",
                                              "summary")}, indent=2))

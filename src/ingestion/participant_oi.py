"""
src/ingestion/participant_oi.py — NSE participant-wise F&O open interest (decision #153, GEX seam 2).

NSE publishes, per trading day, the contracts each participant class (Client, DII, FII, Pro) holds long
and short in index/stock futures and options. GEX's dealer sign (D2) is calibrated from it: the dealer
group's net long-minus-short share of each option class's total OI becomes that side's weight.

Raw file archived as published (sha256), parsed row written to data/lake/participant_oi/. Advisory only:
nothing on the trade path reads it. VM cron #38 (18:45 Mon-Fri) runs `--today`, then the day's GEX profile.

  python3 -m src.ingestion.participant_oi --today          # today + any of the last 5 sessions still missing
  python3 -m src.ingestion.participant_oi --day 2026-10-16
  python3 -m src.ingestion.participant_oi --backfill 2026-07-13 2026-10-19
"""
from __future__ import annotations

import argparse
import csv
import io
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

from src import lake

URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"
DATASET = "participant_oi"
HTTP_TIMEOUT = 20
CATEGORIES = ("Client", "DII", "FII", "Pro", "TOTAL")
# the "dealer" group whose net option position sets the GEX sign; D2a ruled 2026-10-20 (#154): non_client
DEALERS = {"non_client": ("DII", "FII", "Pro"), "pro": ("Pro",), "pro_fii": ("Pro", "FII")}
DEFAULT_DEALER = "non_client"
_COLUMNS = {
    "future index long": "fut_index_long", "future index short": "fut_index_short",
    "future stock long": "fut_stock_long", "future stock short": "fut_stock_short",
    "option index call long": "opt_index_call_long", "option index put long": "opt_index_put_long",
    "option index call short": "opt_index_call_short", "option index put short": "opt_index_put_short",
    "option stock call long": "opt_stock_call_long", "option stock put long": "opt_stock_put_long",
    "option stock call short": "opt_stock_call_short", "option stock put short": "opt_stock_put_short",
    "total long contracts": "total_long", "total short contracts": "total_short",
}


def url_for(day: str) -> str:
    d = date.fromisoformat(day)
    return URL.format(ddmmyyyy=d.strftime("%d%m%Y"))


def parse(text: str) -> dict:
    """The CSV -> {"rows": {category: {field: int}}, "balanced": bool, "reason"}; {} rows on a bad file."""
    rows, header = {}, None
    for rec in csv.reader(io.StringIO(text or "")):
        cells = [c.strip().strip('"').strip() for c in rec]
        if not cells or not cells[0]:
            continue
        if cells[0].lower() == "client type":
            header = [_COLUMNS.get(" ".join(c.lower().split())) for c in cells[1:]]
            continue
        if header and cells[0] in CATEGORIES:
            vals = {}
            for key, raw in zip(header, cells[1:]):
                if key:
                    try:
                        vals[key] = int(float(raw.replace(",", "")))
                    except ValueError:
                        vals[key] = None
            rows[cells[0]] = vals
    out = {"rows": rows, "balanced": None, "reason": None}
    total = rows.get("TOTAL")
    if not total or any(c not in rows for c in CATEGORIES):
        out["reason"] = "file lacks the participant rows or the TOTAL row"
        return out
    pairs = [("opt_index_call_long", "opt_index_call_short"), ("opt_index_put_long", "opt_index_put_short"),
             ("opt_stock_call_long", "opt_stock_call_short"), ("opt_stock_put_long", "opt_stock_put_short")]
    out["balanced"] = all(total.get(a) == total.get(b) for a, b in pairs)
    return out


def sign_weights(parsed: dict, dealer: str = DEFAULT_DEALER) -> dict | None:
    """{"index": {"call", "put"}, "stock": {"call", "put"}}: the dealer group's (long - short) / total OI per
    option class, the side weight `gamma_profile` multiplies gamma x OI by. None when the file cannot speak."""
    rows = (parsed or {}).get("rows") or {}
    total = rows.get("TOTAL") or {}
    group = DEALERS.get(dealer)
    if not group or not total or any(g not in rows for g in group):
        return None
    out = {}
    for cls in ("index", "stock"):
        out[cls] = {}
        for side in ("call", "put"):
            lk, sk = f"opt_{cls}_{side}_long", f"opt_{cls}_{side}_short"
            oi = total.get(lk)
            nets = [rows[g].get(lk) - rows[g].get(sk) for g in group
                    if rows[g].get(lk) is not None and rows[g].get(sk) is not None]
            if not oi or len(nets) != len(group):
                return None
            out[cls][side] = round(sum(nets) / oi, 6)
    return out


def fetch(day: str, opener=None):
    """(status, raw bytes or None) for one day's file. 404 = no file (holiday or not yet published)."""
    req = urllib.request.Request(url_for(day), headers={"User-Agent": "Mozilla/5.0 (alpha_trading research)"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError) as exc:
        return f"error: {exc}", None


def run(day: str, root=None, fetch_fn=None) -> dict:
    """Fetch, archive the raw file, write the parsed row. Never raises; a missing day is a named skip."""
    status, raw = (fetch_fn or fetch)(day)
    if raw is None:
        return {"day": day, "written": False, "status": status,
                "reason": "no file (holiday or not yet published)" if status == 404 else f"fetch failed ({status})"}
    blob = lake.archive_blob(DATASET, day, "fao_participant_oi", raw, ext="csv", root=root)
    parsed = parse(raw.decode("utf-8", errors="replace"))
    if parsed["reason"]:
        return {"day": day, "written": False, "status": status, "reason": parsed["reason"]}
    row = {"date": day, "source": url_for(day), "sha256": (blob or {}).get("sha256"),
           "balanced": parsed["balanced"], "rows": parsed["rows"],
           "weights": {d: sign_weights(parsed, d) for d in DEALERS}}
    path = lake.write_partition(DATASET, day, [row], root=root)
    return {"day": day, "written": path is not None, "status": status, "balanced": parsed["balanced"]}


def backfill(start: str, end: str, root=None, fetch_fn=None, pause: float = 1.0) -> list:
    """`run` for every NSE trading day in [start, end], one request at a time."""
    from src import nse_calendar
    out, d, last = [], date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        if nse_calendar.is_trading_day(d):
            out.append(run(d.isoformat(), root, fetch_fn))
            if pause:
                time.sleep(pause)
        d += timedelta(days=1)
    return out


def catch_up(today: date, sessions: int = 5, root=None, fetch_fn=None, pause: float = 1.0) -> list:
    """`run` for each of the last `sessions` NSE trading days up to `today` not yet in the lake —
    a file NSE published late is picked up on the next evening. One request at a time."""
    from src import nse_calendar
    days, d = [], today
    while len(days) < sessions and (today - d).days < 3 * sessions:
        if nse_calendar.is_trading_day(d):
            days.append(d.isoformat())
        d -= timedelta(days=1)
    out = []
    for day in reversed(days):
        if load(day, root) is None:
            out.append(run(day, root, fetch_fn))
            if pause:
                time.sleep(pause)
    return out


def load(day: str, root=None) -> dict | None:
    """The parsed row for one day from the lake, or None."""
    rows = lake.read_day(DATASET, day, root=root)
    return rows[0] if rows else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--day")
    ap.add_argument("--today", action="store_true", help="today (IST) + missing days of the last 5 sessions")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    ap.add_argument("--root", help="lake root (default data/lake)")
    args = ap.parse_args(argv)
    if args.today:
        from datetime import datetime, timezone
        res = catch_up(datetime.now(timezone(timedelta(hours=5, minutes=30))).date(), root=args.root)
    elif args.backfill:
        res = backfill(args.backfill[0], args.backfill[1], args.root)
    elif args.day:
        res = [run(args.day, args.root)]
    else:
        ap.print_help()
        return 0
    for r in res:
        print(r)
    print(f"[participant_oi] {sum(r['written'] for r in res)}/{len(res)} days written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

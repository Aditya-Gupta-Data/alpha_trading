"""
src/ingestion/flows_backfill.py — historical FII/DII backfill into the lake
===========================================================================

WHY A FILE INGESTOR AND NOT AN API CRAWLER (verified 2026-07-16):
  * NSE `fiidiiTradeReact` serves ONLY the current day — it ignores date
    params and has no historical variant (the `/api/historical/...` paths
    return HTML 404).
  * Moneycontrol's FII/DII page is JS-rendered (no server-side table in the
    HTML) and its JSON feeds 404 / "no data".
  * No free, machine-readable, multi-year DAILY FII/DII source exists.

So real history cannot be scraped — it must be INGESTED from a dataset the
owner can export (StockEdge / Trendlyne / a broker terminal / a Kaggle CSV),
while the live daily cron (`flows_tracker`, 19:35 IST) accumulates the series
FORWARD from today. This module normalizes such an export into the EXACT shape
`flows_tracker` writes and lands it idempotently in the lake, so the
Regime-Compass backtest can consume REAL flows the moment history is supplied.

Output shape (per day, ₹ crore) — identical to flows_tracker:
    {"as_of": "YYYY-MM-DD", "source": "backfill",
     "fii": {"buy": .., "sell": .., "net": ..},
     "dii": {"buy": .., "sell": .., "net": ..}}
written via lake.write_partition("flows", day, [row]) (REPLACE = idempotent).

Accepted inputs (auto-detected by extension / content):
  * JSON: [ {date, fii_net, dii_net, [fii_buy, fii_sell, dii_buy, dii_sell]}, ...]
  * CSV : a header row containing any of those column names
          (case / space / underscore-insensitive).
Dates parsed from YYYY-MM-DD, DD-Mon-YYYY, DD/MM/YYYY, DD-MM-YYYY.
`net` is required OR is derived from buy-sell; a row with neither is skipped
(never a guessed zero — the ingestion NULL-honesty rule).

CLI:  python3 -m src.ingestion.flows_backfill --file history.csv [--dry-run]
Reader for consumers:  load_flow_history() -> {date_iso: {fii_net, dii_net, ...}}
"""
import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

from src import lake

ROOT = Path(__file__).resolve().parent.parent.parent

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("₹", "").strip()
    if s in ("", "-", "--", "NA", "N/A", "null", "None"):
        return None
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


def parse_date(s):
    """Any common Indian-market date spelling -> ISO 'YYYY-MM-DD', or None."""
    if s is None:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # DD-Mon-YYYY  /  DD Mon YYYY
    m = re.match(r"^(\d{1,2})[-\s]([A-Za-z]{3})[A-Za-z]*[-\s](\d{4})$", s)
    if m:
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}"
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


# column aliases -> canonical field (keys are _norm_key'd)
_ALIASES = {
    "date": "date", "tradedate": "date", "asof": "date",
    "fiibuy": "fii_buy", "fiibuyvalue": "fii_buy", "fpibuy": "fii_buy",
    "fiisell": "fii_sell", "fiisellvalue": "fii_sell", "fpisell": "fii_sell",
    "fiinet": "fii_net", "fiinetvalue": "fii_net", "fpinet": "fii_net", "fii": "fii_net",
    "diibuy": "dii_buy", "diibuyvalue": "dii_buy",
    "diisell": "dii_sell", "diisellvalue": "dii_sell",
    "diinet": "dii_net", "diinetvalue": "dii_net", "dii": "dii_net",
}


def _canonical(rec: dict) -> dict:
    """Map an arbitrary raw row's keys onto the canonical field names."""
    out = {}
    for k, v in rec.items():
        canon = _ALIASES.get(_norm_key(k))
        if canon:
            out[canon] = v
    return out


def normalize_record(rec: dict) -> dict | None:
    """One raw historical row -> the flows_tracker-shaped normalized day, or
    None when the date is unparseable or NEITHER side yields a net."""
    c = _canonical(rec)
    day = parse_date(c.get("date"))
    if day is None:
        return None

    def side(prefix):
        buy, sell, net = (_num(c.get(f"{prefix}_buy")),
                          _num(c.get(f"{prefix}_sell")),
                          _num(c.get(f"{prefix}_net")))
        if net is None and None not in (buy, sell):
            net = round(buy - sell, 2)
        if buy is None and sell is None and net is None:
            return None
        return {"buy": buy, "sell": sell, "net": net}

    fii, dii = side("fii"), side("dii")
    if fii is None and dii is None:
        return None            # nothing real on this row — skip, never guess
    return {"as_of": day, "source": "backfill", "fii": fii, "dii": dii}


def read_file(path) -> list:
    """Raw rows from a .json (list) or .csv (DictReader). Never raises."""
    path = Path(path)
    text = path.read_text()
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("rows") or data.get("data") or []
        return data if isinstance(data, list) else []
    return list(csv.DictReader(text.splitlines()))


def backfill(path, lake_root=None, dry_run: bool = False) -> dict:
    """Ingest a historical file into the lake, one idempotent partition per
    day. Returns a summary; never raises for a bad row (counted, skipped)."""
    rows = read_file(path)
    seen, ingested, skipped = {}, 0, 0
    for raw in rows:
        norm = normalize_record(raw) if isinstance(raw, dict) else None
        if norm is None:
            skipped += 1
            continue
        seen[norm["as_of"]] = norm       # last write wins per day (dedupe)
    for day, norm in sorted(seen.items()):
        if not dry_run:
            lake.write_partition("flows", day, [norm], root=lake_root)
        ingested += 1
    days = sorted(seen)
    return {"rows_read": len(rows), "days_ingested": ingested,
            "rows_skipped": skipped, "dry_run": dry_run,
            "span": (days[0], days[-1]) if days else None}


def load_flow_history(lake_root=None, start=None, end=None) -> dict:
    """THE reader for consumers (the Regime-Compass flow gate): every lake
    flows day as {date_iso: {fii_net, dii_net, fii_buy, ...}}. [] safe."""
    out = {}
    for day, row in lake.scan("flows", start=start, end=end, root=lake_root):
        if not isinstance(row, dict):
            continue
        fii, dii = row.get("fii") or {}, row.get("dii") or {}
        out[row.get("as_of") or day] = {
            "fii_net": fii.get("net"), "dii_net": dii.get("net"),
            "fii_buy": fii.get("buy"), "fii_sell": fii.get("sell"),
            "dii_buy": dii.get("buy"), "dii_sell": dii.get("sell"),
            "source": row.get("source")}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backfill historical FII/DII into the lake")
    ap.add_argument("--file", required=True, help="historical CSV or JSON export")
    ap.add_argument("--dry-run", action="store_true", help="parse only, no lake writes")
    args = ap.parse_args()
    print(json.dumps(backfill(args.file, dry_run=args.dry_run), indent=2))

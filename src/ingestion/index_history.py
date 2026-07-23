"""
src/ingestion/index_history.py — NSE historical-index CSV drop-folder clerk
===========================================================================

Owner-supplied backfill for the macro lake's India index channels BELOW
the static-archive floor (indices_lake only reaches 2019-10; NSE's
`ind_close_all` archive doesn't exist before that). The owner downloads
per-year CSVs from NSE's Historical Index Data tool
(nseindia.com/reports-indices-historical-index-data — the ONLY source of
pre-2019 index history, and it bot-blocks scripted access, so a human
download is the only door) and drops them anywhere; this clerk ingests
the whole folder.

Why a NEW clerk and not indices_lake: indices_lake is append-only
(max-date rule — it only grows FORWARD). This history extends the lake
BACKWARD, so it MERGES: union by date, older gaps filled, and a date
already stored is NEVER rewritten (the stored value wins — forward data
already flowed through the live pipeline and is authoritative).

Export format (verified 1999-2019, byte-identical across 20 years):
    Date ,Open ,High ,Low ,Close ,Shares Traded ,Turnover (₹ Cr)
    31-DEC-2019,12247.1,12247.1,12151.8,12168.45,426931711,14812.89
  * DD-MON-YYYY (uppercase month), descending; close is column 4.
  * Filename carries the index: `NIFTY 50-01-01-2018-to-30-12-2018.csv`,
    `India VIX-...`, `NIFTY BANK-...` — mapped to the lake KEY via the
    indices_lake roster (case-insensitive). An unmapped index is a NAMED
    skip, never a guess.

NULL-honest ('-'/blank close -> None -> stored empty), atomic write,
per-file fail-open (one bad file never aborts the folder). Read-only on
all trade state. Mac lane (the owner's Downloads is the drop folder).

CLI: python3 -m src.ingestion.index_history --folder ~/Downloads [--dry-run]
"""
import json
import re
from datetime import date
from pathlib import Path

from src.ingestion import macro_lake as ML
from src.ingestion.indices_lake import INDEX_MAP

ROOT = Path(__file__).resolve().parent.parent.parent
REPORT_LOG = ROOT / "logs" / "index_history.jsonl"

# lowercased NSE display name -> lake KEY (reuse the indices_lake roster).
_NAME_TO_KEY = {name.lower(): key for key, name in INDEX_MAP.items()}

# "<Index Name>-DD-MM-YYYY-to-DD-MM-YYYY.csv" -> the index name.
# An optional " (N)" is the browser's duplicate-download suffix — the
# same index+range re-downloaded; it maps to the same key and merges
# idempotently (adds 0), so match it rather than skip it as a scary miss.
_FNAME_RE = re.compile(
    r"^(?P<name>.+?)-\d{2}-\d{2}-\d{4}-to-\d{2}-\d{2}-\d{4}"
    r"(?: \(\d+\))?\.csv$",
    re.IGNORECASE)

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

_CLOSE_COL = 4          # Date,Open,High,Low,Close,...


def index_key_from_filename(filename: str):
    """The lake KEY a drop-folder file belongs to, or None (an index we
    don't track / an unrecognized name — a NAMED skip upstream)."""
    m = _FNAME_RE.match(Path(filename).name)
    if not m:
        return None
    return _NAME_TO_KEY.get(m.group("name").strip().lower())


def _parse_ddmonyyyy(raw: str):
    """'01-NOV-1999' -> '1999-11-01', locale-independent. None if it
    isn't that shape (header row / junk)."""
    parts = (raw or "").strip().split("-")
    if len(parts) != 3:
        return None
    d, mon, y = parts
    month = _MONTHS.get(mon.strip().upper())
    if month is None or not (d.isdigit() and y.isdigit()):
        return None
    try:
        return date(int(y), month, int(d)).isoformat()
    except ValueError:
        return None


def parse_export(raw) -> list:
    """NSE historical-index CSV -> [(iso_date, close|None)] in file order.
    A '-'/blank close is a NULL-honest hole (None), never 0.0 or a
    dropped row; header and short rows are skipped."""
    text = raw.decode("utf-8", errors="replace") if isinstance(
        raw, (bytes, bytearray)) else str(raw)
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cols = line.split(",")
        if len(cols) <= _CLOSE_COL:
            continue
        iso = _parse_ddmonyyyy(cols[0])
        if iso is None:
            continue                      # header or junk
        rows.append((iso, ML._value(cols[_CLOSE_COL])))
    return rows


def merge_into_lake(key: str, new_rows: list, lake_dir=None,
                    dry_run: bool = False) -> dict:
    """Union `new_rows` into data/lake/macro/<KEY>.csv by date. A date
    already stored is NEVER overwritten (stored wins); only genuinely
    new dates are added. The merged file is written sorted-ascending,
    atomically. Returns a named summary."""
    root = Path(lake_dir) if lake_dir else ML.MACRO_LAKE
    path = root / f"{key}.csv"
    stored = dict(ML.parse_lake_csv(path.read_text())) if path.is_file() else {}
    before = len(stored)
    added, overlap = 0, 0
    for d, v in new_rows:
        if d in stored:
            overlap += 1                  # stored wins — never rewrite
        else:
            stored[d] = v
            added += 1
    if added and not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        body = ML.HEADER + "\n" + "".join(
            f"{d},{'' if v is None else v}\n"
            for d, v in sorted(stored.items()))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body)
        tmp.replace(path)
    merged = sorted(stored)
    return {"key": key, "rows_in_file": len(new_rows), "added": added,
            "overlap_kept": overlap, "stored_before": before,
            "stored_after": len(stored),
            "floor": merged[0] if merged else None,
            "ceiling": merged[-1] if merged else None}


def ingest_folder(folder, lake_dir=None, dry_run: bool = False,
                  log_path=None) -> dict:
    """Every NSE historical-index CSV in `folder` -> the macro lake.
    Per-file fail-open; unmapped/unparseable files are NAMED skips. A
    file's index is read from its NAME (the export has no index column)."""
    folder = Path(folder).expanduser()
    ok, skipped, per_file = [], [], []
    for csv in sorted(folder.glob("*.csv")):
        key = index_key_from_filename(csv.name)
        if key is None:
            skipped.append({"file": csv.name, "why": "unmapped index / "
                            "not a historical-index export"})
            continue
        try:
            rows = parse_export(csv.read_bytes())
            if not rows:
                skipped.append({"file": csv.name, "why": "zero dated rows"})
                continue
            summary = merge_into_lake(key, rows, lake_dir=lake_dir,
                                      dry_run=dry_run)
            summary["file"] = csv.name
            per_file.append(summary)
            ok.append(csv.name)
        except Exception as exc:            # one bad file never aborts
            skipped.append({"file": csv.name,
                            "detail": f"{type(exc).__name__}: {exc}"[:200]})

    # per-KEY roll-up (a key may span several yearly files)
    by_key = {}
    for s in per_file:
        k = by_key.setdefault(s["key"], {"added": 0, "files": 0,
                                         "floor": None, "ceiling": None})
        k["added"] += s["added"]
        k["files"] += 1
        k["floor"] = min(x for x in (k["floor"], s["floor"]) if x) \
            if (k["floor"] or s["floor"]) else None
        k["ceiling"] = max(x for x in (k["ceiling"], s["ceiling"]) if x) \
            if (k["ceiling"] or s["ceiling"]) else None
    result = {"as_of": ML._now_iso(), "folder": str(folder),
              "ingested_files": ok, "skipped": skipped,
              "by_key": by_key, "dry_run": bool(dry_run)}
    if not dry_run and (ok or skipped):
        p = Path(log_path) if log_path else REPORT_LOG
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                fh.write(json.dumps(result, default=str) + "\n")
        except OSError:
            pass
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", type=str, default="~/Downloads")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = ingest_folder(args.folder, dry_run=args.dry_run)
    print(json.dumps(out, indent=2, default=str))

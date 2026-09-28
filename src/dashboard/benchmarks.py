# MANUAL OFFLINE TOOL — off-cron by itself; `scripts/publish_dashboard_mirror.sh`
# runs it before each push so the box gets a fresh file (decision #119).
"""
src/dashboard/benchmarks.py — passive-alternative series for the Compounding chart
================================================================================
Decision #119 (2026-09-28). Three optional lines beside PAPER_10L's curve,
each starting at the contributed capital (₹10,00,000) on the ₹10L base date
(2026-08-07) so the engine can be read against what doing nothing would
have paid:

  nifty50   NSE's own NIFTY 50 daily CLOSE from the macro lake
            (`data/lake/macro/NIFTY.csv`, `date,value`, ingested nightly by
            `ingestion.indices_lake`).
  gold      GOLDBEES (Nippon India Gold ETF) daily close from the NSE
            bhavcopy lake (`data/lake/bhavcopy/<date>.csv`). Chosen over the
            MCX GOLD future in `cross_asset`: the future rolls bi-monthly
            (a chained series would carry roll gaps) and had no 07-Aug row;
            the ETF is what a rupee investor would actually have bought.
  fd_7pct   a fixed deposit at 7.0% p.a., compounded DAILY:
            base × (1 + 0.07/365)^days.

`build()` runs ON THE TRADING VM (reads the lakes, no network) and writes
`data/dashboard_benchmarks.json` = {as_of, epoch, sources, series:
{nifty50: [[date, close], …], gold: [[date, close], …]}} — raw closes only.
`normalize()` (pure) turns closes into rupee values for the chart; the
dashboard box calls it on the mirrored file. A missing lake yields an empty
series and a named `note`, never an invented price.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = "2026-08-07"                          # the ₹10L base (decisions #116/#119)
FD_RATE = 0.07
OUT_PATH = ROOT / "data" / "dashboard_benchmarks.json"
MACRO_DIR = ROOT / "data" / "lake" / "macro"
BHAV_DIR = ROOT / "data" / "lake" / "bhavcopy"
SOURCES = {
    "nifty50": "NIFTY 50 index close — NSE all-indices archive via ingestion.indices_lake (data/lake/macro/NIFTY.csv)",
    "gold": "GOLDBEES ETF close — NSE bhavcopy lake (data/lake/bhavcopy)",
    "fd_7pct": "fixed deposit, 7.0% p.a. compounded daily (synthetic)",
}


def _nifty_closes(macro_dir=None, epoch: str = EPOCH) -> list:
    path = Path(macro_dir or MACRO_DIR) / "NIFTY.csv"
    out = []
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                d, v = (row.get("date") or "").strip(), (row.get("value") or "").strip()
                if d >= epoch and v:
                    try:
                        out.append([d, float(v)])
                    except ValueError:
                        continue
    except OSError:
        return []
    return sorted(out)


def _bhav_close(path: Path, symbol: str) -> float | None:
    """One symbol's EQ close from one NSE bhavcopy day file (comma+space
    separated, header names located by name). None when absent."""
    try:
        with open(path, newline="") as f:
            reader = csv.reader(f, skipinitialspace=True)
            header = [h.strip().upper() for h in next(reader)]
            si, se, ci = header.index("SYMBOL"), header.index("SERIES"), header.index("CLOSE_PRICE")
            for cols in reader:
                if len(cols) > ci and cols[si].strip() == symbol and cols[se].strip() == "EQ":
                    return float(cols[ci])
    except (OSError, ValueError, StopIteration):
        return None
    return None


def _gold_closes(bhav_dir=None, epoch: str = EPOCH, symbol: str = "GOLDBEES") -> list:
    root = Path(bhav_dir or BHAV_DIR)
    out = []
    try:
        names = sorted(p for p in os.listdir(root) if p.endswith(".csv"))
    except OSError:
        return []
    for name in names:
        d = name[:-4]
        if d < epoch:
            continue
        c = _bhav_close(root / name, symbol)
        if c is not None:
            out.append([d, c])
    return out


def build(macro_dir=None, bhav_dir=None, epoch: str = EPOCH, now: datetime = None) -> dict:
    """Raw daily closes since `epoch` for the two market benchmarks."""
    now = now or datetime.now(IST)
    nifty, gold = _nifty_closes(macro_dir, epoch), _gold_closes(bhav_dir, epoch)
    notes = {}
    if not nifty:
        notes["nifty50"] = "no NIFTY close in the macro lake on/after the epoch"
    if not gold:
        notes["gold"] = "no GOLDBEES close in the bhavcopy lake on/after the epoch"
    return {"as_of": now.isoformat(timespec="seconds"), "epoch": epoch, "sources": SOURCES,
            "series": {"nifty50": nifty, "gold": gold}, "notes": notes}


def write(payload: dict, path=None) -> Path:
    path = Path(path) if path is not None else OUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, path)
    return path


def _rebased(closes: list, base_rs: float) -> list:
    """[[date, close]] → [{ts, value}]: value = base × close / first close,
    the first close being the first on/after the epoch. Stamped at the
    15:30 IST session close so the point sits where the day ended."""
    if not closes:
        return []
    first = float(closes[0][1])
    if first <= 0:
        return []
    return [{"ts": f"{d}T15:30:00", "value": round(base_rs * float(c) / first, 2)}
            for d, c in closes]


def fd_series(base_rs: float, epoch: str, until: date, rate: float = FD_RATE) -> list:
    """Daily-compounded FD: one point per calendar day from the epoch."""
    start = date.fromisoformat(epoch)
    if until < start:
        return []
    daily = 1.0 + rate / 365.0
    return [{"ts": f"{(start + timedelta(days=n)).isoformat()}T15:30:00",
             "value": round(base_rs * daily ** n, 2)}
            for n in range((until - start).days + 1)]


def normalize(raw: dict | None, base_rs: float, until: date, epoch: str = EPOCH) -> dict:
    """The chart payload: {base, epoch, as_of, sources, notes, series:
    {nifty50, gold, fd_7pct: [{ts, value}]}}. `raw` = build()'s output (or
    None: only the FD line, the two market lines empty and noted)."""
    raw = raw if isinstance(raw, dict) else {}
    series = raw.get("series") or {}
    notes = dict(raw.get("notes") or {})
    out = {"base": float(base_rs), "epoch": raw.get("epoch") or epoch, "as_of": raw.get("as_of"),
           "sources": dict(SOURCES), "series": {}}
    for key in ("nifty50", "gold"):
        pts = _rebased(list(series.get(key) or []), float(base_rs))
        out["series"][key] = pts
        if not pts and key not in notes:
            notes[key] = "no benchmark file on the mirror yet"
    out["series"]["fd_7pct"] = fd_series(float(base_rs), out["epoch"], until)
    out["notes"] = notes
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    payload = build()
    path = write(payload, argv[0] if argv else None)
    n, g = len(payload["series"]["nifty50"]), len(payload["series"]["gold"])
    print(f"[benchmarks] {path} nifty50={n} gold={g} notes={payload['notes'] or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

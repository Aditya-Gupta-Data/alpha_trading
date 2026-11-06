# MANUAL OFFLINE TOOL — historical NSE F&O bhavcopy (UDiFF) for the option P&L simulator (branch feature/option-pnl-sim).
# One static archive file per trading day, 3-5 s apart (one NSE job at a time). Keeps only the desk's underlyings and
# the columns the simulator needs, gzipped, under data/backtest/fo_history/YYYY-MM-DD.csv.gz. Resumable; a holiday or a
# missing file is a named miss, never fatal.
#   python3 scripts/backfill_fo_history.py --start 2024-01-01 --end 2026-11-06 [--symbols NIFTY,BANKNIFTY,...]
import argparse
import csv
import gzip
import io
import random
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "backtest" / "fo_history"
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
DEFAULT_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS")
KEEP_TYPES = {"IDO", "STO", "IDF", "STF"}
COLUMNS = ("TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric", "HghPric", "LwPric",
           "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst", "TtlTradgVol", "NewBrdLotQty")
PAUSE = (3.0, 5.0)


def filter_rows(text: str, symbols) -> list:
    keep = set(symbols)
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("FinInstrmTp") in KEEP_TYPES and r.get("TckrSymb") in keep:
            out.append({c: (r.get(c) or "").strip() for c in COLUMNS})
    return out


def fetch_day(d: date, symbols, out_dir=None, fetch_bytes=None) -> str:
    """'ok' | 'exists' | 'weekend' | 'missing' for one day."""
    out = Path(out_dir or OUT_DIR) / f"{d.isoformat()}.csv.gz"
    if out.exists():
        return "exists"
    if d.weekday() >= 5:
        return "weekend"
    if fetch_bytes is None:
        from src.ingestion.report_downloader import _fetch_bytes as fetch_bytes
    try:
        blob = fetch_bytes(URL.format(ymd=d.strftime("%Y%m%d")))
        z = zipfile.ZipFile(io.BytesIO(blob))
        rows = filter_rows(z.read(z.namelist()[0]).decode("utf-8", errors="replace"), symbols)
    except Exception:
        return "missing"
    if not rows:
        return "missing"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with gzip.open(tmp, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    tmp.rename(out)
    return "ok"


def backfill(start: date, end: date, symbols=DEFAULT_SYMBOLS, out_dir=None, fetch_bytes=None, sleep=time.sleep) -> dict:
    counts = {}
    d = start
    while d <= end:
        status = fetch_day(d, symbols, out_dir, fetch_bytes)
        counts[status] = counts.get(status, 0) + 1
        if status in ("ok", "missing"):
            print(f"  {d} {status}", flush=True)
            sleep(random.uniform(*PAUSE))
        d += timedelta(days=1)
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    a = ap.parse_args(argv)
    print(backfill(date.fromisoformat(a.start), date.fromisoformat(a.end), a.symbols.split(",")))


if __name__ == "__main__":
    main()

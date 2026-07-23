#!/usr/bin/env python3
"""
scripts/fetch_pre2019_sectors.py — Stage A: close the pre-2019 sector-history gap
=================================================================================

The Strategy Registry's sector-rotation recipes ABSTAIN today because six
NIFTY sector indices only reach back to 2019-10 in our macro lake, while the
crisis episodes they need as analogs sit in 2010-2018. NSE's own pre-2019
history bot-blocks every scripted path (that is why `index_history.py` is a
human-download clerk) — but Yahoo Finance is a DIFFERENT provider and DOES
carry these indices, so yfinance reaches where NSE's archive will not.

This script fetches the missing sectors' daily closes from Yahoo, formats them
into the EXACT NSE historical-index export CSV that `index_history.py` (the
robust drop-folder clerk) parses, and drops one file per sector into `drop/`.
Running the clerk over `drop/` then MERGES them backward into the lake (stored
2019+ NSE values always win — Yahoo only fills the gap below the floor).

HONESTY (this is a secondary source, so it says exactly what it got):
  * Yahoo's coverage begins when each index began (Realty/Infra ~2010-07,
    PSU Bank ~2011-01, Media ~2011-08, Fin Services ~2011-09) — earlier
    months simply do not exist and are never fabricated.
  * NIFTY HEALTHCARE has NO pre-2019 history anywhere — the index launched in
    2021. It is a NAMED skip, not a silent omission.
  * Index LEVELS from Yahoo match NSE's official levels closely (same index),
    but this is not NSE's tape; the clerk's stored-wins merge guarantees no
    trusted forward value is ever overwritten.

Requires yfinance (`pip install yfinance`) — a Mac-lane utility, not a VM dep.

CLI: python3 scripts/fetch_pre2019_sectors.py [--start 2010-01-01]
                                              [--end 2018-12-31] [--out drop]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# lake KEY -> (Yahoo ticker, NSE display name the clerk maps by filename).
# The display name MUST equal indices_lake.INDEX_MAP[key] exactly (case-
# insensitive) or the clerk NAME-skips the file.
SECTORS = {
    "NIFTY_REALTY":      ("^CNXREALTY",          "Nifty Realty"),
    "NIFTY_PSU_BANK":    ("^CNXPSUBANK",         "Nifty PSU Bank"),
    "NIFTY_MEDIA":       ("^CNXMEDIA",           "Nifty Media"),
    "NIFTY_INFRA":       ("^CNXINFRA",           "Nifty Infrastructure"),
    "NIFTY_FIN_SERVICE": ("NIFTY_FIN_SERVICE.NS", "Nifty Financial Services"),
}
# Real, documented impossibility — kept here so the gap is NAMED, not hidden.
NO_PRE2019_DATA = {
    "NIFTY_HEALTHCARE": "Nifty Healthcare Index launched 2021 — no 2010-2018 "
                        "history exists at any source.",
}

CSV_HEADER = "Date ,Open ,High ,Low ,Close ,Shares Traded ,Turnover (Cr)"


def _fmt_date(ts):
    """pandas Timestamp -> NSE's DD-MON-YYYY (uppercase month)."""
    return ts.strftime("%d-%b-%Y").upper()


def _rows_to_csv(df):
    """A yfinance OHLCV frame -> the NSE export CSV text (descending by date,
    as NSE ships it; the clerk is order-independent but we match the format)."""
    if hasattr(df.columns, "get_level_values"):        # flatten MultiIndex
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    lines = [CSV_HEADER]
    for ts, r in df.sort_index(ascending=False).iterrows():
        close = r.get("Close")
        if close is None or close != close:            # NaN close -> skip row
            continue
        o, h, l = r.get("Open"), r.get("High"), r.get("Low")
        vol = r.get("Volume")

        def n(x, dp=2):
            return "" if (x is None or x != x) else f"{float(x):.{dp}f}"

        lines.append(f"{_fmt_date(ts)},{n(o)},{n(h)},{n(l)},{n(close)},"
                     f"{n(vol, 0)},")
    return "\n".join(lines) + "\n"


def fetch_all(start, end, out_dir):
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance not installed — run: pip install yfinance")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # filename range tag from the requested window (clerk parses DD-MM-YYYY)
    a = "".join(reversed(start.split("-")))            # 2010-01-01 -> 01012010
    b = "".join(reversed(end.split("-")))
    tag = f"{a[:2]}-{a[2:4]}-{a[4:]}-to-{b[:2]}-{b[2:4]}-{b[4:]}"

    report = []
    for key, (ticker, name) in SECTORS.items():
        try:
            df = yf.download(ticker, start=start, end=end, progress=False,
                             auto_adjust=False)
        except Exception as exc:
            report.append((key, "ERROR", f"{type(exc).__name__}: {exc}"[:80]))
            continue
        if df is None or len(df) == 0:
            report.append((key, "EMPTY", f"{ticker} returned no rows"))
            continue
        path = out / f"{name}-{tag}.csv"
        path.write_text(_rows_to_csv(df))
        report.append((key, "OK",
                       f"{len(df)} rows {df.index.min().date()}..{df.index.max().date()}"
                       f" -> {path.name}"))

    for key, why in NO_PRE2019_DATA.items():
        report.append((key, "SKIP", why))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default="2018-12-31")
    ap.add_argument("--out", default=str(ROOT / "drop"))
    args = ap.parse_args()

    print(f"Fetching pre-2019 sector closes {args.start} .. {args.end} "
          f"-> {args.out}/\n")
    report = fetch_all(args.start, args.end, args.out)
    for key, status, detail in report:
        mark = {"OK": "✓", "SKIP": "—", "EMPTY": "∅", "ERROR": "✗"}.get(status, "?")
        print(f"  {mark} {key:20s} {status:6s} {detail}")
    got = sum(1 for _, s, _ in report if s == "OK")
    print(f"\n{got}/{len(SECTORS)} sectors written to {args.out}/. Next: ingest with")
    print(f"  python3 -m src.ingestion.index_history --folder {args.out}")


if __name__ == "__main__":
    main()

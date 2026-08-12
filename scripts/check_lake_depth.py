# MANUAL OFFLINE TOOL
"""
scripts/check_lake_depth.py — how many COMPLETE multi-layer days do we have?
============================================================================

READ-ONLY DIAGNOSTIC. Opens every store `O_RDONLY` (SQLite included, via a
`file:...?mode=ro` URI), writes nothing, and imports nothing from `src/`.
Safe to run on a live box mid-session.

THE QUESTION IT ANSWERS: before deciding "wait for 60 organic sessions" vs
"attempt a historical backfill", we need the depth of the data we already
hold — not per layer, which is easy and misleading, but the INTERSECTION,
which is the only number a multi-layer model can actually train on.

THE TRAP IT EXISTS TO EXPOSE — RAGGED MISSINGNESS. Layer depths here differ
by ORDERS OF MAGNITUDE: bhavcopy reaches back to 2019, F&O bhavcopy only to
2026-07. Take the naive intersection across all layers and the answer is
whatever the SHALLOWEST layer holds. Worse, drop incomplete rows and you
have silently conditioned the sample on "days the newest clerk happened to
be running" — a selection rule correlated with deploys, outages and
weekends, i.e. with exactly the market conditions you are trying to measure.
The resulting n looks like data and behaves like a bias.

So this tool never prints one number. It prints three, and the gap between
them IS the finding:

  * UNION      — dates present in at least one layer (the outer bound)
  * COMPLETE   — dates where EVERY tracked layer is present (naive; this is
                 the number that gets quoted and is almost always wrong)
  * IN-WINDOW  — completeness measured only inside the overlap window
                 (max of per-layer starts → min of per-layer ends). Holes
                 HERE are real outages. Holes outside it are just a layer
                 that had not been built yet, which is not missingness at
                 all and must never be imputed.

HOST MATTERS, AND THE TOOL SAYS SO. This repo runs on two machines with a
deliberately uneven split (the Mac is analysis-side; the VM carries the
live ingestion crons). A layer absent here may be healthy there. Absent
layers are therefore reported as ABSENT and EXCLUDED from the intersection
by default — counting them as "missing every day" would turn a host
boundary into a fake data emergency. `--strict` includes them, for when you
are deliberately auditing one host.

CLI:
  python3 scripts/check_lake_depth.py            # table + verdict
  python3 scripts/check_lake_depth.py --json     # machine-readable
  python3 scripts/check_lake_depth.py --strict   # count absent layers as missing
  python3 scripts/check_lake_depth.py --since 2026-01-01
"""
import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LAKE = DATA / "lake"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------- layers
# kind:
#   flat_file  dir of <YYYY-MM-DD>.<ext> files
#   dir_date   dir of <YYYY-MM-DD>/ subdirectories
#   partition  dir of date=<YYYY-MM-DD>/ subdirectories (the lake layout)
#   csv_dates  a CSV whose first column is a date
#   sqlite     a table + date column, opened read-only
LAYERS = [
    # --- prices / indices -------------------------------------------------
    {"key": "bhavcopy", "label": "Bhavcopy (cash prices)", "core": True,
     "kind": "flat_file", "path": LAKE / "bhavcopy"},
    {"key": "macro_nifty", "label": "Index history (NIFTY)", "core": True,
     "kind": "csv_dates", "path": LAKE / "macro" / "NIFTY.csv"},
    {"key": "macro_vix", "label": "India VIX", "core": False,
     "kind": "csv_dates", "path": LAKE / "macro" / "INDIAVIX.csv"},
    {"key": "macro_usdinr", "label": "Macro global (USDINR)", "core": False,
     "kind": "csv_dates", "path": LAKE / "macro" / "USDINR.csv"},
    # --- derivatives ------------------------------------------------------
    {"key": "fo_bhavcopy", "label": "F&O bhavcopy", "core": True,
     "kind": "dir_date", "path": LAKE / "fo_bhavcopy"},
    {"key": "chains", "label": "Option chains", "core": True,
     "kind": "partition_parent", "path": LAKE / "chains"},
    # --- flow / events ----------------------------------------------------
    {"key": "deals_census", "label": "Bulk/block deals", "core": True,
     "kind": "partition", "path": LAKE / "deals_census"},
    {"key": "flows", "label": "FII/DII flows", "core": True,
     "kind": "partition", "path": LAKE / "flows"},
    {"key": "events", "label": "Corporate events", "core": True,
     "kind": "partition", "path": LAKE / "events"},
    {"key": "earnings", "label": "Earnings calendar", "core": False,
     "kind": "partition", "path": LAKE / "earnings"},
    # --- text / sentiment -------------------------------------------------
    {"key": "news_daily", "label": "News sentiment (daily)", "core": True,
     "kind": "partition", "path": LAKE / "news_daily"},
    {"key": "rss", "label": "RSS signals", "core": False,
     "kind": "partition", "path": LAKE / "rss"},
    # --- cross-asset ------------------------------------------------------
    {"key": "cross_asset", "label": "Cross-asset (MCX/global)", "core": False,
     "kind": "partition", "path": LAKE / "cross_asset"},
    # --- derived context --------------------------------------------------
    {"key": "daily_context", "label": "Brain daily context", "core": False,
     "kind": "sqlite", "path": DATA / "brain_map.db",
     "table": "daily_context", "column": "date"},
]


def _dates_flat_file(p):
    return {f.name[:10] for f in p.iterdir()
            if f.is_file() and DATE_RE.match(f.name[:10])}


def _dates_dir_date(p):
    return {d.name for d in p.iterdir() if d.is_dir() and DATE_RE.match(d.name)}


def _dates_partition(p):
    return {d.name[5:] for d in p.iterdir()
            if d.is_dir() and d.name.startswith("date=")
            and DATE_RE.match(d.name[5:])}


def _dates_partition_parent(p):
    """chains/<slug>/date=.../ — a day counts if ANY underlying archived."""
    out = set()
    for sub in p.iterdir():
        if sub.is_dir():
            out |= _dates_partition(sub)
    return out


def _dates_csv(p):
    out = set()
    with p.open(newline="") as fh:
        for row in csv.reader(fh):
            if row and DATE_RE.match(row[0]):
                out.add(row[0])
    return out


def _dates_sqlite(spec):
    uri = f"file:{spec['path']}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT substr({spec['column']},1,10) "
            f"FROM {spec['table']}").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if r[0] and DATE_RE.match(r[0])}


READERS = {"flat_file": _dates_flat_file, "dir_date": _dates_dir_date,
           "partition": _dates_partition,
           "partition_parent": _dates_partition_parent, "csv_dates": _dates_csv}


def collect(layers=None, since=None):
    """{key: {...}} — never raises; an unreadable layer is reported, not fatal."""
    out = {}
    for spec in (layers or LAYERS):
        rec = {"key": spec["key"], "label": spec["label"],
               "core": spec["core"], "path": str(spec["path"]),
               "present": spec["path"].exists(), "dates": set(), "error": None}
        if rec["present"]:
            try:
                if spec["kind"] == "sqlite":
                    rec["dates"] = _dates_sqlite(spec)
                else:
                    rec["dates"] = READERS[spec["kind"]](spec["path"])
            except Exception as exc:                       # never fatal
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        if since:
            rec["dates"] = {d for d in rec["dates"] if d >= since}
        rec["n"] = len(rec["dates"])
        rec["first"] = min(rec["dates"]) if rec["dates"] else None
        rec["last"] = max(rec["dates"]) if rec["dates"] else None
        out[spec["key"]] = rec
    return out


def analyse(coll, strict=False, core_only=True):
    """Union / naive-complete / in-window-complete + the bottleneck ranking."""
    live = [r for r in coll.values() if r["n"] > 0 or (strict and r["present"])]
    if core_only:
        live = [r for r in live if r["core"]] or live
    counted = [r for r in live if r["n"] > 0 or strict]
    excluded = [r for r in coll.values() if r not in counted]

    union = set()
    for r in counted:
        union |= r["dates"]

    complete = set(union)
    for r in counted:
        complete &= r["dates"]

    # the overlap window: where EVERY counted layer had started and not ended
    starts = [r["first"] for r in counted if r["first"]]
    ends = [r["last"] for r in counted if r["last"]]
    win_lo = max(starts) if starts else None
    win_hi = min(ends) if ends else None
    in_window = {d for d in union if win_lo and win_hi and win_lo <= d <= win_hi}
    complete_win = {d for d in in_window if all(d in r["dates"] for r in counted)}

    # bottleneck: who is absent on the most union-days, and in-window
    miss = Counter()
    miss_win = Counter()
    for r in counted:
        miss[r["key"]] = len(union - r["dates"])
        miss_win[r["key"]] = len(in_window - r["dates"])

    return {
        "counted": [r["key"] for r in counted],
        "excluded": [{"key": r["key"], "label": r["label"],
                      "reason": ("absent on this host" if not r["present"]
                                 else r["error"] if r["error"]
                                 else "no dated rows" if not r["n"]
                                 else "non-core (use --all-layers)")}
                     for r in excluded],
        "union": len(union), "union_first": min(union) if union else None,
        "union_last": max(union) if union else None,
        "complete": len(complete), "ragged": len(union) - len(complete),
        "window": [win_lo, win_hi], "in_window": len(in_window),
        "complete_in_window": len(complete_win),
        "ragged_in_window": len(in_window) - len(complete_win),
        "missing_by_layer": miss.most_common(),
        "missing_by_layer_in_window": miss_win.most_common(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="lake depth + ragged-missingness")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="count host-absent layers as missing every day")
    ap.add_argument("--all-layers", action="store_true",
                    help="intersect non-core layers too")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    coll = collect(since=a.since)
    rep = analyse(coll, strict=a.strict, core_only=not a.all_layers)

    if a.json:
        print(json.dumps({"layers": {k: {kk: vv for kk, vv in v.items()
                                         if kk != "dates"}
                                     for k, v in coll.items()},
                          "analysis": rep}, indent=2))
        return 0

    print(f"\nLAKE DEPTH — host {Path.home().name}, root {ROOT}")
    print("=" * 78)
    print(f"{'layer':<26}{'days':>7}  {'first':<12}{'last':<12}status")
    print("-" * 78)
    for r in coll.values():
        if not r["present"]:
            status = "ABSENT on this host"
        elif r["error"]:
            status = f"UNREADABLE {r['error']}"
        elif not r["n"]:
            status = "present, no dated rows"
        else:
            status = "counted" if r["key"] in rep["counted"] else "not intersected"
        tag = "*" if r["core"] else " "
        print(f"{tag}{r['label']:<25}{r['n']:>7}  {str(r['first'] or '-'):<12}"
              f"{str(r['last'] or '-'):<12}{status}")
    print("-" * 78)
    print("* = core layer (intersected by default)\n")

    print(f"UNION      {rep['union']:>6} dates  "
          f"({rep['union_first']} → {rep['union_last']})")
    print(f"COMPLETE   {rep['complete']:>6} dates  all counted layers present")
    print(f"RAGGED     {rep['ragged']:>6} dates  at least one layer missing")
    print()
    print(f"OVERLAP WINDOW  {rep['window'][0]} → {rep['window'][1]}")
    print(f"  in-window     {rep['in_window']:>6} dates")
    print(f"  complete      {rep['complete_in_window']:>6} dates  "
          "<-- the only honest n for a multi-layer model")
    print(f"  ragged        {rep['ragged_in_window']:>6} dates  "
          "real outages, not un-built layers")
    print()
    print("MISSINGNESS BY LAYER (days absent / union):")
    for k, v in rep["missing_by_layer"]:
        print(f"  {k:<20}{v:>7} of {rep['union']}")
    print("\nMISSINGNESS IN-WINDOW (the only holes that are real outages):")
    for k, v in rep["missing_by_layer_in_window"]:
        print(f"  {k:<20}{v:>7} of {rep['in_window']}")
    if rep["excluded"]:
        print("\nEXCLUDED from the intersection:")
        for e in rep["excluded"]:
            print(f"  {e['key']:<20} {e['reason']}")
        print("  (--strict counts these as missing on every day)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

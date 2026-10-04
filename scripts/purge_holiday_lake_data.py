# MANUAL OFFLINE TOOL — run by hand on the VM, market CLOSED (ledger Issue 41, owner ruling 2026-10-05).
#   venv/bin/python scripts/purge_holiday_lake_data.py --dates 2026-09-14 2026-10-02          # DRY RUN
#   venv/bin/python scripts/purge_holiday_lake_data.py --dates 2026-09-14 2026-10-02 --yes    # archive, then delete
"""
PURGE the price data the engine captured on NSE holidays.

Before the holiday calendar (decision #124) the capture jobs ran on exchange
holidays and wrote the previous session's prices under the holiday's date:
flat candles, an unchanged option chain, a repeated close. Left in the lake,
each one is a zero-return "session" in any volatility or range calculation
that walks the partitions. The owner ruled: delete them, do not label them.

WHAT IS PURGED, for each given date (which MUST be a listed NSE holiday):
  data/lake/intraday_15m/date=D/          the 15-minute equity price sweep
  data/lake/darlings_daily/date=D/        the daily all-darlings tap
  data/lake/chains/<slug>/date=D/         the post-close option-chain archive
  data/lake/candles/<slug>/date=D/        the live loop's 15-minute candles
  data/lake/pricer_journal.jsonl          rows whose `as_of` is D (the pricer
                                          re-logged the last real close)

WHAT IS NOT: datasets that are not NSE session prices — cross_asset (MCX and
global bars; real on an NSE holiday), macro_daily (a calendar-day snapshot),
news_daily, events, earnings, deals — and every ledger outside the lake.

Before anything is deleted, every target directory and every removed row is
written to ONE archive OUTSIDE the lake
(data/purged_holiday_data/holiday_lake_purge_<stamp>.tar.gz) and the archive
is re-opened and its member count checked. Nothing in src/ reads that
folder. Idempotent: a second run finds nothing to purge.
"""
import argparse
import io
import json
import os
import shutil
import sys
import tarfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import nse_calendar  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
FLAT_DATASETS = ("intraday_15m", "darlings_daily")       # <lake>/<dataset>/date=D
NESTED_DATASETS = ("chains", "candles")                  # <lake>/<dataset>/<slug>/date=D
ROW_FILES = {"pricer_journal.jsonl": "as_of"}            # <lake>/<file>: rows whose field == D
ARCHIVE_DIR_NAME = "purged_holiday_data"


def _dir_stats(path: Path) -> tuple:
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def build_plan(lake_root: Path, dates: list) -> dict:
    """{dates, refusals, dirs: [{path, files, bytes}], rows: {file: {date: n}}}. Reads only."""
    plan = {"dates": list(dates), "refusals": [], "dirs": [], "rows": {}}
    for d in dates:
        try:
            day = date.fromisoformat(d)
        except ValueError:
            plan["refusals"].append(f"{d!r} is not a date (YYYY-MM-DD)")
            continue
        if not nse_calendar.is_trading_holiday(day):
            plan["refusals"].append(f"{d} is not a listed NSE holiday — a real session's data is never purged")
    if not lake_root.is_dir():
        plan["refusals"].append(f"no lake at {lake_root}")
    if plan["refusals"]:
        return plan
    for d in dates:
        targets = [lake_root / ds / f"date={d}" for ds in FLAT_DATASETS]
        for ds in NESTED_DATASETS:
            base = lake_root / ds
            if base.is_dir():
                targets += [slug / f"date={d}" for slug in sorted(base.iterdir()) if slug.is_dir()]
        for t in targets:
            if t.is_dir():
                n, size = _dir_stats(t)
                plan["dirs"].append({"path": str(t.relative_to(lake_root)), "files": n, "bytes": size})
    for name, field in ROW_FILES.items():
        path = lake_root / name
        if not path.is_file():
            continue
        counts = {d: 0 for d in dates}
        with open(path, "r", errors="replace") as f:
            for line in f:
                hit = _row_date(line, field, counts)
                if hit:
                    counts[hit] += 1
        if any(counts.values()):
            plan["rows"][name] = counts
    return plan


def _row_date(line: str, field: str, dates) -> str | None:
    """The purge date this row carries in `field`, else None. A line that is
    not a JSON object is never matched (it is kept, byte for byte)."""
    if not any(d in line for d in dates):
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    value = str(row.get(field) or "")[:10] if isinstance(row, dict) else ""
    return value if value in dates else None


def purge(lake_root: Path, dates: list, archive_dir: Path, now: datetime = None) -> dict:
    """Archive, verify the archive, then delete. Returns the summary."""
    plan = build_plan(lake_root, dates)
    if plan["refusals"]:
        return {"purged": False, "reason": "; ".join(plan["refusals"])}
    if not plan["dirs"] and not plan["rows"]:
        return {"purged": False, "reason": "nothing to purge", "dirs": 0, "rows": 0}
    stamp = (now or datetime.now(IST)).strftime("%Y%m%d-%H%M%S")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"holiday_lake_purge_{stamp}.tar.gz"
    removed_rows = {}
    expected_members = 0
    with tarfile.open(archive, "w:gz") as tar:
        for d in plan["dirs"]:
            tar.add(lake_root / d["path"], arcname=d["path"])
            expected_members += d["files"]
        for name, field in ROW_FILES.items():
            if name not in plan["rows"]:
                continue
            with open(lake_root / name, "r", errors="replace") as f:
                removed_rows[name] = [line for line in f if _row_date(line, field, dates)]
            blob = "".join(removed_rows[name]).encode()
            info = tarfile.TarInfo(name=f"removed_rows/{name}")
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
            expected_members += 1
        manifest = json.dumps({"purged_at": stamp, "dates": dates, "plan": plan}, indent=1).encode()
        info = tarfile.TarInfo(name="MANIFEST.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        expected_members += 1
    with tarfile.open(archive, "r:gz") as tar:
        got = sum(1 for m in tar.getmembers() if m.isfile())
    if got != expected_members:
        raise SystemExit(f"archive {archive} holds {got} files, expected {expected_members} — nothing deleted")
    for d in plan["dirs"]:
        shutil.rmtree(lake_root / d["path"])
    rows_removed = 0
    for name, field in ROW_FILES.items():
        if name not in removed_rows:
            continue
        path = lake_root / name
        tmp = path.with_name(path.name + ".purge.tmp")
        with open(path, "r", errors="replace") as src, open(tmp, "w") as dst:
            for line in src:
                if _row_date(line, field, dates):
                    rows_removed += 1
                else:
                    dst.write(line)
        os.replace(tmp, path)
    after = build_plan(lake_root, dates)
    return {"purged": not after["dirs"] and not after["rows"], "archive": str(archive),
            "dirs": len(plan["dirs"]), "files": sum(d["files"] for d in plan["dirs"]),
            "bytes": sum(d["bytes"] for d in plan["dirs"]), "rows": rows_removed,
            "left_dirs": len(after["dirs"]), "left_rows": after["rows"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Purge NSE-holiday price data from the lake (ledger Issue 41).")
    ap.add_argument("--dates", nargs="+", required=True, help="NSE holiday dates, YYYY-MM-DD")
    ap.add_argument("--lake", default=str(ROOT / "data" / "lake"))
    ap.add_argument("--yes", action="store_true", help="execute; without it this is a dry run")
    args = ap.parse_args(argv)
    lake = Path(args.lake)
    from src.market_loop import is_market_open
    plan = build_plan(lake, args.dates)
    if is_market_open():
        plan["refusals"].append("the market is open — a purge is an offline repair")
    by_ds = {}
    for d in plan["dirs"]:
        key = d["path"].split("/")[0] + " " + d["path"].rsplit("date=", 1)[1]
        n, f, b = by_ds.get(key, (0, 0, 0))
        by_ds[key] = (n + 1, f + d["files"], b + d["bytes"])
    print(f"PURGE PLAN for {', '.join(args.dates)} in {lake}:")
    for key in sorted(by_ds):
        n, f, b = by_ds[key]
        print(f"  {key}: {n} partition(s), {f} file(s), {b / 1024:.0f} KB")
    for name, counts in plan["rows"].items():
        print(f"  {name}: rows to remove {counts}")
    if plan["refusals"]:
        print("REFUSED:", *[f"  - {r}" for r in plan["refusals"]], sep="\n")
        return 1
    if not plan["dirs"] and not plan["rows"]:
        print("Nothing to purge.")
        return 0
    if not args.yes:
        print("(dry run — nothing changed; re-run with --yes to archive and delete)")
        return 0
    out = purge(lake, args.dates, lake.parent / ARCHIVE_DIR_NAME)
    print(json.dumps(out, indent=1))
    return 0 if out.get("purged") else 1


if __name__ == "__main__":
    sys.exit(main())

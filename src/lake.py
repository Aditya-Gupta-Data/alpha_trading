"""
src/lake.py — the data lake: date-partitioned, append-only bulk history
=======================================================================

Phase 0 of the unified-brain roadmap (docs/HOLY_GRAIL_PLAN.md §3). The HOT
state rules stand exactly as decided (#19/#25): portfolio.json,
journal.jsonl and brain_map.db remain the small, hand-inspectable, writable
memory. The lake is the COLD side: bulk history (option chains, raw deal
payloads, candles, flows, daily snapshots of perishable JSONs) that would
otherwise be overwritten or thrown away every day.

Doctrine (logged in DECISIONS.md):
  * File-based, local, greppable. Partitions are gzip-JSONL —
    `zcat data/lake/<dataset>/date=YYYY-MM-DD/part.jsonl.gz` works. No
    Parquet, no DuckDB, no server, no new datastore semantics.
  * Layout is the one-way door, locked now:
        data/lake/<dataset>/date=YYYY-MM-DD/<name>.jsonl.gz   (rows)
        data/lake/<dataset>/date=YYYY-MM-DD/<name>.<ext>.gz   (raw blobs)
  * ONLY ingestion writes. Everything else scans. The lake is never
    "memory" — no module may treat it as writable state.
  * Fail-open like every ingestion module: a write failure is logged and
    returns None; a scan over a missing dataset yields nothing. Nothing
    here ever raises to a caller or touches trade state.

Writes are atomic (temp file + os.replace, the market_snapshot pattern) so
a reader never catches a half-written partition. `append_rows` uses gzip
member concatenation (each append is a whole gzip member; readers see one
continuous stream), which keeps intraday taps (candles) cheap without
rewriting the day's file.

Manual check:  python3 -m src.lake            (lists datasets and days)
"""

import gzip
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAKE_ROOT = ROOT / "data" / "lake"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Dataset names may nest one level (e.g. "chains/nifty") but stay simple:
# path-safe tokens only — the layout is a contract, not a filesystem toy.
_DATASET_RE = re.compile(r"^[a-z0-9_\-]+(/[a-z0-9_\-]+)?$")


def _check(dataset: str, day: str) -> bool:
    ok = bool(_DATASET_RE.match(dataset or "") and _DATE_RE.match(day or ""))
    if not ok:
        print(f"  (lake: invalid dataset/day {dataset!r}/{day!r} — skipped)")
    return ok


def partition_dir(dataset: str, day: str, root=None) -> Path:
    root = Path(root) if root is not None else LAKE_ROOT
    return root / dataset / f"date={day}"


# ------------------------------------------------------------- writes

def write_partition(dataset: str, day: str, rows: list, name: str = "part",
                    root=None) -> Path | None:
    """Write (REPLACE) one partition file of JSON rows atomically. The
    write-whole-then-rename shape is for daily EOD jobs whose natural unit
    is 'the day's data' (chains, census, flows). Returns the path, or None
    on any failure (logged, never raised)."""
    if not _check(dataset, day):
        return None
    target = partition_dir(dataset, day, root) / f"{name}.jsonl.gz"
    tmp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            with gzip.GzipFile(fileobj=fh, mode="wb") as gz:
                for row in rows or []:
                    gz.write((json.dumps(row) + "\n").encode("utf-8"))
        os.replace(tmp, target)   # atomic on POSIX — all-or-nothing
        tmp = None
        return target
    except (OSError, TypeError, ValueError) as exc:
        print(f"  (lake: write_partition {dataset}/{day} failed [{exc}])")
        return None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def append_rows(dataset: str, day: str, rows: list, name: str = "part",
                root=None) -> int:
    """Append JSON rows to a partition as a new gzip member — the cheap
    path for intraday taps (candles) where rewriting the day's file per
    event would be wasteful. gzip readers transparently read concatenated
    members as one stream. Returns rows written (0 on failure/no rows)."""
    if not rows or not _check(dataset, day):
        return 0
    target = partition_dir(dataset, day, root) / f"{name}.jsonl.gz"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")
        with open(target, "ab") as fh:
            fh.write(gzip.compress(payload))
        return len(rows)
    except (OSError, TypeError, ValueError) as exc:
        print(f"  (lake: append_rows {dataset}/{day} failed [{exc}])")
        return 0


def archive_blob(dataset: str, day: str, name: str, raw: bytes,
                 ext: str = "raw", root=None) -> dict | None:
    """Immutably archive one raw payload (e.g. the exact bytes NSE served)
    with its sha256, so a silent upstream revision shows as a hash change
    instead of history rewriting itself. Same-content re-archives are
    no-ops. Returns {"path", "sha256", "bytes"} or None on failure."""
    if not _check(dataset, day):
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    target = partition_dir(dataset, day, root) / f"{name}.{ext}.gz"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                with gzip.open(target, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() == digest:
                        return {"path": target, "sha256": digest,
                                "bytes": len(raw)}   # unchanged — keep first
                # Content CHANGED for the same day/name: keep both — stamp
                # the new one with its hash prefix so the revision is loud.
                target = target.with_name(f"{name}.rev-{digest[:12]}.{ext}.gz")
            except OSError:
                pass
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                with gzip.GzipFile(fileobj=fh, mode="wb") as gz:
                    gz.write(raw)
            os.replace(tmp, target)
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return {"path": target, "sha256": digest, "bytes": len(raw)}
    except OSError as exc:
        print(f"  (lake: archive_blob {dataset}/{day}/{name} failed [{exc}])")
        return None


# ------------------------------------------------------------- reads

def list_days(dataset: str, root=None) -> list:
    """Sorted YYYY-MM-DD partition days present for a dataset ([] if none)."""
    root = Path(root) if root is not None else LAKE_ROOT
    base = root / dataset
    if not base.is_dir():
        return []
    days = []
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("date="):
            day = child.name[len("date="):]
            if _DATE_RE.match(day):
                days.append(day)
    return sorted(days)


def scan(dataset: str, start: str = None, end: str = None, name: str = None,
         root=None):
    """Iterate (day, row) over a dataset's partitions in date order,
    optionally bounded [start, end] inclusive (lexical compare is valid for
    ISO dates) and filtered to one partition file `name`. Malformed lines
    and unreadable files are skipped, never fatal — mirrors
    deals_tracker.read_deal_history's discipline."""
    for day in list_days(dataset, root):
        if (start and day < start) or (end and day > end):
            continue
        pdir = partition_dir(dataset, day, root)
        try:
            files = sorted(pdir.glob("*.jsonl.gz"))
        except OSError:
            continue
        for path in files:
            if name is not None and not path.name.startswith(f"{name}."):
                continue
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield day, json.loads(line)
                        except ValueError:
                            continue
            except OSError:
                continue


def read_day(dataset: str, day: str, name: str = None, root=None) -> list:
    """All rows of one partition day as a list ([] if absent/unreadable)."""
    return [row for _, row in scan(dataset, start=day, end=day, name=name,
                                   root=root)]


if __name__ == "__main__":
    # Manual check: what's in the lake?
    if not LAKE_ROOT.is_dir():
        print("(lake: empty — no data/lake yet)")
    else:
        for ds_dir in sorted(LAKE_ROOT.iterdir()):
            if not ds_dir.is_dir():
                continue
            for sub in sorted(ds_dir.iterdir()):
                label = (f"{ds_dir.name}/{sub.name}"
                         if not sub.name.startswith("date=") else ds_dir.name)
                if sub.name.startswith("date="):
                    days = list_days(ds_dir.name)
                    print(f"{ds_dir.name}: {len(days)} day(s) "
                          f"[{days[0]} .. {days[-1]}]" if days else ds_dir.name)
                    break
                days = list_days(f"{ds_dir.name}/{sub.name}")
                if days:
                    print(f"{label}: {len(days)} day(s) [{days[0]} .. {days[-1]}]")

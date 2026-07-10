"""
src/market_snapshot.py — the engine's published market read-model
=================================================================

Single-writer / many-reader decoupling. The live loop (src/live_bridge)
is the ONLY component that talks to Dhan for quotes, and once per cycle it
publishes what it saw — the underlying spots plus every open position's
live mark — to data/market_snapshot.json. Any viewer (the dashboard's
GET /api/web/positions, or a copy synced to the Mac) then READS those
marks instead of hitting Dhan again.

Why this exists: a second independent quote-fetcher (e.g. the local
dashboard on the Mac) sharing the one Dhan access token (decision #48 —
one active token per account) races the live engine on the rate-limited
quote endpoint, so its marks intermittently come back empty. Reading the
engine's already-computed marks removes that contention entirely — the
engine fetches once, everyone else reads the file.

Fail-safe by hard rule, both directions:
  * write() never raises into the live loop — a disk hiccup must not kill
    the trading cycle; it returns False and logs one line.
  * read() never raises into a request handler — a missing / stale /
    corrupt file simply reads as None, and callers fall back to whatever
    they did before (a direct mark, or "n/a").

The write is atomic (temp file + os.replace) so a reader never catches a
half-written file.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "data" / "market_snapshot.json"
IST = timezone(timedelta(hours=5, minutes=30))

# The live loop publishes every POLL_INTERVAL_SECONDS (60s). Three cycles
# of slack keeps a reader tolerant of one or two missed writes before it
# declares the snapshot stale and falls back.
DEFAULT_MAX_AGE_SECONDS = 180


def write(spots: dict, marks: list, now: datetime = None,
          path=None) -> bool:
    """Atomically publish {as_of, epoch, spots, marks}. Returns True on
    success, False on any failure — never raises."""
    path = Path(path) if path is not None else SNAPSHOT_PATH
    now = now or datetime.now(IST)
    clean_spots = {}
    for k, v in (spots or {}).items():
        try:
            clean_spots[str(k)] = float(v) if v is not None else None
        except (TypeError, ValueError):
            clean_spots[str(k)] = None
    payload = {
        "as_of": now.isoformat(timespec="seconds"),
        "epoch": now.timestamp(),
        "spots": clean_spots,
        "marks": [m for m in (marks or []) if isinstance(m, dict)],
    }
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)   # atomic on POSIX — readers see all-or-nothing
        return True
    except Exception as e:
        print(f"  (market_snapshot: publish failed: {e})")
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def read(max_age_seconds: float = None, path=None,
         now: datetime = None) -> dict | None:
    """The published snapshot as a dict, or None. When `max_age_seconds`
    is given, a snapshot older than that reads as None (so a stale file —
    e.g. the engine stopped publishing — never masquerades as live).
    Never raises."""
    path = Path(path) if path is not None else SNAPSHOT_PATH
    if not path.exists():
        return None
    try:
        snap = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if not isinstance(snap, dict) or "epoch" not in snap:
        return None
    if max_age_seconds is not None:
        age = age_seconds(snap, now=now)
        if age is None or age > max_age_seconds:
            return None
    return snap


def age_seconds(snapshot: dict, now: datetime = None) -> float | None:
    """Seconds since the snapshot was published, or None if unparseable."""
    if not isinstance(snapshot, dict) or "epoch" not in snapshot:
        return None
    now = now or datetime.now(IST)
    try:
        return now.timestamp() - float(snapshot["epoch"])
    except (TypeError, ValueError):
        return None


def marks_by_id(snapshot: dict) -> dict:
    """{short_id: mark_dict} from a snapshot's marks list, or {}."""
    if not isinstance(snapshot, dict):
        return {}
    out = {}
    for m in snapshot.get("marks") or []:
        if isinstance(m, dict) and m.get("short_id"):
            out[m["short_id"]] = m
    return out


def spot_for(snapshot: dict, ticker: str) -> float | None:
    """The published spot for `ticker`, or None. Lets a reader mark an
    equity leg (spot minus entry) from the engine's spot without any
    Dhan call of its own."""
    if not isinstance(snapshot, dict) or not ticker:
        return None
    value = (snapshot.get("spots") or {}).get(str(ticker))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

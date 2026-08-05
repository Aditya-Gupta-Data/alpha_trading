#!/usr/bin/env python3
"""
scripts/fetch_sector_bars.py — THE producer for data/sector_index_bars.json
===========================================================================

**MAC-ONLY. NEVER RUN THIS ON THE VM.** yfinance is a Mac-lane dependency
(not in requirements.txt), Yahoo rate-limits/blocks datacentre IPs, and the
boundary doctrine keeps every crawler off the VM's address. `src/` must stay
free of a yfinance import, which is why this is a script and not an
`ingestion/` clerk — the `scripts/fetch_pre2019_sectors.py` precedent.

WHY IT EXISTS (2026-08-05). `data/sector_index_bars.json` had **no producer
anywhere in the repository**. It was written once on 2026-07-16 and never
refreshed, while still feeding a LIVE bullish veto through
`analysis/sector_trend.is_sector_bullish` -> `analysis/regime_filters`.
`staleness_guard` now disarms that veto when the file is stale; this script
is the other half — the thing that keeps it fresh so the veto can re-arm
itself. Neither half is useful without the other.

WHAT IT WRITES — the EXACT shape `sector_trend` already reads, unchanged:

    { "<yahoo_index>": {"sector": "IT",
                        "bars": [[date, low, high, close], ...]}, ... }

`sector_trend._closes` takes `b[3]` and `is_sector_bullish` reads
`bars[-1][0]` as `as_of`, so the tuple order is load-bearing. Indices and
their sector names come from `config/sector_universe.json` — the single
source of that mapping — never from a list hardcoded here. Two sectors can
share one index (BATTERY_EV and AUTO both map to ^CNXAUTO); the index is
fetched ONCE and the `sector` label is the first sector that claims it,
matching what the existing file already contains.

THE MERGE RULE — extend forward, never rewrite history (the
`ingestion/index_history.py` doctrine): union by date, **the STORED value
wins on any overlapping date**, new dates are appended, and the result is
written sorted. A bad fetch can therefore never corrupt or shorten the
4,600-bar history we already trust; the worst case is that today's row is
missing. Writes are atomic (tmp + rename), so a kill mid-write cannot leave
a half-file where a live veto reads.

NULL-HONEST: a bar with any missing OHLC value is SKIPPED, never zero-filled
and never forward-filled — `sector_trend` needs 201 real closes for an SMA200
and a fabricated one is worse than a short history. Per-index fail-open: one
dead index keeps its existing bars and never costs the others. An outage is
named (SB-4xx/5xx) into `logs/sector_bars.jsonl`, never a silent pass.

CLI:
    python3 scripts/fetch_sector_bars.py                  # incremental (2y)
    python3 scripts/fetch_sector_bars.py --period max     # full rebuild
    python3 scripts/fetch_sector_bars.py --dry-run        # writes nothing
    python3 scripts/fetch_sector_bars.py --json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = ROOT / "config" / "sector_universe.json"
OUT_PATH = ROOT / "data" / "sector_index_bars.json"
LEDGER_PATH = ROOT / "logs" / "sector_bars.jsonl"

DEFAULT_PERIOD = "2y"          # plenty over an SMA200; merge keeps the rest
THROTTLE_SECONDS = 1.5         # politeness between Yahoo calls
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------- pure helpers

def load_index_map(universe_path=None) -> dict:
    """{yahoo_index: sector_label} from config/sector_universe.json.

    First sector to claim an index owns the label (AUTO before BATTERY_EV in
    file order), which is what the existing artifact already carries."""
    path = Path(universe_path) if universe_path else UNIVERSE_PATH
    try:
        sectors = json.loads(path.read_text()).get("sectors", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for sector, meta in sectors.items():
        sym = (meta or {}).get("yahoo_index")
        if sym and sym not in out:
            out[sym] = sector
    return out


def _num(value):
    """A finite float, or None. Never a guess, never a zero-fill."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):     # NaN / inf
        return None
    return f


def rows_from_history(history) -> list:
    """A yfinance DataFrame -> [[date, low, high, close], ...].

    Any row missing a value is DROPPED — NULL-honesty beats coverage here,
    because a fabricated close silently moves an SMA the engine votes on."""
    rows = []
    if history is None or getattr(history, "empty", True):
        return rows
    for stamp, row in history.iterrows():
        low, high, close = _num(row.get("Low")), _num(row.get("High")), _num(row.get("Close"))
        if low is None or high is None or close is None:
            continue
        day = stamp.date().isoformat() if hasattr(stamp, "date") else str(stamp)[:10]
        rows.append([day, round(low, 2), round(high, 2), round(close, 2)])
    return rows


def merge_bars(stored: list, fetched: list) -> list:
    """Union by date, STORED WINS on overlap, sorted ascending.

    Forward-extension only: nothing already in the file is ever rewritten by
    a later fetch, so a bad Yahoo day cannot rot trusted history."""
    by_date = {}
    for bar in fetched or []:
        if bar and len(bar) == 4:
            by_date[bar[0]] = list(bar)
    for bar in stored or []:                    # stored last => stored wins
        if bar and len(bar) == 4:
            by_date[bar[0]] = list(bar)
    return [by_date[d] for d in sorted(by_date)]


def _read_existing(out_path=None) -> dict:
    path = Path(out_path) if out_path else OUT_PATH
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(payload: dict, out_path=None) -> None:
    path = Path(out_path) if out_path else OUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def _ledger(entry: dict, ledger_path=None) -> None:
    path = Path(ledger_path) if ledger_path else LEDGER_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": datetime.now(IST).isoformat(
                timespec="seconds"), **entry}) + "\n")
    except OSError:
        pass                                    # the ledger is best-effort


# ------------------------------------------------------------------ the fetch

def _default_fetch(symbol: str, period: str):
    """The ONLY yfinance seam. Imported lazily so `--dry-run`, the tests and
    an import of this module all work on a box without yfinance."""
    import yfinance
    return yfinance.Ticker(symbol).history(period=period, auto_adjust=False)


def run(period: str = DEFAULT_PERIOD, fetch_fn=None, out_path=None,
        universe_path=None, ledger_path=None, dry_run: bool = False,
        throttle: float = THROTTLE_SECONDS) -> dict:
    """Refresh every sector index. Per-index fail-open; returns an honest
    report. Nothing is written when `dry_run`."""
    index_map = load_index_map(universe_path)
    if not index_map:
        _ledger({"code": "SB-404", "detail": "no sector universe"}, ledger_path)
        return {"ok": [], "failed": [{"index": None, "code": "SB-404",
                                      "detail": "no sector universe"}],
                "written": False, "as_of": None}

    fetch_fn = fetch_fn or _default_fetch
    store = _read_existing(out_path)
    ok, failed = [], []

    for i, (symbol, sector) in enumerate(index_map.items()):
        stored = (store.get(symbol) or {}).get("bars") or []
        try:
            if i and throttle:
                time.sleep(throttle)
            rows = rows_from_history(fetch_fn(symbol, period))
            if not rows:
                failed.append({"index": symbol, "code": "SB-404",
                               "detail": "no usable rows returned"})
                store.setdefault(symbol, {"sector": sector, "bars": stored})
                continue
            merged = merge_bars(stored, rows)
            store[symbol] = {"sector": sector, "bars": merged}
            ok.append({"index": symbol, "sector": sector,
                       "bars": len(merged), "added": len(merged) - len(stored),
                       "as_of": merged[-1][0]})
        except Exception as exc:                # per-index fail-open
            failed.append({"index": symbol, "code": "SB-500",
                           "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
            if stored:                          # keep what we already trust
                store[symbol] = {"sector": sector, "bars": stored}

    as_of = max((r["as_of"] for r in ok), default=None)
    written = False
    if ok and not dry_run:
        _atomic_write(store, out_path)
        written = True
    for f in failed:
        _ledger(f, ledger_path)
    if not ok:
        _ledger({"code": "SB-500", "detail": "every index failed — file "
                                             "left untouched"}, ledger_path)
    return {"ok": ok, "failed": failed, "written": written, "as_of": as_of,
            "indices": len(index_map)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--period", default=DEFAULT_PERIOD,
                    help="yfinance period (default 2y; use 'max' to rebuild)")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = run(period=args.period, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for r in report["ok"]:
            print(f"  ✅ {r['index']:<14} {r['sector']:<11} "
                  f"{r['bars']:>5} bars (+{r['added']}) as of {r['as_of']}")
        for f in report["failed"]:
            print(f"  ❌ {str(f['index']):<14} {f['code']} {f['detail']}")
        state = "written" if report["written"] else "NOT written"
        print(f"sector bars {state} — {len(report['ok'])}/"
              f"{report['indices']} indices, as of {report['as_of']}")
    # A run that refreshed nothing is a FAILED run, and the exit code says so
    # (the scrip_master doctrine: an unread source must never look clean).
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

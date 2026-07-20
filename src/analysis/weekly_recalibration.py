"""
src/analysis/weekly_recalibration.py — the Saturday fundamental clock
=====================================================================

The weekly half of the Two-Clock architecture (owner-approved
2026-07-20). The DAILY clock (patience_basket --eod) re-grades on price
and valuation; THIS clock re-judges the fundamentals themselves —
fundamentals only change when new quarterly filings arrive, so weekly is
their honest cadence:

  1. REFRESH  fresh quarterly filings for every captured symbol
              (integrated_results — SEBI integrated-filing XBRL, MAC-ONLY,
              throttled; the long stage, ~15-30 min over the full lake)
  2. RE-SCREEN  fundamental_screener re-runs the v1 pass rule + forensic
              trust gate over the refreshed lake -> a NEW darlings queue
              (drops the failures, admits new passers; a new passer still
              ripens through the report_downloader/deep-read pipeline
              before the forensic gate fully trusts it)
  3. PINS     Directive 2, the No-Orphan rule: a dropped name with an
              OPEN paper shadow is pinned into the tier table —
              grade "strong_sell" when the screen REJECTED it,
              grade "ungraded" when the screen merely lost the data
              (NULL-honesty: a sell verdict is never manufactured from
              absence). A dropped name with no open shadow just drops.
              A pinned name that re-passes the screen is un-pinned.
  4. REBUILD  pricer -> valuation -> tier grading over the new queue, so
              Monday opens on a ruthlessly current table (the weekly
              clock OVERRIDES the daily clock through the pins).
  5. CARD     one Discord summary (fail-open).

Every stage fail-opens and is reported honestly; a refresh outage still
re-screens on the existing lake (last week's filings — stale but real).

CLI:  python3 -m src.analysis.weekly_recalibration
          [--dry-run] [--skip-refresh]
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"
PINS_PATH = ROOT / "data" / "darling_pins.json"
RESULTS_LAKE = ROOT / "data" / "lake" / "financial_results"

IST = timezone(timedelta(hours=5, minutes=30))


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def update_pins(old_tickers: list, queue: dict, pins: dict,
                held: set, today: str) -> dict:
    """The No-Orphan bookkeeping. Returns the new pin map:
    dropped + held -> pinned (strong_sell when rejected, ungraded when
    the data went missing); back-in-queue or no-longer-held -> cleared;
    surviving pins carry their original reason and date."""
    new_tickers = set(queue.get("tickers") or [])
    rejected = queue.get("rejected") or {}
    dropped = sorted((set(old_tickers) - new_tickers) | set(pins))
    out = {}
    for sym in dropped:
        if sym in new_tickers or sym not in held:
            continue                 # recovered, or nothing open to protect
        if sym in pins:              # carried pin: keep its original story
            out[sym] = pins[sym]
        elif sym in rejected:
            out[sym] = {"grade": "strong_sell", "pinned_on": today,
                        "reason": ("weekly re-screen rejected: "
                                   f"{rejected[sym]}")}
        else:
            out[sym] = {"grade": "ungraded", "pinned_on": today,
                        "reason": "weekly re-screen: data insufficient "
                                  "to judge (held position kept visible)"}
    return out


def recalibrate(refresh_fn=None, screen_fn=None, pricer_fn=None,
                valuation_fn=None, tiers_fn=None, open_fn=None,
                broadcast_fn=None, queue_path=None, pins_path=None,
                skip_refresh: bool = False, write: bool = True) -> dict:
    """The whole Saturday pass. Every stage injectable and fail-open."""
    qpath = Path(queue_path) if queue_path else QUEUE_PATH
    ppath = Path(pins_path) if pins_path else PINS_PATH
    today = datetime.now(IST).date().isoformat()
    report = {"as_of": datetime.now(IST).replace(tzinfo=None)
                                        .isoformat(timespec="seconds"),
              "errors": []}
    old_tickers = _load(qpath, {}).get("tickers") or []

    # 1. refresh quarterly filings (the long, throttled stage)
    if skip_refresh:
        report["refresh"] = "skipped"
    else:
        try:
            if refresh_fn is None:
                from src.ingestion.integrated_results import run as _ir
                refresh_fn = _ir
            symbols = sorted(f.stem for f in RESULTS_LAKE.glob("*.json")) \
                if RESULTS_LAKE.is_dir() else []
            r = refresh_fn(symbols)
            report["refresh"] = r.get("summary", r) if isinstance(r, dict) else r
        except Exception as e:
            report["errors"].append(f"refresh: {e}")
            report["refresh"] = "failed (re-screening on the existing lake)"

    # 2. re-screen -> the new queue
    try:
        if screen_fn is None:
            from src.analysis.fundamental_screener import run as screen_fn
        queue = screen_fn(write=write)
    except Exception as e:
        report["errors"].append(f"screen: {e}")
        report["queue_unchanged"] = True
        queue = _load(qpath, {})
    new_tickers = queue.get("tickers") or []
    report["screened"] = queue.get("screened")
    report["added"] = sorted(set(new_tickers) - set(old_tickers))
    report["dropped"] = sorted(set(old_tickers) - set(new_tickers))

    # 3. pins (No-Orphan rule)
    try:
        if open_fn is None:
            from src.analysis.darling_tiers import _open_darling_symbols \
                as open_fn
        held = set(open_fn())
    except Exception as e:
        report["errors"].append(f"open positions: {e}")
        held = set()
    old_pins = _load(ppath, {}).get("pins") or {}
    pins = update_pins(old_tickers, queue, old_pins, held, today)
    report["pins"] = pins
    report["pins_cleared"] = sorted(set(old_pins) - set(pins))
    if write:
        ppath.parent.mkdir(parents=True, exist_ok=True)
        ppath.write_text(json.dumps(
            {"as_of": report["as_of"], "pins": pins}, indent=1))

    # 4. rebuild: pricer -> valuation -> tiers (fresh mu/sigma included)
    for name, fn, mod in (("pricer", pricer_fn, "dynamic_pricer"),
                          ("valuation", valuation_fn, "valuation_scorer")):
        try:
            if fn is None:
                import importlib
                fn = importlib.import_module(f"src.analysis.{mod}").run
            fn()
        except Exception as e:
            report["errors"].append(f"{name}: {e}")
    try:
        if tiers_fn is None:
            from src.analysis.darling_tiers import run as tiers_fn
        tiers = tiers_fn(write=write, broadcast=False)  # ONE weekly card below
        report["tiers"] = tiers.get("counts")
    except Exception as e:
        report["errors"].append(f"tiers: {e}")
        tiers = {}

    # 5. the one weekly card
    try:
        if broadcast_fn is None:
            from src.notifier import fire_broadcast as broadcast_fn
        counts = report.get("tiers") or {}
        dist = ", ".join(f"{t} {n}" for t, n in counts.items() if n)
        lines = [f"screened {report.get('screened')}: "
                 f"{len(new_tickers)} darlings "
                 f"(+{len(report['added'])} new, "
                 f"-{len(report['dropped'])} dropped)"]
        if report["added"]:
            lines.append("new: " + ", ".join(report["added"]))
        if report["dropped"]:
            lines.append("dropped: " + ", ".join(report["dropped"]))
        for sym, p in pins.items():
            lines.append(f"pinned {p['grade']}: {sym} — {p['reason']}")
        if report["pins_cleared"]:
            lines.append("pins cleared: "
                         + ", ".join(report["pins_cleared"]))
        if dist:
            lines.append(f"tiers: {dist}")
        if report["errors"]:
            lines.append("⚠ errors: " + "; ".join(report["errors"]))
        broadcast_fn({"event": "weekly_recalibration", "ticker": "DARLINGS",
                      "date": report["as_of"],
                      "description": "🔁 Weekly Recalibration —\n"
                                     + "\n".join(lines)})
        report["card_fired"] = True
    except Exception as e:
        report["card_fired"] = False
        print(f"  (weekly card skipped: {e})")
    return report


if __name__ == "__main__":
    import sys

    dry = "--dry-run" in sys.argv
    rep = recalibrate(skip_refresh="--skip-refresh" in sys.argv or dry,
                      write=not dry)
    print(json.dumps({k: v for k, v in rep.items()}, indent=1,
                     default=str))

"""
src/analysis/macro_nightly.py — the VM macro heartbeat (the scoring clock)
==========================================================================

The always-on daily run that makes the 60-session public scoring clock
REAL (spec §3, gates G2/G5). Lives on the VM cron — NOT the Mac EOD
chain (patience_basket), because the Mac is analysis-only and not
guaranteed up each day; a scoring clock that only ticks when a laptop is
open is not a clock.

Each night, off-hours:
  1. ingest today's cross-asset data — FRED globals (macro_lake) + NSE
     indices (indices_lake).
  2. put the regime declaration on the immutable ledger (macro_regime).

It does NOT rebuild templates/playbooks — archetypes change only when
the CATALOG changes, and that rebuild runs on the Mac (deep lake) and
ships the artifacts down. The nightly reads those artifacts + the lake
and declares against them.

Every stage FAILS OPEN: a dead FRED key or an NSE holiday leaves the
ledger honest (a no-data declaration or yesterday's marks), never
crashes the cron. One heartbeat line per run to logs/macro_nightly.log
so ops_monitor can see it ran. Fully injectable for offline tests.

CLI / cron:  python3 -m src.analysis.macro_nightly
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_LOG = ROOT / "logs" / "macro_nightly.log"


def _now_iso():
    from src.ingestion import macro_lake as ML
    return ML._now_iso()


def run(fred_fn=None, indices_fn=None, declare_fn=None,
        clock=None, heartbeat_path=None) -> dict:
    """One nightly cycle: ingest FRED + NSE indices, then declare.
    Each stage is caught independently so one dead source never aborts
    the others or the cron. Returns a summary and writes ONE heartbeat
    line. All three stages are injectable (offline tests)."""
    today = (clock or date.today)()
    stages = {}

    # 1. FRED globals (needs FRED_API_KEY in the VM env; a missing key is
    #    a NAMED per-series failure inside ingest_all, never a raise)
    try:
        if fred_fn is None:
            from src.ingestion.macro_lake import ingest_all as fred_fn
        r = fred_fn()
        stages["fred"] = {"ok": r.get("ok"),
                          "failed": [f.get("series")
                                     for f in r.get("failed") or []]}
    except Exception as exc:
        stages["fred"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # 2. NSE indices for today (static archive, scripted-safe; holiday =
    #    honest no_file)
    try:
        if indices_fn is None:
            from src.ingestion.indices_lake import ingest_day as indices_fn
        r = indices_fn(today)
        stages["indices"] = {"no_file": r.get("no_file"),
                             "rows_added": len(r.get("rows_added") or {})}
    except Exception as exc:
        stages["indices"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # 3. declare onto the immutable ledger — the actual clock tick
    try:
        if declare_fn is None:
            from src.analysis.macro_regime import declare as declare_fn
        d = declare_fn()
        stages["declare"] = {
            "declared": d.get("declared"),
            "horizons": {h: {"declared": v.get("declared"),
                             "phase": v.get("phase"),
                             "archetype": (v.get("best") or {}).get(
                                 "archetype")}
                         for h, v in (d.get("horizons") or {}).items()}}
    except Exception as exc:
        stages["declare"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    summary = {"ts": _now_iso(), "as_of": today.isoformat(),
               "stages": stages}
    path = Path(heartbeat_path) if heartbeat_path else HEARTBEAT_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(summary, default=str) + "\n")
    except OSError:
        pass
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))

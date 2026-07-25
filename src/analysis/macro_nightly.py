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
so ops_monitor can see it ran, PLUS one Discord health card (Phase-2
observability, 2026-07-25) fired unconditionally at the end through
notifier.fire_broadcast — the scannable per-stage line, e.g.
[🟢 FRED: OK | 🔴 Indices: FAILED | 🟢 Declare: OK | 🟢 Scorer: OK];
a Discord outage never fails the cron. Fully injectable for offline
tests.

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


def heartbeat_line(stages: dict) -> tuple:
    """The scannable one-line health string for the Discord card, plus
    the all-green flag. Pure function over run()'s stages dict.

    Per component: 🔴 when the stage raised (its dict carries "error"),
    when FRED reported failed series, or when declare hit the cache-miss
    ALERT (the silence ban: a cache miss must look RED, never routine).
    An NSE holiday (indices no_file) and an honest declare abstention are
    NOT failures — the engine breathed; the world just had nothing to say.
    """
    def _mark(name, stage, detail_ok="OK", detail_bad="FAILED"):
        if not isinstance(stage, dict):
            return f"🔴 {name}: MISSING", False
        if "error" in stage:
            return f"🔴 {name}: {detail_bad}", False
        return f"🟢 {name}: {detail_ok}", True

    parts, oks = [], []

    fred = stages.get("fred")
    line, ok = _mark("FRED", fred)
    if ok and fred.get("failed"):
        line, ok = f"🔴 FRED: {len(fred['failed'])} series FAILED", False
    parts.append(line); oks.append(ok)

    idx = stages.get("indices")
    line, ok = _mark("Indices", idx)
    if ok and idx.get("no_file"):
        line = "🟢 Indices: OK (no file — holiday?)"
    parts.append(line); oks.append(ok)

    dec = stages.get("declare")
    line, ok = _mark("Declare", dec)
    if ok and dec.get("ALERT"):
        line, ok = "🔴 Declare: CACHE MISS", False
    parts.append(line); oks.append(ok)

    line, ok = _mark("Scorer", stages.get("score"))
    parts.append(line); oks.append(ok)

    return "[" + " | ".join(parts) + "]", all(oks)


def run(fred_fn=None, indices_fn=None, declare_fn=None, scorer_fn=None,
        clock=None, heartbeat_path=None, notify_fn=None) -> dict:
    """One nightly cycle: ingest FRED + NSE indices, then declare.
    Each stage is caught independently so one dead source never aborts
    the others or the cron. Returns a summary, writes ONE heartbeat
    line, and fires ONE Discord health card (notify_fn, default the
    house fire_broadcast door) unconditionally at the end — 🔴 per
    failed-open stage, 🟢 otherwise. All stages are injectable
    (offline tests)."""
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

    # 3. declare onto the immutable ledger — the actual clock tick.
    #    THE VM is a DUMB EXECUTOR: require_cache=True forbids the 30-min
    #    recompute — a stale/absent cache makes the run abstain fast and
    #    scream, never grind the e2-micro (owner directive 2026-07-23).
    try:
        if declare_fn is None:
            from src.analysis.macro_regime import declare as _declare
            def declare_fn():
                return _declare(require_cache=True)
        d = declare_fn()
        horizons = d.get("horizons") or {}
        stages["declare"] = {
            "declared": d.get("declared"),
            "horizons": {h: {"declared": v.get("declared"),
                             "phase": v.get("phase"),
                             "cache_status": v.get("cache_status"),
                             "archetype": (v.get("best") or {}).get(
                                 "archetype")}
                         for h, v in horizons.items()}}
        # silence ban: a cache miss is a LOUD ops fault, not a silent grind
        misses = [h for h, v in horizons.items()
                  if str(v.get("cache_status") or "").startswith("miss")]
        if misses:
            stages["declare"]["ALERT"] = (
                f"Cache Miss/Stale - Aborting (horizons: {misses}) — "
                "reseed the VM's fingerprint cache from the Mac")
    except Exception as exc:
        stages["declare"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    # 4. Stage B (SB-2) — forward-score the declarations whose windows have now
    #    elapsed, then rebuild the scoreboard. LAST and FAIL-OPEN: a scorer fault
    #    can never touch the declaration or the clock. Pure shadow — reads the
    #    lake, writes only its own ledgers (macro_strategy_scores.jsonl +
    #    strategy_scoreboard.json). Runs AFTER declare so today's fresh regime is
    #    already on the ledger before we resolve the matured past ones.
    try:
        if scorer_fn is None:
            from src.analysis.strategy_scorer import run as _score
            from src.analysis.strategy_scoreboard import build_scoreboard as _board

            def scorer_fn():
                s = _score()
                bs = _board().get("summary", {})
                return {"graded": s.get("graded"), "wins": s.get("wins"),
                        "pending": s.get("pending_declarations"),
                        "confirmed": bs.get("confirmed_count"),
                        "contradicted": bs.get("contradicted_count")}
        stages["score"] = scorer_fn()
    except Exception as exc:
        stages["score"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    status_line, all_ok = heartbeat_line(stages)
    summary = {"ts": _now_iso(), "as_of": today.isoformat(),
               "status_line": status_line, "all_ok": all_ok,
               "stages": stages}
    path = Path(heartbeat_path) if heartbeat_path else HEARTBEAT_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(summary, default=str) + "\n")
    except OSError:
        pass

    # 5. The Discord health card — UNCONDITIONAL and LAST, through the one
    #    Discord door (Dept 6 rule). Its own try/except: a Discord outage
    #    can never fail the cron or un-write the heartbeat log line above.
    try:
        if notify_fn is None:
            from src.notifier import fire_broadcast as notify_fn
        notify_fn({"event": "macro_heartbeat", "ticker": "MACRO",
                   "date": today.isoformat(), "all_ok": all_ok,
                   "description": status_line})
    except Exception as exc:
        print(f"  (macro_nightly heartbeat card failed: {exc})")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))

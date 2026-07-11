"""
src/discovery/run_miners.py — the discovery pass orchestrator
=============================================================

Phase 5 of docs/HOLY_GRAIL_PLAN.md (§8.3). Runs every miner over both
corpora in one call and returns an HONEST combined report: how many
transactions each corpus offered, how many candidates survived, how many
were newly registered. It ENUMERATES + REGISTERS only — nothing here
surfaces a pattern to a card; that gate is still the proving harness
(trial → validation → drift monitor).

Deliberately MANUAL-ONLY (not in scripts/setup_cron.sh, not in
src/sleep_phase.py). The panel's rule: don't run the miners nightly until
the daily_context series is long enough that a lagged itemset can plausibly
clear the support floor — until then every run legitimately finds nothing,
and a nightly "0 survivors" card is just noise that trains the owner to
ignore the surface. When the history is there, THIS is the single entry a
cron line or a sleep-phase task calls; the wiring decision gets its own
DECISIONS row when the data justifies it.

The report leads with the honest headline: on a short history, zero
survivors is CORRECT, and the transaction counts show WHY (thin support),
never hidden behind an empty success message.

Manual:  python3 -m src.discovery.run_miners
"""

import json
from datetime import date

from src.discovery import cooccurrence_miner as cm
from src.discovery import sequence_miner as sm
from src.validation import stat_gates as sg

MINERS = (("cooccurrence", cm), ("sequence", sm))
CORPORA = ("real", "sim")


def run_all(conn=None, db_path=None, today: date = None) -> dict:
    """Run every (miner × corpus) and aggregate. Reuses a caller conn or
    opens its own. Never raises — a miner that throws is recorded as an
    error entry and the pass continues (fail-open, decision-#30 style)."""
    own = conn is None
    if conn is None:
        from src import brain_map
        conn = brain_map.connect(db_path)
    today = today or date.today()
    runs, totals = [], {"transactions": 0, "survivors": 0,
                        "newly_registered": 0, "errors": 0}
    try:
        for name, mod in MINERS:
            for corpus in CORPORA:
                try:
                    res = mod.run(conn=conn, corpus=corpus, today=today)
                    runs.append({"miner": name, **{k: res[k] for k in (
                        "corpus", "transactions", "survivors",
                        "newly_registered")}})
                    for k in ("transactions", "survivors", "newly_registered"):
                        totals[k] += res[k]
                except Exception as exc:      # one miner never sinks the pass
                    runs.append({"miner": name, "corpus": corpus,
                                 "error": str(exc)})
                    totals["errors"] += 1
    finally:
        if own:
            conn.close()
    return {"date": today.isoformat(), "runs": runs, "totals": totals,
            "floors": sg.configured_floors(),
            "summary": _summary(runs, totals)}


def _summary(runs: list, totals: dict) -> str:
    """The plain-English headline. Frames a thin-data zero as correct, and
    shows the support reality (transaction counts) that explains it."""
    real_txns = sum(r.get("transactions", 0) for r in runs
                    if r.get("corpus") == "real")
    floor = sg.configured_floors()["min_support_itemset"]
    if totals["survivors"] == 0:
        why = ("no survivors — CORRECT on this history: an itemset needs "
               f"≥{floor} supporting transactions and the real corpus "
               f"offered {real_txns}. Nothing is surfaced that hasn't "
               "earned it.")
        return f"🔎 Discovery pass: {why}"
    return (f"🔎 Discovery pass: {totals['survivors']} candidate(s) "
            f"registered ({totals['newly_registered']} new) across "
            f"{len(runs)} miner-runs — each now owes the proving harness "
            "its out-of-sample rent before any card cites it.")


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2, default=str))

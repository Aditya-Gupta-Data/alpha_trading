"""
src/analysis/patience_basket.py — the Mac EOD evening chain (Dept 8)
====================================================================

Owner directive 2026-07-20 (the Lifecycle Portfolio Management System):
the RIPE/waiting bucket logic that used to live here is SCRAPPED —
grading is now the 7-tier engine in `darling_tiers.py` (strong/weak
buy-hold-sell + watch + Tier-0 ungraded, family-transition cards). This
module keeps the owner's muscle-memory entry point and remains the ONE
evening chain composition root:

    bhavcopy fetch -> F&O bundle -> pricer -> valuation -> TIER GRADING
    -> darling shadow leg (tier-driven entries AND Strong-Sell forced
    exits)

Each stage fail-opens; a missing bhavcopy (holiday) still refreshes the
grading from existing artifacts. MAC-ONLY (boundary doctrine); the cron
line is pasted by the owner (TCC). The weekly Saturday clock
(`weekly_recalibration.py`) re-screens fundamentals and pins failures —
this daily chain honors those pins through the tier engine.

CLI:  python3 -m src.analysis.patience_basket [--eod] [--dry-run]
      (no flag: re-grade from existing artifacts, no fetches)
"""


def eod_chain() -> dict:
    """The Mac evening chain: today's bhavcopy -> F&O bundle -> pricer ->
    valuation -> tier grading -> shadow leg. Each stage fail-opens."""
    from datetime import date as _date

    from src.analysis.darling_tiers import run as tiers_run
    from src.analysis.dynamic_pricer import run as pricer_run
    from src.analysis.valuation_scorer import run as valuation_run
    from src.ingestion.bhavcopy_clerk import fetch_day
    from src.ingestion.fo_bhavcopy import fetch_recent

    day = fetch_day(_date.today())
    fo = fetch_recent(3)          # F&O bundle leg (owner: no manual DLs)
    pricer_run()
    valuation_run()
    tiers = tiers_run()
    report = {"as_of": tiers.get("as_of"), "tiers": tiers.get("counts"),
              "card_fired": tiers.get("card_fired"),
              "pins_cleared": tiers.get("pins_cleared"),
              "bhavcopy": day, "fo_snapshot_as_of": fo.get("snapshot_as_of")}
    # VM-SHIFT (decision #83, owner override 2026-07-21): the Mac is the
    # ANALYSIS side only — every trade, rupee and ledger row lives on the
    # VM. This chain's last job is shipping tonight's artifacts down the
    # scp lane: the tier table + pricer levels (the VM desk's eyes,
    # freshness-gated there) and the weekly darling-ids file (quote ids
    # from Dhan's public scrip master — heavy fetch, weekly guard).
    # Fail-open per artifact; a missed ship = the VM holds yesterday's
    # copy and its own staleness gates judge it.
    from pathlib import Path as _Path
    data_dir = _Path(__file__).resolve().parents[2] / "data"
    shipped = []
    try:
        from src import firm_treasury
        from src.ingestion import scrip_master
        try:
            scrip_master.ensure_darling_ids()
        except Exception as exc:
            print(f"  (darling ids refresh failed [{exc}])")
        for art in ("darling_tiers.json", "darlings_levels.json",
                    "darling_ids.json"):
            p = data_dir / art
            if p.exists() and firm_treasury.vm_push_file(p):
                shipped.append(art)
    except Exception as exc:
        print(f"  (artifact ship failed [{exc}])")
    report["artifacts_shipped"] = shipped
    # MACRO LEG (2026-07-23, Macro Regime Engine M4): ingest tonight's
    # cross-asset rows (FRED + NSE indices) and put the regime
    # declaration ON THE RECORD — the 60-session public scoring clock
    # (spec §3, gates G2/G5). Fail-open like every other stage: a dead
    # macro leg leaves the ledger honest-empty tonight, never breaks
    # the darlings chain. Advisory authority: none (Dept-5 graduation).
    try:
        from src.ingestion.macro_lake import ingest_all as _macro_ingest
        from src.ingestion.indices_lake import ingest_day as _idx_ingest
        from src.analysis.macro_regime import declare as _regime_declare
        macro = _macro_ingest()
        idx = _idx_ingest(_date.today())
        reg = _regime_declare()
        report["macro"] = {
            "fred_ok": macro.get("ok"), "fred_failed":
                [f.get("series") for f in macro.get("failed") or []],
            "indices_no_file": idx.get("no_file"),
            "regime_declared": reg.get("declared"),
            "regime_reason": reg.get("reason"),
            "regime_best": (reg.get("best") or {}).get("archetype")}
    except Exception as exc:
        print(f"  (macro leg failed [{exc}])")
        report["macro"] = {"error": str(exc)[:200]}
    return report


if __name__ == "__main__":
    import sys

    if "--eod" in sys.argv:
        print(eod_chain())
    else:
        from src.analysis.darling_tiers import run as tiers_run
        dry = "--dry-run" in sys.argv
        res = tiers_run(write=not dry, broadcast=not dry)
        print(f"tiers as of {res['as_of']}: "
              + ", ".join(f"{t} {n}" for t, n in res["counts"].items() if n))

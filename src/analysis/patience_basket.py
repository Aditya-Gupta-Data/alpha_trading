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
    # Firm treasury (owner Directive 1, decision #80): rotate capital
    # AFTER grading (freshest Buy-tier demand read) and BEFORE the shadow
    # leg spends. Fail-open: an unreachable VM keeps the current split.
    try:
        from src import firm_treasury
        if firm_treasury.TREASURY_ENABLED:
            t = firm_treasury.run_rotation()
            report["treasury"] = {"rotated": t.get("rotated"),
                                  "reason": (t.get("move") or {}).get(
                                      "reason") or t.get("reason")}
        else:
            report["treasury"] = None
    except Exception as exc:
        print(f"  (firm treasury failed — split unchanged [{exc}])")
        report["treasury"] = None
    # The darling shadow leg (F&O tranche step 5, re-wired to tiers
    # 2026-07-20): Strong-Sell forced exits + Buy-family entries.
    # Fail-open: telemetry can never break the chain.
    try:
        from src.equity_shadow_proposer import run_darling_cycle

        # Equity desk (owner ruling 2026-07-20): the ONE place the desk's
        # paper capital is wired in. A missing/disabled desk degrades to
        # the zero-capital telemetry leg, never to a broken chain.
        capital_fn = settle_fn = None
        desk = None
        try:
            from src import equity_desk as desk
            if desk.EQUITY_DESK_ENABLED:
                capital_fn, settle_fn = desk.fund_entry, desk.settle_exit
        except Exception as exc:
            print(f"  (equity desk unavailable — telemetry only [{exc}])")
        shadow = run_darling_cycle(capital_fn=capital_fn,
                                   settle_fn=settle_fn)
        report["shadow"] = {
            "entries": len(shadow["entries"]),
            "exits": len(shadow["exits"]),
            "settlements": len(shadow.get("settlements") or []),
            "funded": len([e for e in shadow["entries"]
                           if (e.get("funding") or {}).get("funded")])}
        if desk is not None and capital_fn is not None:
            desk.broadcast_activity(shadow)   # one card, quiet days silent
    except Exception as exc:
        print(f"  (patience basket: darling shadow leg failed [{exc}])")
        report["shadow"] = None
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

"""
src/margin_audit.py — the SPAN margin audit (Department 3, read-only)
=====================================================================

Task 2 step 1 of the 2026-07-19 SPAN work: BEFORE wiring the VIX-stress
multiplier into live reservations, measure what it would have done to the
book we actually traded. Report-only — reads the journal, writes nothing,
places nothing.

For every journaled spread this audit answers three questions:

  1. DRIFT — does the margin recorded at proposal time still match what
     `portfolio.calculate_span_margin` computes from the recorded legs
     today? (A mismatch means the margin model changed under an open book
     — worth knowing before trusting any of the numbers below.)
  2. ENTRY-TIME STRESS — what would the reservation have been with the
     stress factor applied at the entry's own recorded VIX
     (`receipt.vix`)? Entries without a recorded VIX are reported
     honestly as unstressable, never guessed.
  3. PANIC SCENARIO — what does the whole OPEN book reserve if every
     position is re-margined at the panic factor, and does the capital
     pool still hold it? How many of the historical entries would NOT
     have fit (walking the journal in order against the pool)?

Every seam is injectable (entries, pool) so the audit tests offline.

CLI, from the project folder:

    python3 -m src.margin_audit [--json]
"""
import json

from src.portfolio import VIX_STRESS_BANDS, calculate_span_margin, span_stress_factor

PANIC_FACTOR = VIX_STRESS_BANDS[0][1]
DRIFT_TOLERANCE_RS = 0.01


def _status(entry: dict) -> str:
    if entry.get("outcome"):
        return "resolved"
    d = entry.get("decision")
    return {"approved": "open", "pending_approval": "pending"}.get(d, d or "?")


def _row(entry: dict) -> dict | None:
    """One audited line per journaled spread; None for non-spread entries."""
    spread = entry.get("spread")
    if not spread or not (spread.get("margin") or {}).get("total_margin"):
        return None
    lots = int(spread.get("lots", 1))
    recorded = round(float(spread["margin"]["total_margin"]) * lots, 2)
    try:
        recomputed = round(calculate_span_margin(
            spread["legs"], spread["lot_size"])["total_margin"] * lots, 2)
    except Exception:
        recomputed = None
    vix = (entry.get("receipt") or {}).get("vix")
    factor = span_stress_factor(vix)
    return {
        "short_id": entry.get("short_id"),
        "status": _status(entry),
        "strategy": spread.get("strategy"),
        "lots": lots,
        "recorded_margin_rs": recorded,
        "recomputed_margin_rs": recomputed,
        "margin_drift": (recomputed is not None
                         and abs(recomputed - recorded) > DRIFT_TOLERANCE_RS),
        "entry_vix": vix,
        "entry_stress_factor": factor if vix is not None else None,
        "reserved_with_entry_stress_rs": (round(recorded * factor, 2)
                                          if vix is not None else None),
        "reserved_at_panic_rs": round(recorded * PANIC_FACTOR, 2),
    }


def audit(entries: list, pool: float = None) -> dict:
    """The full audit over journal-shaped entries. `pool` defaults to the
    account's starting capital (the Rs.10L paper pool)."""
    if pool is None:
        from src.portfolio_manager import STARTING_CAPITAL
        pool = STARTING_CAPITAL

    rows = [r for r in (_row(e) for e in entries or []) if r]
    open_rows = [r for r in rows if r["status"] == "open"]

    open_base = round(sum(r["recorded_margin_rs"] for r in open_rows), 2)
    open_panic = round(sum(r["reserved_at_panic_rs"] for r in open_rows), 2)

    # Greedy replay in journal order: which historical entries would NOT
    # have fit had every reservation carried the panic factor?
    cash = float(pool)
    squeezed_out = []
    for r in rows:
        need = r["reserved_at_panic_rs"]
        if need <= cash:
            cash -= need
        else:
            squeezed_out.append(r["short_id"])

    stressed_entries = [r for r in rows if (r["entry_stress_factor"] or 1.0) > 1.0]
    return {
        "pool_rs": float(pool),
        "n_spreads": len(rows),
        "n_open": len(open_rows),
        "n_margin_drift": sum(1 for r in rows if r["margin_drift"]),
        "n_missing_entry_vix": sum(1 for r in rows if r["entry_vix"] is None),
        "n_entries_born_stressed": len(stressed_entries),
        "open_book_base_margin_rs": open_base,
        "open_book_panic_margin_rs": open_panic,
        "open_book_panic_extra_rs": round(open_panic - open_base, 2),
        "open_book_panic_pct_of_pool": (round(open_panic / pool * 100, 2)
                                        if pool else None),
        "panic_factor": PANIC_FACTOR,
        "n_squeezed_out_at_panic": len(squeezed_out),
        "squeezed_out_ids": squeezed_out,
        "rows": rows,
    }


def render(report: dict) -> str:
    lines = [
        "SPAN MARGIN AUDIT (report-only — nothing is changed by this run)",
        f"  pool Rs.{report['pool_rs']:,.0f}  |  spreads audited: "
        f"{report['n_spreads']} ({report['n_open']} open)",
        f"  margin drift vs today's model: {report['n_margin_drift']} "
        f"entr{'y' if report['n_margin_drift'] == 1 else 'ies'}",
        f"  entries born under stressed VIX (factor > 1.0): "
        f"{report['n_entries_born_stressed']}"
        + (f"  |  no recorded VIX: {report['n_missing_entry_vix']}"
           if report["n_missing_entry_vix"] else ""),
        "",
        f"  OPEN book base margin:  Rs.{report['open_book_base_margin_rs']:,.2f}",
        f"  OPEN book at panic x{report['panic_factor']:g}: "
        f"Rs.{report['open_book_panic_margin_rs']:,.2f} "
        f"(+Rs.{report['open_book_panic_extra_rs']:,.2f}, "
        f"{report['open_book_panic_pct_of_pool']:g}% of the pool)"
        if report["open_book_panic_pct_of_pool"] is not None else "",
        f"  entries squeezed out replaying history at panic reservations: "
        f"{report['n_squeezed_out_at_panic']}"
        + (f" ({', '.join(report['squeezed_out_ids'])})"
           if report["squeezed_out_ids"] else ""),
        "",
        f"  {'id':8}  {'status':8}  {'strategy':16} {'recorded':>12} "
        f"{'entry VIX':>9} {'stressed':>12} {'panic':>12}  drift",
    ]
    for r in report["rows"]:
        lines.append(
            f"  {r['short_id'] or '?':8}  {r['status']:8}  "
            f"{(r['strategy'] or '?'):16} {r['recorded_margin_rs']:>12,.2f} "
            f"{r['entry_vix'] if r['entry_vix'] is not None else 'n/a':>9} "
            f"{r['reserved_with_entry_stress_rs'] if r['reserved_with_entry_stress_rs'] is not None else 'n/a':>12} "
            f"{r['reserved_at_panic_rs']:>12,.2f}  "
            f"{'DRIFT' if r['margin_drift'] else 'ok'}")
    return "\n".join(str(x) for x in lines if x != "")


if __name__ == "__main__":
    import sys

    from src import journal

    report = audit(journal.read_all())
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))

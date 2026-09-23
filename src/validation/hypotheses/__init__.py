"""
src/validation/hypotheses — THE PROVING COURT'S HYPOTHESIS QUEUE (Dept 5,
decision #109, 2026-09-23).

Each module here is ONE frozen hypothesis the architect ordered TESTED
BEFORE any live change: a canonical definition (registered once in the
court's registry as a CANDIDATE, aged into TRIAL by the court's own rule),
a pure `cohorts()` that splits the desk's real out-of-sample record into
the two cohorts the hypothesis compares, and `evidence()` that scores them
on the metric the hypothesis names. `run_nightly` runs every one inside the
21:00 court job, merges the evidence onto the registry row (`oos_stats`)
and reports it on the court's summary. NOTHING here is on a live path:
no proposer, tracker, desk or venue imports this package.

    A  tod_entry_window — entries between 10:00 and 14:30 IST vs all-day:
       per-trade Sharpe of realized R (options journal).
    B  mansfield_rs     — equity desk entries filtered by Mansfield relative
       strength vs NIFTY (> 0 = outperforming) vs unfiltered: max drawdown
       of the cumulative-R path (equity ledger).
"""
from __future__ import annotations

from datetime import date

from . import mansfield_rs, tod_entry_window

HYPOTHESES = (tod_entry_window, mansfield_rs)
MIN_N = 20      # below this per cohort the verdict is insufficient_n, never a lean


def run_nightly(conn, today: date = None, **seams) -> dict:
    """Register (idempotent) + score every queued hypothesis. Never raises
    for one hypothesis; each failure is a named skip on its own row."""
    from src.validation import registry as rg
    out = {}
    for mod in HYPOTHESES:
        name = mod.NAME
        try:
            reg = rg.register(conn, "hypothesis", mod.DEFINITION, mod.DESCRIPTION,
                              mining_run="proving_court.hypotheses")
            ev = mod.evidence(today=today, **{k: v for k, v in seams.items()
                                               if k in mod.SEAMS})
            rg.update_oos_stats(conn, reg["pattern_id"],
                                {"as_of": (today or date.today()).isoformat(), **ev})
            out[name] = {"pattern_id": reg["pattern_id"], "status": rg.get(conn, reg["pattern_id"])["status"],
                         **ev}
        except Exception as exc:
            out[name] = {"skip": f"{type(exc).__name__}: {exc}"}
    return out


def render_lines(summary: dict) -> list:
    lines = []
    for name, row in (summary or {}).items():
        if "skip" in row:
            lines.append(f"• [{name}] skipped — {row['skip']}")
            continue
        lines.append(f"• [{name}|{row.get('status')}] {row.get('verdict')} — "
                     f"{row.get('headline', '')}")
    return lines

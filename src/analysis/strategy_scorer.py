"""
src/analysis/strategy_scorer.py — SB-1: the forward-scoring core (RESOLVE/SCORE/ACCUMULATE)
==========================================================================================

Stage B (docs/stage_b_forward_scoring_spec.md). The Strategy Registry's
PREFER/SHOW verdicts are IN-SAMPLE (historical episodes). This module builds the
OUT-OF-SAMPLE record: it reads the immutable nightly declaration ledger
(logs/macro_regime_declarations.jsonl) and, for every past declaration whose
forward horizon has now ELAPSED, measures what each declared recipe ACTUALLY
returned over its window — with the SAME return math that scored the historical
episodes (macro_playbooks.episode_phase_returns + strategy_registry.evaluate_leg),
only anchored on the declaration date instead of a historical anchor. Zero new
math ⇒ zero backtest/forward discrepancy.

OUT-OF-SAMPLE BY CONSTRUCTION: a call is graded only once the FULL phase window
has elapsed on the benchmark calendar (the embargo), and the graded value is a
function of THAT window only — data after the window can never change it
(timelock-proven). Idempotent (a call is keyed by declaration_date × horizon ×
strategy_id). PURE SHADOW: no capital, no journal, no trade state — it reads the
lake and appends ONE immutable ledger (logs/macro_strategy_scores.jsonl).

Old (pre-SR-3) ledger lines carry no `top_strategies`; they resolve to zero
graded rows, honestly. Fail-open on all I/O.

CLI: python3 -m src.analysis.strategy_scorer [--dry-run]
"""
import bisect
import json
from datetime import date
from pathlib import Path

from src.analysis import macro_playbooks as PB
from src.analysis import strategy_registry as SR

ROOT = Path(__file__).resolve().parents[2]
DECLARATIONS_PATH = ROOT / "logs" / "macro_regime_declarations.jsonl"
SCORES_PATH = ROOT / "logs" / "macro_strategy_scores.jsonl"

# name -> frozen spec from the CODE catalog (stable, independent of the registry
# artifact which may rebuild between declaration and scoring).
CATALOG_BY_NAME = {s["name"]: s
                   for s in SR.SEED_STRATEGIES + SR.PLACEBO_STRATEGIES}


def _phase_hi(horizon, phase):
    """The last session-offset of a phase window — a call is resolvable only
    once this many sessions have elapsed since the declaration."""
    for name, _lo, hi in PB.PHASES_BY_HORIZON.get(horizon, ()):
        if name == phase:
            return hi
    return None


def _benchmark_calendar(lake_dir=None):
    """NIFTY session dates (holes dropped) — the same calendar
    episode_phase_returns anchors its windows on."""
    return [d for d, _ in PB._closes(PB.BENCHMARK, lake_dir)]


def _resolution(decl_date, calendar, hi):
    """(anchor_idx, resolved?). Resolved once the FULL window has elapsed on the
    benchmark calendar. anchor_idx = the last session <= decl_date (matching
    episode_phase_returns' own anchoring)."""
    idx = bisect.bisect_right(calendar, decl_date) - 1
    if idx < 0 or hi is None:
        return None, False
    return idx, (len(calendar) - 1 - idx) >= hi


def read_ledger(path=None):
    """Declaration ledger lines, in file order. Corrupt lines skipped."""
    out = []
    try:
        text = Path(path or DECLARATIONS_PATH).read_text()
    except OSError:
        return out
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def already_scored(path=None):
    """Set of (declaration_date, horizon, strategy_id) already graded — the
    idempotency guard so a re-run never double-counts."""
    seen = set()
    try:
        text = Path(path or SCORES_PATH).read_text()
    except OSError:
        return seen
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        seen.add((r.get("declaration_date"), r.get("horizon"),
                  r.get("strategy_id")))
    return seen


def score_horizon(decl_date, horizon, block, calendar, lake_dir, seen,
                  resolved_on):
    """RESOLVE + SCORE one declared horizon of one declaration. Returns
    (graded_rows, resolved?, pending?). NULL-honest: an unpriceable recipe or an
    absent phase yields no row, never a guessed zero."""
    phase = block.get("phase")
    if not (block.get("declared") and phase):
        return [], False, False                      # not a scorable call
    hi = _phase_hi(horizon, phase)
    _anchor_idx, resolved = _resolution(decl_date, calendar, hi)
    if not resolved:
        return [], False, True                       # embargo: still pending

    # forward phase returns, anchored on the DECLARATION date (the reused math)
    ph, _bench = PB.episode_phase_returns(
        decl_date, lake_dir, phases=PB.PHASES_BY_HORIZON[horizon])
    rows = []
    for s in block.get("top_strategies") or []:
        spec = CATALOG_BY_NAME.get(s.get("name"))
        if not spec:
            continue                                 # unknown recipe name
        sid = SR.strategy_id(spec)
        if (decl_date, horizon, sid) in seen:
            continue                                 # already graded
        r = SR.evaluate_leg(spec, ph, phase)
        if r is None:
            continue                                 # window unpriceable — skip
        rows.append({
            "declaration_date": decl_date, "resolved_on": resolved_on,
            "horizon": horizon, "archetype": block.get("archetype"),
            "phase": phase, "strategy_id": sid, "name": s.get("name"),
            "in_sample_verdict": block.get("strategy_verdict"),
            "in_sample_significant": s.get("significant"),
            "realized_return": round(r, 6), "win": bool(r > 0),
            "null": SR.STRUCTURAL_NULL})
    return rows, True, False


def run(declarations_path=None, scores_path=None, lake_dir=None,
        clock=None, dry_run=False):
    """One scoring pass: RESOLVE matured declarations, SCORE their recipes with
    the reused registry math, ACCUMULATE graded calls onto the immutable forward
    ledger. Idempotent; returns a summary."""
    resolved_on = (clock or (lambda: date.today().isoformat()))()
    calendar = _benchmark_calendar(lake_dir)
    seen = already_scored(scores_path)
    graded, pending = [], 0
    for line in read_ledger(declarations_path):
        dd = line.get("as_of_session")
        if not dd:
            continue
        for hz, block in (line.get("horizons") or {}).items():
            rows, _res, is_pending = score_horizon(
                dd, hz, block, calendar, lake_dir, seen, resolved_on)
            graded.extend(rows)
            pending += 1 if is_pending else 0
            for r in rows:                           # in-pass dedup
                seen.add((r["declaration_date"], r["horizon"],
                          r["strategy_id"]))
    if graded and not dry_run:
        p = Path(scores_path or SCORES_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            for r in graded:
                fh.write(json.dumps(r, default=str) + "\n")
    return {"resolved_on": resolved_on, "graded": len(graded),
            "wins": sum(1 for r in graded if r["win"]),
            "pending_declarations": pending}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), indent=2, default=str))

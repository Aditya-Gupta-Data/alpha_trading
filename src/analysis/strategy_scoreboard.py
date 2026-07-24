"""
src/analysis/strategy_scoreboard.py — SB-3/SB-4: forward rollup + graduation
============================================================================

Stage B (docs/stage_b_forward_scoring_spec.md). SB-1's scorer appends one
immutable graded call per resolved declaration to logs/macro_strategy_scores.jsonl.
This module ROLLS those calls up — per (archetype, phase, recipe) cell, mirroring
the in-sample registry table exactly — and GRADUATES each cell's forward record
through the Dept-5 rulebook:

  ACCUMULATING          fewer than MIN_FWD_CALLS live calls — no verdict yet
  FORWARD_CONFIRMED     the live hit-rate's Wilson LOWER bound clears the null
  FORWARD_CONTRADICTED  the live hit-rate's Wilson UPPER bound sits BELOW the null
  INCONCLUSIVE          enough calls, but the CI straddles the null (no edge)

The forward stratum is NEVER pooled with the in-sample record (the real/sim-split
discipline): the scoreboard reports both side by side so the money question —
did an in-sample PREFER actually confirm live? — is answerable at a glance.

Read-only over the immutable scores ledger; writes data/strategy_scoreboard.json
(atomic). No capital, no trade state. The weekly digest reads `digest_lines()`.

CLI: python3 -m src.analysis.strategy_scoreboard [--dry-run]
"""
import json
from datetime import date
from pathlib import Path

from src.analysis import strategy_registry as SR
from src.analysis import strategy_scorer as SC
from src.validation import stat_gates as sg

ROOT = Path(__file__).resolve().parents[2]
SCORES_PATH = SC.SCORES_PATH
SCOREBOARD_PATH = ROOT / "data" / "strategy_scoreboard.json"

NULL = SR.STRUCTURAL_NULL                 # drift-removed: the coin the recipe must beat
MIN_FWD_CALLS = sg.MIN_PROMOTION_RESOLUTIONS   # live calls before a verdict is issued


def _status(wins, n, min_calls=MIN_FWD_CALLS, null=NULL):
    """The graduation verdict on a cell's LIVE record alone (SB-4). Uses the
    same one-sided Wilson bounds as the rest of the house — a verdict only once
    the honest interval clears (or falls below) the null, never on the headline."""
    if n < min_calls:
        return "ACCUMULATING"
    lb = sg.wilson_lower_bound(wins, n)                 # lower bound on win-rate
    win_ub = 1.0 - sg.wilson_lower_bound(n - wins, n)   # upper bound on win-rate
    if lb > null:
        return "FORWARD_CONFIRMED"
    if win_ub < null:
        return "FORWARD_CONTRADICTED"
    return "INCONCLUSIVE"


def read_scores(path=None):
    """Graded calls from the immutable forward ledger. Corrupt lines skipped."""
    out = []
    try:
        text = Path(path or SCORES_PATH).read_text()
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


def _cell_record(calls):
    """One (archetype, phase, recipe) cell's forward record + graduation. The
    in-sample stamp is taken from the LATEST call (the registry may have rebuilt
    between declarations)."""
    n = len(calls)
    wins = sum(1 for c in calls if c.get("win"))
    rets = [c["realized_return"] for c in calls
            if c.get("realized_return") is not None]
    latest = max(calls, key=lambda c: c.get("resolved_on") or "")
    return {
        "strategy_id": latest.get("strategy_id"), "name": latest.get("name"),
        "in_sample": {"verdict": latest.get("in_sample_verdict"),
                      "significant": latest.get("in_sample_significant")},
        "forward": {"n": n, "wins": wins, "hit_rate": round(wins / n, 3),
                    "wilson_lb": round(sg.wilson_lower_bound(wins, n), 3),
                    "mean_return": round(sum(rets) / len(rets), 6) if rets else None,
                    "first_call": min(c.get("declaration_date") or "" for c in calls),
                    "last_call": max(c.get("declaration_date") or "" for c in calls)},
        "status": _status(wins, n),
    }


def build_scoreboard(scores_path=None, out_path=None, clock=None, dry_run=False):
    """Roll the immutable scores ledger up into the forward twin of the registry
    table (table[archetype][phase] = ranked recipe records + statuses) + a
    plain-English summary. Atomic write."""
    groups = {}
    for c in read_scores(scores_path):
        key = (c.get("archetype"), c.get("phase"), c.get("strategy_id"))
        groups.setdefault(key, []).append(c)

    table, counts = {}, {"ACCUMULATING": 0, "FORWARD_CONFIRMED": 0,
                         "FORWARD_CONTRADICTED": 0, "INCONCLUSIVE": 0}
    confirmed, contradicted = [], []
    for (aid, phase, _sid), calls in groups.items():
        rec = _cell_record(calls)
        table.setdefault(aid, {}).setdefault(phase, []).append(rec)
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        tag = {"name": rec["name"], "archetype": aid, "phase": phase,
               "n": rec["forward"]["n"], "hit_rate": rec["forward"]["hit_rate"],
               "wilson_lb": rec["forward"]["wilson_lb"],
               "in_sample_verdict": rec["in_sample"]["verdict"]}
        if rec["status"] == "FORWARD_CONFIRMED":
            confirmed.append(tag)
        elif rec["status"] == "FORWARD_CONTRADICTED":
            contradicted.append(tag)
    for aid in table:
        for phase in table[aid]:
            table[aid][phase].sort(key=lambda r: -r["forward"]["wilson_lb"])

    doc = {
        "built_at": (clock or (lambda: date.today().isoformat()))(),
        "params": {"min_fwd_calls": MIN_FWD_CALLS, "null": NULL,
                   "confirm": "wilson_lb > null", "contradict": "wilson_ub < null"},
        "table": table,
        "summary": {"cells_tracked": len(groups), "by_status": counts,
                    "confirmed_count": len(confirmed),
                    "contradicted_count": len(contradicted),
                    "confirmed": confirmed, "contradicted": contradicted},
        "note": ("forward = out-of-sample LIVE record; in_sample = the registry's "
                 "historical verdict. The two are reported side by side and NEVER "
                 "pooled. A cell advises nothing more until FORWARD_CONFIRMED + an "
                 "owner ruling."),
    }
    if not dry_run:
        p = Path(out_path or SCOREBOARD_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(p)
    return doc


def digest_lines(scoreboard_path=None):
    """Plain-English Stage-B lines for the weekly Discord digest. Honest when the
    board is empty (the clock is young). Read-only; returns a list of strings."""
    try:
        doc = json.loads(Path(scoreboard_path or SCOREBOARD_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return ["Forward clock: no scoreboard yet."]
    s = doc.get("summary", {})
    by = s.get("by_status", {})
    lines = [f"Forward clock — {s.get('cells_tracked', 0)} recipe·regime cells "
             f"tracking live ({by.get('ACCUMULATING', 0)} accumulating, "
             f"{by.get('FORWARD_CONFIRMED', 0)} confirmed, "
             f"{by.get('FORWARD_CONTRADICTED', 0)} contradicted)."]
    for c in s.get("confirmed", [])[:5]:
        lines.append(f"  ✅ CONFIRMED live: {c['name']} in {c['archetype']}/"
                     f"{c['phase']} — {c['hit_rate']:.0%} over {c['n']} calls "
                     f"(LB {c['wilson_lb']:.0%}, was {c['in_sample_verdict']} in-sample)")
    for c in s.get("contradicted", [])[:5]:
        lines.append(f"  ❌ CONTRADICTED live: {c['name']} in {c['archetype']}/"
                     f"{c['phase']} — {c['hit_rate']:.0%} over {c['n']} calls")
    return lines


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    doc = build_scoreboard(dry_run=args.dry_run)
    print(json.dumps({"built_at": doc["built_at"], "summary": doc["summary"]},
                     indent=2, default=str))

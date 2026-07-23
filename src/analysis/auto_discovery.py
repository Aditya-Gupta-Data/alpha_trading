"""
src/analysis/auto_discovery.py — the unsupervised discovery engine
==================================================================

docs/auto_discovery_spec.md, made real. The engine proposes its OWN
episodes from 25 years of cross-asset data — no human `macro_episodes.yaml`
input. Human labels become post-hoc annotations, never inputs.

Build state (2026-07-23, owner autonomous runbook #5):
  * AD-1 — FUNCTIONAL: unsupervised shock scan (change-points) +
    slow-burn motif scan (DTW self-similarity) → a candidate list.
    Compute-only, writes `data/discovered_candidates.json`, ZERO
    authority.
  * AD-2 — SCAFFOLDED: the significance layer (OOS + phase-randomized
    surrogates + stability). Structured with signatures + the null-model
    plumbing; the full surrogate test is the next build. Nothing is
    admitted as an episode until this passes.
  * AD-3 — SCAFFOLDED: candidate-court wiring (validation/registry).
  * AD-4 — SCAFFOLDED: dual-catalog tracker (human ∪ auto).

Stdlib only (no numpy); reuses macro_features (the ONE featurizer) and
macro_fingerprints.dtw_distance (the ONE matcher) — no train/serve skew,
no second engine. Mac-side weekly mining job (heavy scan), never the
nightly cron.

CLI: python3 -m src.analysis.auto_discovery [--dry-run]
"""
import json
from datetime import date, datetime
from pathlib import Path

from src.analysis import macro_features as MF
from src.analysis import macro_fingerprints as FP

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES_PATH = ROOT / "data" / "discovered_candidates.json"

# core global channels — same commensurable set the taxonomy clusters on
_CORE = ("BRENT", "DXY", "USDINR", "US10Y")
SHOCK_MIN_GAP_DAYS = 90        # two peaks < this apart are one shock
SHOCK_TOP_N = 25
MOTIF_WINDOW_SESSIONS = 378    # ~18 months of trading days
MOTIF_STRIDE = 21             # ~monthly stride across history


# ------------------------------------------------------- AD-1a: shocks

def _aligned_closes(lake_dir=None):
    """(dates, {channel: [close|None]}) aligned on the union calendar of
    the core channels — the raw material for both scans."""
    series = {c: MF.read_series(c, lake_dir) for c in _CORE}
    dates, matrix, _ = MF.align(series)
    return dates, matrix


def _z_series(values, window, baseline=None):
    """Rolling z of the window-day %-change at every day — via the SAME
    macro_features.zdelta math (no skew), None until history suffices.
    `baseline` resolves at CALL time (tests shrink it for speed)."""
    baseline = baseline if baseline is not None else MF.Z_BASELINE_SESSIONS
    return [MF.zdelta(values[:t + 1], window, baseline)
            for t in range(len(values))]


def system_stress(dates, matrix, window=20):
    """Per-day cross-asset stress = RMS of |z20| across the channels that
    have a z that day. A spike = the whole system moving at once — a
    shock signature. None on days no channel can score."""
    zbychan = {c: _z_series(v, window) for c, v in matrix.items()}
    out = []
    for i in range(len(dates)):
        zs = [zbychan[c][i] for c in matrix
              if zbychan[c][i] is not None]
        if not zs:
            out.append(None)
        else:
            out.append((sum(z * z for z in zs) / len(zs)) ** 0.5)
    return out


def rank_shock_candidates(lake_dir=None, top_n=SHOCK_TOP_N,
                          min_gap_days=SHOCK_MIN_GAP_DAYS):
    """Unsupervised shock anchors: local peaks of system-stress, kept
    greedily strongest-first with a min-gap so one crisis = one anchor.
    Returns [{date, stress}] ranked — UNNAMED, UNPROVEN (AD-2 gates)."""
    dates, matrix = _aligned_closes(lake_dir)
    stress = system_stress(dates, matrix)
    scored = sorted(((s, d) for d, s in zip(dates, stress)
                     if s is not None), reverse=True)
    chosen = []
    for s, d in scored:
        dd = date.fromisoformat(d)
        if all(abs((dd - date.fromisoformat(c["date"])).days) >= min_gap_days
               for c in chosen):
            chosen.append({"date": d, "stress": round(s, 3)})
        if len(chosen) >= top_n:
            break
    return chosen


# ------------------------------------------------- AD-1b: slow motifs

def _slow_windows(lake_dir=None, window=MOTIF_WINDOW_SESSIONS,
                  stride=MOTIF_STRIDE, z_window=60):
    """Sliding ~18-month z60 fingerprint windows across all history —
    the raw material for motif self-similarity. `z_window` is the slow
    %-change horizon (60 by design; tests shrink it)."""
    dates, matrix = _aligned_closes(lake_dir)
    zbychan = {c: _z_series(v, z_window) for c, v in matrix.items()}
    windows = []
    for start in range(0, len(dates) - window + 1, stride):
        rows = []
        for i in range(start, start + window):
            row = {f"{c}:z60": zbychan[c][i] for c in matrix
                   if zbychan[c][i] is not None}
            rows.append(row)
        if any(rows):
            windows.append({"start": dates[start],
                            "end": dates[start + window - 1], "rows": rows})
    return windows


def scan_motifs(lake_dir=None, max_pairs=15, window=MOTIF_WINDOW_SESSIONS,
                stride=MOTIF_STRIDE, z_window=60):
    """Recurring slow-burn windows: the lowest-DTW-distance
    NON-overlapping window pairs across history (reusing the ONE DTW).
    Each pair = 'this 18-month stretch rhymes with that one' — a
    candidate cycle, UNNAMED, UNPROVEN."""
    windows = _slow_windows(lake_dir, window=window, stride=stride,
                            z_window=z_window)
    pairs = []
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            # non-overlapping only
            if windows[j]["start"] <= windows[i]["end"]:
                continue
            d, cov = FP.dtw_distance(windows[i]["rows"], windows[j]["rows"])
            if d is not None:
                pairs.append({"a": [windows[i]["start"], windows[i]["end"]],
                              "b": [windows[j]["start"], windows[j]["end"]],
                              "dtw": round(d, 4), "coverage": cov})
    pairs.sort(key=lambda p: p["dtw"])
    return pairs[:max_pairs]


def discover(lake_dir=None, out_path=None, dry_run=False) -> dict:
    """AD-1 orchestrator: scan shocks + motifs → candidate list.
    Writes `data/discovered_candidates.json`. NO authority — every
    candidate must clear AD-2 (significance) then the court before it is
    an episode."""
    doc = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "note": ("AD-1 unsupervised candidates — UNPROVEN. Not episodes "
                 "until AD-2 significance + the candidate court pass."),
        "shock_candidates": rank_shock_candidates(lake_dir),
        "motif_candidates": scan_motifs(lake_dir),
    }
    if not dry_run:
        p = Path(out_path or CANDIDATES_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(p)
    return doc


# ---------------------------------------- AD-2: significance (scaffold)

def significance_gate(candidate, lake_dir=None, n_surrogates=200):
    """SCAFFOLD (AD-2). A candidate becomes admissible only if it beats a
    null of phase-randomized / block-bootstrap surrogates of the same
    series (preserving autocorrelation), recurs out-of-sample, and is
    stable to window perturbation. Returns a verdict dict; until built,
    an explicit not-built marker so nothing is silently admitted."""
    return {"admitted": False, "status": "AD-2_not_built",
            "candidate": candidate,
            "todo": ["phase_randomized_surrogates", "block_bootstrap",
                     "out_of_sample_recurrence", "window_stability"]}


# ---------------------------------------- AD-3: court wiring (scaffold)

def route_to_court(admitted_candidates):
    """SCAFFOLD (AD-3). Admitted candidates -> validation/registry as
    hypotheses under the SAME lifecycle every human episode faces;
    survivors land in data/discovered_episodes.json (source='auto')."""
    return {"status": "AD-3_not_built", "would_route": admitted_candidates}


# ------------------------------------- AD-4: dual-catalog (scaffold)

def merged_catalog(human_path=None, discovered_path=None):
    """SCAFFOLD (AD-4). The tracker declares against human ∪ auto
    archetypes; agreement = strongest signal, an auto-only regime the
    human catalog missed = a discovery card."""
    return {"status": "AD-4_not_built"}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    d = discover(dry_run=args.dry_run)
    print(json.dumps({"shocks": d["shock_candidates"][:12],
                      "motifs": d["motif_candidates"][:6]},
                     indent=2, default=str))

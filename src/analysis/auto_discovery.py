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
import cmath
import json
import math
import random
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


# ------------------------------------------ AD-2: the significance layer
# The moat: reject noise. A candidate is admitted only if it beats BOTH
# null models AND survives a held-out test. Stdlib only.

def _mean_std(xs):
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = math.fsum(xs) / n
    var = math.fsum((x - m) ** 2 for x in xs) / n
    return m, math.sqrt(var)


def block_bootstrap(series, block_len, rng):
    """Same-length surrogate from random contiguous blocks (holes
    dropped) — preserves within-block autocorrelation, destroys the
    rest. The simple, robust null."""
    vals = [v for v in series if v is not None]
    n = len(vals)
    if n == 0:
        return []
    out = []
    while len(out) < n:
        start = rng.randint(0, max(0, n - block_len))
        out.extend(vals[start:start + block_len])
    return out[:n]


def _fft(a, invert=False):
    """Iterative radix-2 Cooley-Tukey FFT (len power of two), pure
    stdlib — the primitive behind phase randomization."""
    n = len(a)
    a = list(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        wlen = cmath.exp((2j if invert else -2j) * math.pi / length)
        for i in range(0, n, length):
            w, half = 1 + 0j, length >> 1
            for k in range(half):
                u, v = a[i + k], a[i + k + half] * w
                a[i + k], a[i + k + half] = u + v, u - v
                w *= wlen
        length <<= 1
    return [x / n for x in a] if invert else a


def phase_randomize(series, rng):
    """Same-length surrogate with the SAME power spectrum (all linear
    autocorrelation preserved) but random, conjugate-symmetric phases
    (real output), rescaled to the original mean/std. The stricter
    null."""
    vals = [float(v) for v in series if v is not None]
    n = len(vals)
    if n < 4:
        return list(vals)
    size = 1
    while size < n:
        size <<= 1
    spec = _fft([complex(x) for x in vals + [0.0] * (size - n)])
    for k in range(1, size >> 1):
        spec[k] = cmath.rect(abs(spec[k]), rng.uniform(0, 2 * math.pi))
        spec[size - k] = spec[k].conjugate()
    sur = [x.real for x in _fft(spec, invert=True)][:n]
    m0, s0 = _mean_std(vals)
    m1, s1 = _mean_std(sur)
    return list(vals) if s1 == 0 else [m0 + (x - m1) * s0 / s1 for x in sur]


def surrogate_pvalue(observed, surrogate_stats, extreme="high"):
    """Add-one fraction of surrogates at least as extreme as `observed`.
    'high' where bigger=stronger (shock stress); 'low' where
    smaller=stronger (motif DTW distance)."""
    if not surrogate_stats:
        return 1.0
    if extreme == "high":
        k = sum(1 for s in surrogate_stats if s >= observed)
    else:
        k = sum(1 for s in surrogate_stats if s <= observed)
    return (k + 1) / (len(surrogate_stats) + 1)


def oos_split(dates, frac=0.6):
    """Index splitting `dates` into a train head and a held-out tail."""
    return max(1, int(len(dates) * frac))


def _max_stress_of(channels_vals, window):
    """Peak system-stress a channel-value set produces — the statistic a
    shock must beat its surrogates on (reuses the AD-1 stress def)."""
    keys = list(channels_vals)
    if not keys:
        return 0.0
    idx = [str(i) for i in range(len(channels_vals[keys[0]]))]
    stress = system_stress(idx, channels_vals, window=window)
    real = [s for s in stress if s is not None]
    return max(real) if real else 0.0


def shock_significance(observed_stress, channels_vals, window=20,
                       n_surrogates=200, alpha=0.05, rng=None):
    """Admit a shock only if its stress beats BOTH block-bootstrap AND
    phase-randomized surrogates at `alpha` — one null is not enough."""
    rng = rng or random.Random(0)
    block_max, phase_max = [], []
    for _ in range(n_surrogates):
        block_max.append(_max_stress_of(
            {c: block_bootstrap(v, window, rng)
             for c, v in channels_vals.items()}, window))
        phase_max.append(_max_stress_of(
            {c: phase_randomize(v, rng)
             for c, v in channels_vals.items()}, window))
    p_block = surrogate_pvalue(observed_stress, block_max, "high")
    p_phase = surrogate_pvalue(observed_stress, phase_max, "high")
    return {"kind": "shock", "observed": round(observed_stress, 3),
            "p_block": round(p_block, 4), "p_phase": round(p_phase, 4),
            "admitted": p_block < alpha and p_phase < alpha,
            "n_surrogates": n_surrogates, "alpha": alpha}


def held_out_confirms(candidate_date, dates, stress, oos_frac=0.6):
    """Held-out honesty: a shock in the TRAIN head must have a
    comparably-extreme event in the held-out tail too (recurs, not a
    one-off fit); a candidate already in the tail self-confirms."""
    split = oos_split(dates, oos_frac)
    if candidate_date >= dates[split]:
        return True, "candidate in held-out tail"
    obs = stress[dates.index(candidate_date)]
    tail = [s for s in stress[split:] if s is not None]
    if obs is None or not tail:
        return False, "no held-out stress to compare"
    return max(tail) >= 0.7 * obs, f"held-out peak {max(tail):.2f} vs 0.7*{obs:.2f}"


def significance_gate(candidate, lake_dir=None, n_surrogates=200,
                      alpha=0.05, oos_frac=0.6, rng=None):
    """AD-2 verdict for ONE shock candidate: beats both null models AND
    the held-out test → admitted. Reads the core channels once; a motif
    verdict (same surrogate machinery, DTW statistic) is the next slice.
    Returns the full verdict — nothing is admitted silently."""
    if candidate.get("kind") and candidate["kind"] != "shock":
        return {"admitted": False, "status": "motif_gate_pending",
                "candidate": candidate}
    dates, matrix = _aligned_closes(lake_dir)
    if not dates:
        return {"admitted": False, "status": "no_data", "candidate": candidate}
    stress = system_stress(dates, matrix)
    cdate = candidate["date"]
    if cdate not in dates:
        return {"admitted": False, "status": "date_not_in_lake",
                "candidate": candidate}
    observed = stress[dates.index(cdate)]
    sig = shock_significance(observed or 0.0, matrix,
                             n_surrogates=n_surrogates, alpha=alpha, rng=rng)
    oos_ok, oos_detail = held_out_confirms(cdate, dates, stress, oos_frac)
    sig["held_out_confirmed"] = oos_ok
    sig["held_out_detail"] = oos_detail
    sig["date"] = cdate
    sig["admitted"] = sig["admitted"] and oos_ok      # BOTH nulls AND held-out
    return sig


# ------------------------------------------------- AD-3: candidate court

DISCOVERED_EPISODES_PATH = ROOT / "data" / "discovered_episodes.json"


def route_to_court(admitted_candidates, out_path=None, dry_run=False):
    """AD-3: record AD-2-ADMITTED candidates as provisional discovered
    episodes (`source='auto'`) in `data/discovered_episodes.json` — the
    input the dual-catalog tracker (AD-4) reads. Only candidates whose
    verdict says `admitted` are written; the rest are dropped with a
    count. (Full Dept-5 `validation/registry` enrolment — the same
    lifecycle human episodes face — is the remaining slice; this writes
    the provisional artifact it will consume.) Idempotent atomic write."""
    admitted = [c for c in admitted_candidates if c.get("admitted")]
    doc = {"built_at": datetime.now().isoformat(timespec="seconds"),
           "source": "auto", "n_admitted": len(admitted),
           "n_rejected": len(admitted_candidates) - len(admitted),
           "episodes": [{"name": f"auto_{c.get('kind', 'shock')}_"
                                 f"{c.get('date', '?')}",
                         "anchor": c.get("date"), "kind": c.get("kind",
                                                                 "shock"),
                         "source": "auto", "significance": c}
                        for c in admitted]}
    if not dry_run:
        p = Path(out_path or DISCOVERED_EPISODES_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(p)
    return doc


# ------------------------------------------------- AD-4: dual catalog

def merged_catalog(human_path=None, discovered_path=None):
    """AD-4: the union the tracker declares against — human episodes
    (`macro_episodes.yaml`) ∪ auto-discovered (`discovered_episodes.json`).
    Each entry tagged by `source`; an auto episode whose anchor sits far
    from every human anchor is flagged `discovery=True` (a regime the
    human catalog MISSED — the card-worthy find). Agreement (auto near a
    human anchor) is the strongest signal. Missing files degrade to
    whatever exists (honest, never a crash)."""
    try:
        human = FP.load_episodes(human_path)
    except (OSError, ValueError):
        human = []
    try:
        disc = json.loads(
            Path(discovered_path or DISCOVERED_EPISODES_PATH).read_text()
        ).get("episodes", [])
    except (OSError, json.JSONDecodeError):
        disc = []
    human_anchors = [date.fromisoformat(e["anchor"]) for e in human
                     if e.get("anchor")]
    out = [{**e, "source": "human"} for e in human]
    for e in disc:
        anchor = e.get("anchor")
        near = False
        if anchor and human_anchors:
            ad = date.fromisoformat(anchor)
            near = any(abs((ad - h).days) <= 90 for h in human_anchors)
        out.append({**e, "source": "auto", "discovery": not near})
    return {"n_human": len(human), "n_auto": len(disc),
            "n_discoveries": sum(1 for e in out
                                 if e.get("discovery")), "episodes": out}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    d = discover(dry_run=args.dry_run)
    print(json.dumps({"shocks": d["shock_candidates"][:12],
                      "motifs": d["motif_candidates"][:6]},
                     indent=2, default=str))

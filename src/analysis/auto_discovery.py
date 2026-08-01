# MANUAL OFFLINE TOOL — not on any cron/systemd path; keep out of dead-code sweeps (Phase-1 audit 2026-07-25)
"""
src/analysis/auto_discovery.py — the unsupervised discovery engine
==================================================================

docs/auto_discovery_spec.md, made real. The engine proposes its OWN
episodes from 25 years of cross-asset data — no human `macro_episodes.yaml`
input. Human labels become post-hoc annotations, never inputs.

Build state (corrected 2026-08-01 — the previous header claimed AD-2/3/4
were "SCAFFOLDED" long after they were built in `d495de3`; it was written
before that landed and never updated. BUILT-BUT-UNPROVEN is the honest
state, and the distinction matters: none of this has met real data):
  * AD-1 — BUILT + real-lake-run 2026-08-01: unsupervised shock scan over
    CO-STRESS (second-max |z20|, ≥MIN_CHANNELS — see `co_stress` for the
    full autopsy of the RMS statistic it replaced) + slow-burn motif scan
    (DTW self-similarity) → a candidate list. Compute-only, writes
    `data/discovered_candidates.json`, ZERO authority.
  * AD-2 — BUILT for SHOCK candidates: circular-shift + phase-randomized
    surrogates (both hole-pattern-preserving; the block bootstrap was
    retired from the gate — its ~268 splices per surrogate were exactly
    the discontinuity the statistic detects) + a held-out (OOS)
    recurrence test; admits only on BOTH nulls AND held-out. Use
    `gate_many` for scans (ONE shared null — it is candidate-independent).
    **NOT built: motif significance** — any non-shock candidate returns
    `status="motif_gate_pending"` and can never be admitted today. The
    DTW-statistic surrogate test is the missing slice (deliberately
    deferred 2026-08-01: prove the shock gate on real data first).
  * AD-3 — PARTIAL: `route_to_court` writes AD-2-admitted candidates to
    `data/discovered_episodes.json`. Full Dept-5 `validation/registry`
    enrolment — the lifecycle human episodes face — is NOT built.
  * AD-4 — BUILT: `merged_catalog` unions human ∪ auto, tags `source`,
    flags an auto anchor far from every human one as `discovery=True`.

  ZERO EXECUTION AUTHORITY throughout, and on NO schedule (the marker
  above is load-bearing). Nothing here feeds the Stage-B declaration
  clock; an admitted candidate is a research artifact, not an episode.
  11 tests (`tests/test_auto_discovery.py`) run on synthetic lakes.

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
    """Rolling z of the window-day %-change at every day — the SAME
    macro_features.zdelta math (no skew), None until history suffices.
    `baseline` resolves at CALL time (tests shrink it for speed).

    O(n·baseline) instead of the O(n²) it used to be. The old form called
    `zdelta(values[:t+1], ...)` per day, and zdelta rebuilds the whole
    change-list on every call — fine for one pass over a lake, fatal for a
    surrogate null that needs hundreds (2026-08-01: a 200-surrogate null
    over the full 16.8k-session lake projected to ~3.7 HOURS). The change
    list is built once here and each day reads its own trailing window.
    BIT-IDENTICAL by construction: the same `math.fsum` over the same 252
    values in the same order — `test_z_series_matches_zdelta_exactly` pins
    it against the original definition."""
    baseline = baseline if baseline is not None else MF.Z_BASELINE_SESSIONS
    n = len(values)
    out = [None] * n
    if window <= 0 or n < window + 1:
        return out
    # changes[i] is the %-change ENDING at index i+window (zdelta's own def)
    changes = []
    for t in range(window, n):
        v0, v1 = values[t - window], values[t]
        changes.append(None if (v0 is None or v1 is None or v0 == 0)
                       else (v1 - v0) / v0)
    usable = []                      # every non-None change, in order
    for i, cur in enumerate(changes):
        t = i + window               # the day this change belongs to
        if cur is None:
            continue                 # zdelta returns None when current is
        usable.append(cur)
        if len(usable) < baseline:
            continue                 # "insufficient_history"
        tail = usable[-baseline:]
        mean = math.fsum(tail) / baseline
        var = math.fsum((c - mean) ** 2 for c in tail) / baseline
        if var == 0.0:
            continue                 # degenerate baseline -> None
        out[t] = (cur - mean) / math.sqrt(var)
    return out


# How many channels must report a z before a day is even ELIGIBLE for a
# simultaneity read. You cannot measure "the whole system moving at once"
# from one bond yield — and pre-1973 the lake IS one bond yield. With the
# full core set this floor makes the scannable window ~1988-06+ (BRENT's
# z warms up), which is the honest span (owner ruling 2026-08-01).
MIN_CHANNELS = 3


def co_stress(dates, matrix, window=20, min_channels=MIN_CHANNELS):
    """Per-day simultaneity = the SECOND-LARGEST |z| among the channels
    reporting that day. Large only when at least TWO assets are in shock
    at once — the physical property the engine actually hunts. None on
    days with fewer than `min_channels` z-scores (ineligible, never 0).

    THE 2026-08-01 REDEFINITION (owner ruling). The original statistic —
    RMS of |z| over the channels PRESENT — had a variable divisor, so it
    was not comparable across coverage eras: one channel at |z|=15.6 on a
    2-channel day scored RMS 11.0 and out-ranked COVID (4 channels, 5.27).
    Consequences, all verified on the real lake before the change:
      * NO surrogate null could be beaten (its max always found a big
        single move on a thin-coverage day — p saturated at 1.0000 across
        two different surrogate models);
      * six of AD-1's top-25 "shocks" were single-channel US10Y ghosts
        from the 1962-72 era;
      * crisis anchors were MIS-DATED toward thin-coverage days — under
        co-stress COVID re-anchors from 2020-05-19 to 2020-03-18 (the
        actual panic peak), GFC 10-22→10-24, taper 05-29→05-28;
      * 1991-07-03 (old #1, stress 6.25) demotes to co-stress 0.36: in
        this lake July 1991 is a USDINR devaluation — ONE channel — and
        an honest simultaneity statistic says so.
    The second-max has no divisor: a lone spike scores ~0 because the
    second-strongest mover was quiet. Under it the null is finally both
    beatable and graded (COVID p_circ 0.0588 at 50 surrogates vs a flat
    1.0000 before)."""
    zbychan = {c: _z_series(v, window) for c, v in matrix.items()}
    out = []
    for i in range(len(dates)):
        zs = sorted((abs(zbychan[c][i]) for c in matrix
                     if zbychan[c][i] is not None), reverse=True)
        out.append(zs[1] if len(zs) >= min_channels else None)
    return out


def system_stress(dates, matrix, window=20):
    """SUPERSEDED by `co_stress` (2026-08-01) — see its docstring for the
    full autopsy. Kept only as a reference implementation of the old RMS
    read; NOTHING in the shock pipeline calls it any more. Do not wire it
    back into a gate: its variable divisor makes it incomparable across
    coverage eras and no surrogate null can be specified for it."""
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
    """Unsupervised shock anchors: local peaks of CO-STRESS (second-max
    |z|, ≥MIN_CHANNELS — the 2026-08-01 statistic), kept greedily
    strongest-first with a min-gap so one crisis = one anchor.
    Returns [{date, stress}] ranked — UNNAMED, UNPROVEN (AD-2 gates).
    `stress` carries the co-stress value (key name kept for artifact
    compatibility; `stat` names the definition)."""
    dates, matrix = _aligned_closes(lake_dir)
    stress = co_stress(dates, matrix)
    scored = sorted(((s, d) for d, s in zip(dates, stress)
                     if s is not None), reverse=True)
    chosen = []
    for s, d in scored:
        dd = date.fromisoformat(d)
        if all(abs((dd - date.fromisoformat(c["date"])).days) >= min_gap_days
               for c in chosen):
            chosen.append({"date": d, "stress": round(s, 3),
                           "stat": "co_stress_2ndmax_z20"})
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


def _with_same_holes(series, new_values):
    """Scatter `new_values` back into `series`'s non-None slots, keeping
    LENGTH and HOLE PATTERN identical.

    THE RAGGED-MISSINGNESS FIX (2026-08-01). The surrogate builders used
    to return only the non-None values, so on the real lake — where
    coverage is ragged (US10Y from 1962, USDINR 1973, BRENT 1987, DXY
    2006) — each channel's surrogate came back a different length and
    `_max_stress_of` overran with an IndexError. AD-2 had never been
    runnable in production. Padding or bounds-checking would have been
    WRONG rather than merely incomplete: `system_stress` measures
    cross-asset SIMULTANEITY, so channels of unequal length are no longer
    time-aligned and the RMS denominator differs between the observed
    statistic and its null — p-values computed under conditions the
    observation never faced. The null matrix must be exactly as ragged as
    the observed one, which is what this guarantees."""
    out, k = [], 0
    for v in series:
        if v is None:
            out.append(None)
        else:
            out.append(new_values[k] if k < len(new_values) else None)
            k += 1
    return out


def circular_shift(series, rng):
    """ONE splice. Rotate a channel's observed values by a random offset,
    wrapping around, then restore the hole pattern.

    THE SPLICE FIX (2026-08-01, owner ruling). `block_bootstrap` below
    concatenates random contiguous blocks, so EVERY junction is an
    artificial discontinuity — and `system_stress` is built to detect
    exactly that (a large simultaneous z-move). A 5,363-point surrogate
    carried ~268 such splices and the statistic takes the MAX over all of
    them, so the null measured "worst splice artifact in 268 tries"
    against "worst real crisis in 20 years": its MEDIAN max-stress (4.66)
    nearly beat COVID (5.27) and p saturated at 1.0000. A rotation
    introduces exactly one wrap-point and otherwise preserves the
    channel's autocorrelation and marginal distribution EXACTLY.

    THE NULL THIS IMPLIES, stated plainly: each channel is rotated by its
    OWN independent offset, so every channel keeps its real dynamics while
    the RELATIVE TIMING between channels is randomised. H0 becomes "the
    four channels move as they really do, but their co-movement lines up
    by chance" — which is the correct null for a simultaneity statistic.
    (Rotating all channels by the SAME offset would be useless: the
    surrogate would just be the original series time-shifted, containing
    COVID intact, and p would be ~1 by construction.)"""
    vals = [v for v in series if v is not None]
    n = len(vals)
    if n < 2:
        return list(series)
    k = rng.randrange(n)
    return _with_same_holes(series, vals[k:] + vals[:k])


def block_bootstrap(series, block_len, rng):
    """Same-length surrogate from random contiguous blocks — preserves
    within-block autocorrelation, destroys the rest.

    SUPERSEDED as AD-2's null by `circular_shift` (2026-08-01): the block
    junctions are exactly the discontinuity `system_stress` detects, so
    this null is unbeatable by real events. Kept as a primitive (and
    hole-correct since the same date) because it remains a valid
    bootstrap for statistics that are NOT discontinuity-sensitive — but
    do not wire it back into a max-stress gate."""
    vals = [v for v in series if v is not None]
    n = len(vals)
    if n == 0:
        return list(series)
    out = []
    while len(out) < n:
        start = rng.randint(0, max(0, n - block_len))
        out.extend(vals[start:start + block_len])
    return _with_same_holes(series, out[:n])


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
        return list(series)
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
    rescaled = (list(vals) if s1 == 0
                else [m0 + (x - m1) * s0 / s1 for x in sur])
    return _with_same_holes(series, rescaled)   # ragged-missingness fix


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
    """Peak CO-STRESS a channel-value set produces — the statistic a
    shock must beat its surrogates on. SAME definition and SAME
    ≥MIN_CHANNELS eligibility as the observed side: a null computed under
    different conditions than the observation is not a null (the
    2026-08-01 lesson, twice over). Surrogates keep each channel's length
    and hole pattern, so the index is simply the longest channel's."""
    keys = list(channels_vals)
    if not keys:
        return 0.0
    n = max(len(v) for v in channels_vals.values())
    idx = [str(i) for i in range(n)]
    stress = co_stress(idx, channels_vals, window=window)
    real = [s for s in stress if s is not None]
    return max(real) if real else 0.0


def build_null(channels_vals, window=20, n_surrogates=200, rng=None) -> dict:
    """The max-stress null under both surrogate models.

    CANDIDATE-INDEPENDENT BY CONSTRUCTION — it depends only on the lake,
    never on which day is being tested; only `observed` differs per
    candidate. So build it ONCE and score every candidate against it:
    the per-candidate rebuild `shock_significance` used to do was a pure
    N-fold waste (2026-08-01: 21 candidates × the same computation, ~23
    min at even 10 surrogates). This is the standard common-random-numbers
    arrangement; the only consequence to disclose is that p-values across
    candidates become correlated through one shared draw, which is fine
    for admission decisions and matters only if candidates were ranked
    against each other."""
    rng = rng or random.Random(0)
    circular_max, phase_max = [], []
    for _ in range(n_surrogates):
        circular_max.append(_max_stress_of(
            {c: circular_shift(v, rng) for c, v in channels_vals.items()},
            window))
        phase_max.append(_max_stress_of(
            {c: phase_randomize(v, rng) for c, v in channels_vals.items()},
            window))
    return {"circular": circular_max, "phase": phase_max,
            "n_surrogates": n_surrogates, "window": window}


def shock_significance(observed_stress, channels_vals, window=20,
                       n_surrogates=200, alpha=0.05, rng=None, null=None):
    """Admit a shock only if its stress beats BOTH the circular-shift AND
    the phase-randomized null at `alpha` — one null is not enough.

    Both nulls destroy cross-channel SIMULTANEITY (independent rotation /
    independent phases) while preserving each channel's own dynamics, so
    they answer the question the statistic actually asks. Pass a
    precomputed `null` from `build_null` to score many candidates against
    one draw; omit it and one is built here (convenient, N-fold slower)."""
    null = null or build_null(channels_vals, window, n_surrogates,
                              rng or random.Random(0))
    p_circ = surrogate_pvalue(observed_stress, null["circular"], "high")
    p_phase = surrogate_pvalue(observed_stress, null["phase"], "high")
    return {"kind": "shock", "observed": round(observed_stress, 3),
            "p_circular": round(p_circ, 4), "p_phase": round(p_phase, 4),
            "admitted": p_circ < alpha and p_phase < alpha,
            "n_surrogates": null.get("n_surrogates", n_surrogates),
            "alpha": alpha}


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


def gate_many(candidates, lake_dir=None, n_surrogates=200, alpha=0.05,
              oos_frac=0.6, rng=None):
    """AD-2 over a whole candidate list against ONE shared null — the way
    to run a real scan (`significance_gate` per candidate rebuilds the
    same null every time). Returns [verdict], order preserved."""
    dates, matrix = _aligned_closes(lake_dir)
    if not dates:
        return [{"admitted": False, "status": "no_data", "candidate": c}
                for c in candidates]
    stress = co_stress(dates, matrix)
    null = build_null(matrix, 20, n_surrogates, rng or random.Random(0))
    out = []
    for cand in candidates:
        if cand.get("kind") and cand["kind"] != "shock":
            out.append({"admitted": False, "status": "motif_gate_pending",
                        "candidate": cand})
            continue
        cdate = cand["date"]
        if cdate not in dates:
            out.append({"admitted": False, "status": "date_not_in_lake",
                        "candidate": cand})
            continue
        observed = stress[dates.index(cdate)]
        sig = shock_significance(observed or 0.0, matrix, alpha=alpha,
                                 null=null)
        ok, detail = held_out_confirms(cdate, dates, stress, oos_frac)
        sig.update({"held_out_confirmed": ok, "held_out_detail": detail,
                    "date": cdate, "admitted": sig["admitted"] and ok})
        out.append(sig)
    return out


def significance_gate(candidate, lake_dir=None, n_surrogates=200,
                      alpha=0.05, oos_frac=0.6, rng=None, null=None):
    """AD-2 verdict for ONE shock candidate: beats both null models AND
    the held-out test → admitted. Reads the core channels once; a motif
    verdict (same surrogate machinery, DTW statistic) is the next slice.
    Returns the full verdict — nothing is admitted silently.
    For a whole scan use `gate_many` (one shared null)."""
    if candidate.get("kind") and candidate["kind"] != "shock":
        return {"admitted": False, "status": "motif_gate_pending",
                "candidate": candidate}
    dates, matrix = _aligned_closes(lake_dir)
    if not dates:
        return {"admitted": False, "status": "no_data", "candidate": candidate}
    stress = co_stress(dates, matrix)
    cdate = candidate["date"]
    if cdate not in dates:
        return {"admitted": False, "status": "date_not_in_lake",
                "candidate": candidate}
    observed = stress[dates.index(cdate)]
    sig = shock_significance(observed or 0.0, matrix,
                             n_surrogates=n_surrogates, alpha=alpha,
                             rng=rng, null=null)
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

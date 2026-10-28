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
  * AD-1 — BUILT + real-lake-run 2026-08-01 + Phase 2 2026-10-28: unsupervised
    shock scan over CO-STRESS (`co_stress`), Mahalanobis distributional break
    (`mahalanobis_stress`), and Frobenius rolling-correlation-matrix shift
    (`frobenius_corr_break`), plus slow-burn motif scan (`scan_motifs`, DTW
    self-similarity) → a candidate list. Compute-only, writes
    `data/discovered_candidates.json`, ZERO authority.
  * AD-2 — BUILT for BOTH SHOCK and MOTIF candidates (Phase 2, 2026-10-28):
    circular-shift + phase-randomized surrogates (both hole-pattern-preserving)
    + held-out (OOS) recurrence test; motif candidates additionally require
    window-perturbation stability (`motif_stability`). An incomplete motif
    stub without `[start, end]` window bounds returns
    `status="motif_gate_pending"`.
  * AD-3 — BUILT (Phase 2, 2026-10-28): `route_to_court` writes AD-2-admitted
    candidates to `data/discovered_episodes.json` and, when a `conn` is
    supplied (or via `enrol_in_registry`), enrols admitted discoveries into
    Department 5's `src/validation/registry.py` (`CANDIDATE` lifecycle).
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


def _invert_matrix(mat: list[list[float]]) -> list[list[float]] | None:
    """Pure-stdlib Gauss-Jordan matrix inversion with partial pivoting.
    Returns None if singular."""
    k = len(mat)
    if k == 0:
        return []
    aug = [
        [float(mat[r][c]) for c in range(k)]
        + [1.0 if r == c else 0.0 for c in range(k)]
        for r in range(k)
    ]
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        div = aug[col][col]
        for j in range(2 * k):
            aug[col][j] /= div
        for r in range(k):
            if r == col:
                continue
            factor = aug[r][col]
            if factor != 0.0:
                for j in range(2 * k):
                    aug[r][j] -= factor * aug[col][j]
    return [row[k:] for row in aug]


def mahalanobis_stress(dates, matrix, window=20, baseline=None,
                       min_channels=MIN_CHANNELS, ridge=1e-3):
    """Dimension-normalized Mahalanobis distributional break (`docs/auto_discovery_spec.md` §1.1):
    `sqrt((z_t^T Sigma_t^-1 z_t) / k)` across the `k >= min_channels` active
    channels on day `t`, where `Sigma_t` is the trailing empirical covariance
    over up to `baseline` prior joint observations (with diagonal `ridge`).
    Pure stdlib (Gauss-Jordan inversion)."""
    if baseline is None:
        baseline = MF.Z_BASELINE_SESSIONS
    zbychan = {c: _z_series(v, window, baseline) for c, v in matrix.items()}
    chans = sorted(matrix)
    n = len(dates)
    out = []
    for i in range(n):
        active = [c for c in chans if zbychan[c][i] is not None]
        k = len(active)
        if k < min_channels:
            out.append(None)
            continue
        z_now = [float(zbychan[c][i]) for c in active]
        hist = []
        lo = max(0, i - baseline)
        for t in range(lo, i):
            if all(zbychan[c][t] is not None for c in active):
                hist.append([float(zbychan[c][t]) for c in active])
        if len(hist) < max(k + 2, 8):
            # Fallback to diagonal unit-variance RMS when joint history is thin
            out.append(math.sqrt(math.fsum(x * x for x in z_now) / k))
            continue
        m = len(hist)
        means = [math.fsum(row[col] for row in hist) / m for col in range(k)]
        cov = [[0.0] * k for _ in range(k)]
        for r in range(k):
            for c in range(r, k):
                val = math.fsum(
                    (row[r] - means[r]) * (row[c] - means[c]) for row in hist
                ) / m
                if r == c:
                    val += ridge
                cov[r][c] = val
                cov[c][r] = val
        inv = _invert_matrix(cov)
        if inv is None:
            out.append(math.sqrt(math.fsum(x * x for x in z_now) / k))
            continue
        quad = 0.0
        for r in range(k):
            quad += z_now[r] * math.fsum(inv[r][c] * z_now[c] for c in range(k))
        out.append(math.sqrt(max(0.0, quad) / k))
    return out


def frobenius_corr_break(dates, matrix, window=5, corr_window=20,
                         shift_lag=10, min_channels=MIN_CHANNELS):
    """Dimension-normalized Frobenius norm of the rolling correlation shift
    (`docs/auto_discovery_spec.md` §1.2):
    `||R_t - R_{t - shift_lag}||_F / sqrt(k * (k - 1))` over active channels
    with `k >= min_channels`. Pure stdlib."""
    zbychan = {c: _z_series(v, window) for c, v in matrix.items()}
    chans = sorted(matrix)
    n = len(dates)

    def _corr_pair(ca, cb, end_idx):
        lo = max(0, end_idx - corr_window + 1)
        xs, ys = [], []
        for t in range(lo, end_idx + 1):
            va, vb = zbychan[ca][t], zbychan[cb][t]
            if va is not None and vb is not None:
                xs.append(float(va))
                ys.append(float(vb))
        if len(xs) < max(5, corr_window // 2):
            return None
        mx, sx = _mean_std(xs)
        my, sy = _mean_std(ys)
        if sx < 1e-12 or sy < 1e-12:
            return None
        cov = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
        return max(-1.0, min(1.0, cov / (sx * sy)))

    out = []
    for i in range(n):
        prev_i = i - shift_lag
        if prev_i < corr_window:
            out.append(None)
            continue
        active = [
            c for c in chans
            if zbychan[c][i] is not None and zbychan[c][prev_i] is not None
        ]
        if len(active) < min_channels:
            out.append(None)
            continue
        diffs_sq = []
        valid_channels = set()
        for idx_a in range(len(active)):
            for idx_b in range(idx_a + 1, len(active)):
                ca, cb = active[idx_a], active[idx_b]
                r_now = _corr_pair(ca, cb, i)
                r_prev = _corr_pair(ca, cb, prev_i)
                if r_now is not None and r_prev is not None:
                    diffs_sq.append((r_now - r_prev) ** 2)
                    valid_channels.add(ca)
                    valid_channels.add(cb)
        if len(valid_channels) < min_channels or not diffs_sq:
            out.append(None)
            continue
        # 2 * sum_{a<b} (r_now - r_prev)^2 divided by k*(k-1) = mean squared off-diag shift
        out.append(math.sqrt(math.fsum(diffs_sq) / len(diffs_sq)))
    return out


def rank_shock_candidates(lake_dir=None, top_n=SHOCK_TOP_N,
                          min_gap_days=SHOCK_MIN_GAP_DAYS,
                          stat="co_stress"):
    """Unsupervised shock anchors: local peaks of CO-STRESS (second-max
    |z|, ≥MIN_CHANNELS — the 2026-08-01 statistic), Mahalanobis break
    (`stat='mahalanobis'`), or Frobenius correlation shift
    (`stat='frobenius'`), kept greedily strongest-first with a min-gap so
    one crisis = one anchor.
    Returns [{date, stress, stat}] ranked — UNNAMED, UNPROVEN (AD-2 gates)."""
    dates, matrix = _aligned_closes(lake_dir)
    if stat == "mahalanobis":
        stress = mahalanobis_stress(dates, matrix)
        stat_label = "mahalanobis_z20"
    elif stat == "frobenius":
        stress = frobenius_corr_break(dates, matrix)
        stat_label = "frobenius_corr_break"
    else:
        stress = co_stress(dates, matrix)
        stat_label = "co_stress_2ndmax_z20"
    scored = sorted(((s, d) for d, s in zip(dates, stress)
                     if s is not None), reverse=True)
    chosen = []
    for s, d in scored:
        dd = date.fromisoformat(d)
        if all(abs((dd - date.fromisoformat(c["date"])).days) >= min_gap_days
               for c in chosen):
            chosen.append({"date": d, "stress": round(s, 3),
                           "stat": stat_label})
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
    from src.validation import stat_gates

    dates, _ = _aligned_closes(lake_dir)
    win_start = dates[0] if dates else "2016-01-01"
    win_end = dates[-1] if dates else "2023-12-31"
    doc = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "note": ("AD-1 unsupervised candidates — UNPROVEN. Not episodes "
                 "until AD-2 significance + the candidate court pass."),
        "provenance": stat_gates.backfill_hypothesis_metadata(
            window_start=win_start,
            window_end=win_end,
            universe_kind="macro_and_survivor_lake",
        ),
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


def _window_rows_from_z(zbychan: dict, start_idx: int, end_idx: int) -> list[dict]:
    """Slice `[start_idx, end_idx]` (inclusive) into fingerprint channel rows."""
    rows = []
    for i in range(start_idx, end_idx + 1):
        row = {
            f"{c}:z60": zbychan[c][i]
            for c in zbychan
            if i < len(zbychan[c]) and zbychan[c][i] is not None
        }
        rows.append(row)
    return rows


def motif_stability(dates, zbychan, span_a: tuple[int, int],
                    span_b: tuple[int, int], observed_dtw: float) -> tuple[bool, float]:
    """Window-perturbation stability (`docs/auto_discovery_spec.md` §3.4):
    perturb window endpoints by ±1 session and verify DTW distance remains
    within `max(1.35 * observed_dtw, observed_dtw + 0.35)`."""
    n = len(dates)
    a0, a1 = span_a
    b0, b1 = span_b
    tol = max(1.35 * observed_dtw, observed_dtw + 0.35)
    checks = []
    for da in (-1, 0, 1):
        for db in (-1, 0, 1):
            if da == 0 and db == 0:
                continue
            na0, na1 = a0 + da, a1 + da
            nb0, nb1 = b0 + db, b1 + db
            if na0 < 0 or na1 >= n or nb0 < 0 or nb1 >= n or nb0 <= na1:
                continue
            ra = _window_rows_from_z(zbychan, na0, na1)
            rb = _window_rows_from_z(zbychan, nb0, nb1)
            d, _ = FP.dtw_distance(ra, rb)
            if d is not None:
                checks.append(d <= tol)
    if not checks:
        return True, 1.0
    score = sum(1 for ok in checks if ok) / len(checks)
    return score >= 0.65, round(score, 4)


def held_out_motif_confirms(dates, zbychan, span_a: tuple[int, int],
                            span_b: tuple[int, int], observed_dtw: float,
                            oos_frac: float = 0.6) -> tuple[bool, str]:
    """Out-of-sample recurrence for motifs (`docs/auto_discovery_spec.md` §3.3):
    confirms if the recurrence window `b` ends in the held-out tail, or if a
    sliding window in the held-out tail matches window `a` within tolerance."""
    split = oos_split(dates, oos_frac)
    if span_b[1] >= split:
        return True, "motif recurrence window in held-out tail"
    win_len = span_a[1] - span_a[0] + 1
    if len(dates) - split < win_len:
        return False, "held-out tail shorter than motif window"
    ra = _window_rows_from_z(zbychan, span_a[0], span_a[1])
    best_tail = None
    step = max(1, win_len // 4)
    for s in range(split, len(dates) - win_len + 1, step):
        rt = _window_rows_from_z(zbychan, s, s + win_len - 1)
        d, _ = FP.dtw_distance(ra, rt)
        if d is not None and (best_tail is None or d < best_tail):
            best_tail = d
    if best_tail is None:
        return False, "no comparable window in held-out tail"
    tol = max(1.35 * observed_dtw, observed_dtw + 0.35)
    return best_tail <= tol, f"held-out DTW {best_tail:.3f} vs tol {tol:.3f}"


def motif_significance(candidate: dict, dates: list, matrix: dict,
                       z_window: int = 60, n_surrogates: int = 50,
                       alpha: float = 0.05, oos_frac: float = 0.6,
                       rng=None) -> dict:
    """AD-2 DTW surrogate significance gate for a motif candidate
    `{"a": [start_a, end_a], "b": [start_b, end_b], ...}`.
    Requires valid 2-endpoint windows `a` and `b`; an incomplete stub returns
    `status="motif_gate_pending"`."""
    a = candidate.get("a") or []
    b = candidate.get("b") or []
    if len(a) < 2 or len(b) < 2:
        return {"admitted": False, "status": "motif_gate_pending",
                "candidate": candidate}
    if not dates:
        return {"admitted": False, "status": "no_data", "candidate": candidate}
    if a[0] not in dates or a[1] not in dates or b[0] not in dates or b[1] not in dates:
        return {"admitted": False, "status": "date_not_in_lake",
                "candidate": candidate}

    span_a = (dates.index(a[0]), dates.index(a[1]))
    span_b = (dates.index(b[0]), dates.index(b[1]))
    win_len = max(1, span_a[1] - span_a[0] + 1)
    eff_z = int(candidate.get("z_window") or min(z_window, max(3, win_len // 2)))

    zbychan = {c: _z_series(v, eff_z) for c, v in matrix.items()}
    rows_a = _window_rows_from_z(zbychan, span_a[0], span_a[1])
    rows_b = _window_rows_from_z(zbychan, span_b[0], span_b[1])
    obs_dtw, obs_cov = FP.dtw_distance(rows_a, rows_b)
    if obs_dtw is None:
        return {"admitted": False, "status": "unobservable_motif",
                "candidate": candidate}

    rng = rng or random.Random(0)
    circ_dtws, phase_dtws = [], []
    for _ in range(n_surrogates):
        c_mat = {c: circular_shift(v, rng) for c, v in matrix.items()}
        c_z = {c: _z_series(v, eff_z) for c, v in c_mat.items()}
        dc, _ = FP.dtw_distance(
            _window_rows_from_z(c_z, span_a[0], span_a[1]),
            _window_rows_from_z(c_z, span_b[0], span_b[1]),
        )
        if dc is not None:
            circ_dtws.append(dc)

        p_mat = {c: phase_randomize(v, rng) for c, v in matrix.items()}
        p_z = {c: _z_series(v, eff_z) for c, v in p_mat.items()}
        dp, _ = FP.dtw_distance(
            _window_rows_from_z(p_z, span_a[0], span_a[1]),
            _window_rows_from_z(p_z, span_b[0], span_b[1]),
        )
        if dp is not None:
            phase_dtws.append(dp)

    p_circ = surrogate_pvalue(obs_dtw, circ_dtws, "low")
    p_phase = surrogate_pvalue(obs_dtw, phase_dtws, "low")
    stab_ok, stab_score = motif_stability(dates, zbychan, span_a, span_b, obs_dtw)
    oos_ok, oos_detail = held_out_motif_confirms(
        dates, zbychan, span_a, span_b, obs_dtw, oos_frac=oos_frac
    )
    admitted = (p_circ < alpha) and (p_phase < alpha) and stab_ok and oos_ok
    return {
        "kind": "motif",
        "a": list(a[:2]),
        "b": list(b[:2]),
        "date": b[0],
        "observed_dtw": round(obs_dtw, 4),
        "coverage": obs_cov,
        "p_circular": round(p_circ, 4),
        "p_phase": round(p_phase, 4),
        "stability_ok": stab_ok,
        "stability_score": stab_score,
        "held_out_confirmed": oos_ok,
        "held_out_detail": oos_detail,
        "admitted": admitted,
        "n_surrogates": n_surrogates,
        "alpha": alpha,
    }


def _is_motif_candidate(cand: dict) -> bool:
    return bool(
        (cand.get("kind") and cand["kind"] != "shock")
        or ("a" in cand and "b" in cand and "date" not in cand)
    )


def gate_many(candidates, lake_dir=None, n_surrogates=200, alpha=0.05,
              oos_frac=0.6, rng=None):
    """AD-2 over a whole candidate list against ONE shared null — the way
    to run a real scan (`significance_gate` per candidate rebuilds the
    same null every time). Returns [verdict], order preserved."""
    dates, matrix = _aligned_closes(lake_dir)
    if not dates:
        out_empty = []
        for c in candidates:
            if _is_motif_candidate(c) and (len(c.get("a") or ()) < 2 or len(c.get("b") or ()) < 2):
                out_empty.append({"admitted": False, "status": "motif_gate_pending", "candidate": c})
            else:
                out_empty.append({"admitted": False, "status": "no_data", "candidate": c})
        return out_empty
    stress = co_stress(dates, matrix)
    base_rng = rng or random.Random(0)
    null = build_null(matrix, 20, n_surrogates, base_rng)
    out = []
    for cand in candidates:
        if _is_motif_candidate(cand):
            out.append(motif_significance(
                cand, dates, matrix, n_surrogates=min(n_surrogates, 50),
                alpha=alpha, oos_frac=oos_frac, rng=base_rng
            ))
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
    """AD-2 verdict for ONE shock or motif candidate: beats both null models AND
    the held-out test → admitted. Reads the core channels once.
    Returns the full verdict — nothing is admitted silently.
    For a whole scan use `gate_many` (one shared null)."""
    if _is_motif_candidate(candidate):
        if len(candidate.get("a") or ()) < 2 or len(candidate.get("b") or ()) < 2:
            return {"admitted": False, "status": "motif_gate_pending",
                    "candidate": candidate}
        dates, matrix = _aligned_closes(lake_dir)
        return motif_significance(
            candidate, dates, matrix, n_surrogates=n_surrogates,
            alpha=alpha, oos_frac=oos_frac, rng=rng
        )
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


def enrol_in_registry(admitted_candidates: list, conn,
                      mining_run: str = "auto_discovery") -> list[dict]:
    """AD-3 Department-5 `validation/registry` enrolment (`docs/auto_discovery_spec.md` §4):
    mint a frozen `CANDIDATE` hypothesis in `candidate_patterns` for each
    AD-2-admitted shock or motif candidate. Idempotent on canonical definition."""
    from src.validation import registry

    enrolments = []
    for c in admitted_candidates or []:
        if not c.get("admitted"):
            continue
        kind = c.get("kind", "shock")
        anchor = c.get("date") or (c.get("b") or [None])[0] or "?"
        if kind == "motif":
            definition = {
                "source": "auto_discovery",
                "kind": "auto_motif",
                "window_a": list((c.get("a") or [])[:2]),
                "window_b": list((c.get("b") or [])[:2]),
            }
            desc = f"Auto-discovered slow-burn motif {definition['window_a']} ~ {definition['window_b']}"
            disc_win = f"{(c.get('a') or ['?'])[0]}..{(c.get('b') or ['?','?'])[-1]}"
        else:
            definition = {
                "source": "auto_discovery",
                "kind": "auto_shock",
                "anchor": anchor,
                "stat": c.get("stat", "co_stress_2ndmax_z20"),
            }
            desc = f"Auto-discovered cross-asset shock at {anchor}"
            disc_win = str(anchor)

        fdr_q = max(
            float(c.get("p_circular") or 0.05),
            float(c.get("p_phase") or 0.05),
        )
        reg = registry.register(
            conn,
            kind=f"auto_{kind}",
            definition=definition,
            description=desc,
            mining_run=mining_run,
            discovery_window=disc_win,
            support_n=1,
            fdr_q=round(fdr_q, 4),
            insample_stats={k: v for k, v in c.items() if k != "candidate"},
        )
        enrolments.append({"anchor": anchor, "kind": kind, **reg})
    return enrolments


def route_to_court(admitted_candidates, out_path=None, dry_run=False,
                   conn=None, mining_run="auto_discovery"):
    """AD-3: record AD-2-ADMITTED candidates as provisional discovered
    episodes (`source='auto'`) in `data/discovered_episodes.json` — the
    input the dual-catalog tracker (AD-4) reads — AND, when `conn` is
    supplied, enrol every admitted candidate into Department 5's
    `src/validation/registry.py` (`CANDIDATE` lifecycle). Only candidates
    whose verdict says `admitted` are written; the rest are dropped with a
    count. Idempotent atomic write."""
    admitted = [c for c in admitted_candidates if c.get("admitted")]
    doc = {"built_at": datetime.now().isoformat(timespec="seconds"),
           "source": "auto", "n_admitted": len(admitted),
           "n_rejected": len(admitted_candidates) - len(admitted),
           "episodes": [{"name": f"auto_{c.get('kind', 'shock')}_"
                                 f"{c.get('date') or (c.get('b') or ['?'])[0]}",
                         "anchor": c.get("date") or (c.get("b") or [None])[0],
                         "kind": c.get("kind", "shock"),
                         "source": "auto", "significance": c}
                        for c in admitted]}
    if conn is not None:
        doc["registry_enrolments"] = enrol_in_registry(
            admitted, conn, mining_run=mining_run
        )
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

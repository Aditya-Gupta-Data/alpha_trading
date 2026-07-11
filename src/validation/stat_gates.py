"""
src/validation/stat_gates.py — the shared anti-hallucination toolkit
====================================================================

Phase 4 of docs/HOLY_GRAIL_PLAN.md (§7.1). The whole discovery brain lives
or dies on trust: one confidently-presented spurious pattern trains the
human to ignore the real ones (the skeptic's abstention doctrine, #35/#44,
applied to everything). Every noise-control primitive lives HERE — no
miner or promotion gate ever defines its own threshold inline, so gates
can only be loosened deliberately, in a logged decision, never eroded
ad hoc.

Pure Python + math/random stdlib. Every function is deterministic given
its inputs (permutation nulls take an explicit seed).

The floors (echoing #55's 30-trade evolution floor and #44's MIN_* refusal
constants):

  MIN_SUPPORT_ITEMSET      15   co-occurrence candidates below this are noise
  MIN_SUPPORT_SEQUENCE      8   'A then B' occurrences below this are noise
  MIN_PROMOTION_RESOLUTIONS 10  out-of-discovery resolutions before VALIDATED
  MIN_VALIDATION_N         30   full-window floor (real + sim, strata shown)
  MIN_AFFINITY_EPISODES     8   affinity evidence counts EPISODES, not deals
  FDR_Q                  0.10   Benjamini-Hochberg ceiling per mining batch

Real-vs-simulated policy (the panel's locked rule): simulated evidence can
SUPPORT a promotion but never solely justify one — `promotable` refuses
zero-real-evidence cases regardless of sim volume. Learning consumers
(tuner, skeptic, miners) must exclude EXCLUDED_REF_PREFIXES so the system
never learns from its own hypotheses.
"""

import json
import math
import random
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"


def configured_floors(config_path=None) -> dict:
    """The active floors: BALANCED defaults, overridden by any
    harness_* key in config.json. Read per call so a tuning edit takes
    effect without a restart. Never raises — a broken config keeps the
    defaults."""
    floors = {
        "min_resolutions": MIN_PROMOTION_RESOLUTIONS,
        "min_validation_n": MIN_VALIDATION_N,
        "min_support_itemset": MIN_SUPPORT_ITEMSET,
        "min_support_sequence": MIN_SUPPORT_SEQUENCE,
        "min_affinity_episodes": MIN_AFFINITY_EPISODES,
        "min_view_resolutions": MIN_VIEW_RESOLUTIONS,
        "fdr_q": FDR_Q,
    }
    path = Path(config_path) if config_path is not None else _CONFIG_PATH
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return floors
    key_map = {
        "min_resolutions": ("harness_min_resolutions", int),
        "min_validation_n": ("harness_min_validation_n", int),
        "min_support_itemset": ("harness_min_support_itemset", int),
        "min_support_sequence": ("harness_min_support_sequence", int),
        "min_affinity_episodes": ("harness_min_affinity_episodes", int),
        "min_view_resolutions": ("harness_min_view_resolutions", int),
        "fdr_q": ("harness_fdr_q", float),
    }
    for out_key, (cfg_key, cast) in key_map.items():
        if cfg_key in raw:
            try:
                floors[out_key] = cast(raw[cfg_key])
            except (ValueError, TypeError):
                pass
    return floors

# --------------------------------------------------------------- floors

# Defaults are the owner's BALANCED strictness (chosen 2026-07-11): patterns
# surface a bit sooner than the strict panel numbers, with more of them
# flagged-then-killed. Every floor is overridable in config.json
# (harness_min_resolutions / harness_fdr_q / harness_min_support_itemset /
# harness_min_support_sequence / harness_min_validation_n /
# harness_min_affinity_episodes) so strictness re-tunes without a code
# change — see configured_floors().
MIN_SUPPORT_ITEMSET = 12
MIN_SUPPORT_SEQUENCE = 6
MIN_PROMOTION_RESOLUTIONS = 7
MIN_VALIDATION_N = 20
MIN_AFFINITY_EPISODES = 6
# The pattern×strategy evidence view (HOLY_GRAIL_PLAN §8.6) refuses to
# render a strategy row below this many REAL resolutions — a 2-real
# structure comparison is noise wearing a preference. Distinct from the
# promotion floor: this only gates what the descriptive view SHOWS.
MIN_VIEW_RESOLUTIONS = 5
FDR_Q = 0.15

# Journal-ref namespaces that are the system's own hypotheses/replays —
# NEVER training data for tuner/skeptic/miners (self-poisoning guard).
EXCLUDED_REF_PREFIXES = ("sim:", "shadow:", "trial:", "placebo:")
# Tag namespaces the miners must not re-consume (tautological rediscovery).
EXCLUDED_TAG_PREFIXES = ("auto:",)


def is_learnable_ref(journal_ref: str) -> bool:
    return not str(journal_ref or "").startswith(EXCLUDED_REF_PREFIXES)


def is_minable_tag(tag: str) -> bool:
    return not str(tag or "").startswith(EXCLUDED_TAG_PREFIXES)


# ------------------------------------------------------------- intervals

def wilson_lower_bound(wins: int, n: int, z: float = 1.6449) -> float:
    """One-sided 95% Wilson lower bound on a win rate (z=1.6449). THE
    number every displayed win-rate must carry: a 7/8 pattern's 87%
    headline hides a ~59% lower bound. Returns 0.0 for n=0."""
    if n <= 0:
        return 0.0
    wins = max(0, min(wins, n))
    phat = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def binomial_p_two_sided(wins: int, n: int, p0: float) -> float:
    """Exact two-sided binomial p-value for observing `wins`/`n` against a
    null rate p0 (small-sample method: sum of outcome probabilities ≤ the
    observed outcome's probability). n here is hundreds at most — exact
    beats approximation."""
    if n <= 0:
        return 1.0
    p0 = min(1.0, max(0.0, p0))
    probs = [math.comb(n, k) * (p0 ** k) * ((1 - p0) ** (n - k))
             for k in range(n + 1)]
    observed = probs[max(0, min(wins, n))]
    return min(1.0, sum(p for p in probs if p <= observed + 1e-15))


def breakeven_win_rate(avg_win_r: float, avg_loss_r: float) -> float:
    """The STRUCTURAL null for a pattern's win-rate test: with defined-risk
    R profiles (known at entry from width/credit/profit-take/frictions —
    decision #27/#36 constants), a pattern paying +w on wins and -l on
    losses breaks even at l/(w+l). Never derive this from the pattern's
    own empirical R (the panel's circularity fix)."""
    w, l = abs(avg_win_r), abs(avg_loss_r)
    if w + l <= 0:
        return 0.5
    return l / (w + l)


# ------------------------------------------------------------- multiplicity

def benjamini_hochberg(p_values: list, q: float = FDR_Q) -> list:
    """BH step-up over one mining batch's FULL hypothesis list (the
    denominator must include the candidates that looked bad — you can't
    correct for hypotheses you never counted). Returns a same-order list
    of booleans: survives-at-q."""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    survives_rank = 0
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= q * rank / m:
            survives_rank = rank
    out = [False] * m
    for rank, idx in enumerate(order, start=1):
        if rank <= survives_rank:
            out[idx] = True
    return out


def block_permutation_null(outcomes: list, k: int, iters: int = 1000,
                           block: int = 10, seed: int = 7) -> list:
    """Null distribution for 'the best k-trade pattern found by luck':
    circularly rotate the outcome sequence in whole blocks (preserving
    regime autocorrelation, unlike a full shuffle), take k consecutive
    outcomes per draw, and record their win count. The real best pattern
    must beat the 95th percentile of this null or the batch is
    mining-noise. Deterministic per seed."""
    n = len(outcomes)
    if n == 0 or k <= 0 or k > n:
        return []
    rng = random.Random(seed)
    blocks = [outcomes[i:i + block] for i in range(0, n, block)]
    null = []
    for _ in range(iters):
        order = blocks[:]
        rng.shuffle(order)
        flat = [o for b in order for o in b]
        start = rng.randrange(n)
        window = [flat[(start + j) % n] for j in range(k)]
        null.append(sum(window))
    return null


def percentile(values: list, pct: float) -> float:
    """Nearest-rank percentile (deterministic, no numpy)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[rank - 1])


# ------------------------------------------------------------- stability

def split_window_stable(values: list) -> bool:
    """Decision #55's split-window check as the shared primitive: the
    metric (per-trade R, P&L, ...) must be non-negative in BOTH halves —
    a pattern that only pays in one regime-half is a slice artifact."""
    if len(values) < 2:
        return False
    mid = len(values) // 2
    return sum(values[:mid]) >= 0 and sum(values[mid:]) >= 0


def concentration_veto(values: list, window: int = 10,
                       max_share: float = 0.5) -> bool:
    """True (VETO) when more than `max_share` of total positive value sits
    inside one `window`-length run — one volatility event wearing a
    pattern costume. Only meaningful for positive-total series."""
    total = sum(values)
    if total <= 0 or len(values) <= window:
        return False
    best = max(sum(values[i:i + window])
               for i in range(len(values) - window + 1))
    return best / total > max_share


# ------------------------------------------------------------- promotion

def promotable(real_wins: int, real_n: int, sim_wins: int, sim_n: int,
               null_rate: float,
               min_resolutions: int = MIN_PROMOTION_RESOLUTIONS) -> dict:
    """The composed promotion verdict for one hypothesis, honest strata
    shown: combined evidence must clear the floor, the Wilson LOWER bound
    of the combined win-rate must beat the structural null rate, AND at
    least one REAL resolution must exist (simulated evidence supports,
    never solely justifies — locked policy). Returns
    {promote, reason, real, sim, wilson_lb}."""
    n = real_n + sim_n
    wins = real_wins + sim_wins
    lb = wilson_lower_bound(wins, n)
    out = {"promote": False, "real": {"wins": real_wins, "n": real_n},
           "sim": {"wins": sim_wins, "n": sim_n},
           "wilson_lb": round(lb, 4), "null_rate": round(null_rate, 4)}
    if n < min_resolutions:
        out["reason"] = f"insufficient n ({n}/{min_resolutions})"
        return out
    if real_n == 0:
        out["reason"] = "no real evidence (sim-only can never promote)"
        return out
    if lb <= null_rate:
        out["reason"] = (f"Wilson LB {lb:.2f} does not beat the structural "
                         f"null {null_rate:.2f}")
        return out
    out.update(promote=True, reason="clears floor + real evidence + LB")
    return out

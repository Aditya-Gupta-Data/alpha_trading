"""
src/knowledge_graph/brain_evolution.py — Self-Evolving Brain Map Primitives
===========================================================================

Implements the two remaining pillars of `docs/self_evolving_brain_map.md`
(Phase 2 of `docs/ARCHITECTURE_ROADMAP.md` §3):

  1. **DTW "You Are Here" Trajectory Lifecycle Coordinate (§1):**
     Given a live trailing multi-channel window `live_rows` and a dictionary of
     reference episode/archetype trajectories (`templates`), slides `live_rows`
     across each template via subsequence-DTW (`macro_fingerprints.dtw_distance`)
     to locate the current position within the historical cycle:
       - `lifecycle_pct` in `[0.0, 1.0]` (`matched_end_idx / (template_len - 1)`),
       - `phase` (`"inception"` [0–0.20), `"expansion"` [0.20–0.55),
         `"peak_stress"` [0.55–0.80), `"resolution"` [0.80–1.00]),
       - `similarity = 1 / (1 + dtw)`,
       - `remaining_sessions = template_len - 1 - matched_end_idx`,
       - `declared = True` iff `similarity >= sim_floor` and `coverage >= cov_floor`.

  2. **Beta-Bernoulli Time-Decayed Bayesian Pattern Confidence & Brier Ship-Gate (§2, §4.3):**
     Replaces static win-rate point estimates with a conjugate Beta(alpha_0, beta_0)
     posterior updated by exponentially time-decayed real outcomes
     (`weight_i = 0.5 ** (age_days / half_life_days)`):
       - `alpha = alpha_0 + sum_{wins} weight_i`
       - `beta  = beta_0  + sum_{losses} weight_i`
       - `posterior_mean = alpha / (alpha + beta)`
     Enforces the `stat_gates.is_learnable_ref` provenance firewall (excluding
     `sim:`, `shadow:`, `trial:`, `placebo:`, and `hypothetical` rows by default)
     and a walk-forward **Brier Score Calibration Ship-Gate** (`calibration_ship_gate`)
     that refuses to ship (`ship_allowed = False`) below `min_oos_samples = 30`
     or whenever the model's out-of-sample Brier score fails to beat the
     climatological base-rate baseline.

Zero execution authority: all outputs are advisory/context only.
"""

from __future__ import annotations

import math
from datetime import date

from src.analysis import macro_fingerprints as FP
from src.validation import stat_gates

DEFAULT_SIM_FLOOR = 0.70
DEFAULT_COV_FLOOR = 0.60
DEFAULT_PRIOR_ALPHA = 2.0
DEFAULT_PRIOR_BETA = 2.0
DEFAULT_HALF_LIFE_DAYS = 90.0
MIN_OOS_BRIER_SAMPLES = 30


def lifecycle_phase_for(pct: float) -> str:
    """Map normalized lifecycle coordinate `pct` in `[0, 1]` to a named phase."""
    if pct < 0.20:
        return "inception"
    if pct < 0.55:
        return "expansion"
    if pct < 0.80:
        return "peak_stress"
    return "resolution"


def locate_in_lifecycle(
    live_rows: list[dict],
    templates: dict[str, list[dict]],
    stride: int = 1,
    band: int = FP.DTW_BAND,
    sim_floor: float = DEFAULT_SIM_FLOOR,
    cov_floor: float = DEFAULT_COV_FLOOR,
) -> dict:
    """Slide `live_rows` (length `L`) across every reference trajectory in
    `templates` (`{template_name: [channel_row, ...]}`) using banded multivariate
    DTW (`macro_fingerprints.dtw_distance`) to answer "You Are Here".

    Returns:
      {
        "declared": bool,
        "reason": str | None,
        "best_template": str | None,
        "matched_start_idx": int | None,
        "matched_end_idx": int | None,
        "template_length": int | None,
        "lifecycle_pct": float | None,
        "phase": str | None,
        "remaining_sessions": int | None,
        "dtw": float | None,
        "similarity": float | None,
        "coverage": float | None,
        "runner_up": dict | None,
      }
    """
    L = len(live_rows or [])
    if L == 0 or not templates:
        return {
            "declared": False,
            "reason": "empty_live_window_or_templates",
            "best_template": None,
            "matched_start_idx": None,
            "matched_end_idx": None,
            "template_length": None,
            "lifecycle_pct": None,
            "phase": None,
            "remaining_sessions": None,
            "dtw": None,
            "similarity": None,
            "coverage": None,
            "runner_up": None,
        }

    candidates: list[dict] = []
    step = max(1, int(stride))
    for name in sorted(templates):
        ref_rows = templates[name] or []
        T = len(ref_rows)
        if T < L:
            continue
        best_for_template = None
        for start in range(0, T - L + 1, step):
            end = start + L - 1
            sub = ref_rows[start:start + L]
            d, cov = FP.dtw_distance(live_rows, sub, band=band)
            if d is None or cov < cov_floor:
                continue
            sim = 1.0 / (1.0 + d)
            pct = (end / (T - 1)) if T > 1 else 1.0
            entry = {
                "template": name,
                "matched_start_idx": start,
                "matched_end_idx": end,
                "template_length": T,
                "lifecycle_pct": round(pct, 4),
                "phase": lifecycle_phase_for(pct),
                "remaining_sessions": max(0, T - 1 - end),
                "dtw": round(d, 4),
                "similarity": round(sim, 4),
                "coverage": round(cov, 4),
            }
            if best_for_template is None or entry["similarity"] > best_for_template["similarity"]:
                best_for_template = entry
        if best_for_template is not None:
            candidates.append(best_for_template)

    if not candidates:
        return {
            "declared": False,
            "reason": "no_comparable_template_window",
            "best_template": None,
            "matched_start_idx": None,
            "matched_end_idx": None,
            "template_length": None,
            "lifecycle_pct": None,
            "phase": None,
            "remaining_sessions": None,
            "dtw": None,
            "similarity": None,
            "coverage": None,
            "runner_up": None,
        }

    candidates.sort(key=lambda c: (-c["similarity"], c["template"]))
    best = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    declared = best["similarity"] >= sim_floor and best["coverage"] >= cov_floor
    reason = None if declared else (
        f"similarity {best['similarity']:.3f} < floor {sim_floor:.2f}"
    )
    return {
        "declared": declared,
        "reason": reason,
        "best_template": best["template"] if declared else None,
        "matched_start_idx": best["matched_start_idx"],
        "matched_end_idx": best["matched_end_idx"],
        "template_length": best["template_length"],
        "lifecycle_pct": best["lifecycle_pct"],
        "phase": best["phase"],
        "remaining_sessions": best["remaining_sessions"],
        "dtw": best["dtw"],
        "similarity": best["similarity"],
        "coverage": best["coverage"],
        "runner_up": runner_up,
    }


def _parse_iso_day(val) -> date | None:
    if isinstance(val, date):
        return val
    if not val:
        return None
    try:
        return date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def compute_bayesian_confidence(
    outcomes: list[dict],
    as_of: date | str | None = None,
    prior_alpha: float = DEFAULT_PRIOR_ALPHA,
    prior_beta: float = DEFAULT_PRIOR_BETA,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    include_simulated: bool = False,
) -> dict:
    """Compute time-decayed Beta-Bernoulli posterior confidence over `outcomes`
    (`[{"date": "YYYY-MM-DD", "result": "win"|"loss", "journal_ref": ..., "hypothetical": bool}]`).

    Enforces point-in-time honesty (`outcome.date <= as_of`) and the
    `stat_gates.is_learnable_ref` provenance firewall unless `include_simulated=True`."""
    as_of_day = _parse_iso_day(as_of)
    alpha = float(prior_alpha)
    beta = float(prior_beta)
    raw_wins = 0
    raw_losses = 0
    decay_rate = math.log(2.0) / max(1.0, float(half_life_days))

    for row in outcomes or []:
        if not isinstance(row, dict):
            continue
        if not include_simulated:
            if row.get("hypothetical"):
                continue
            ref = row.get("journal_ref")
            if ref is not None and not stat_gates.is_learnable_ref(str(ref)):
                continue
        res = str(row.get("result") or "").lower()
        if res not in ("win", "loss"):
            continue
        r_day = _parse_iso_day(row.get("date"))
        if as_of_day is not None and r_day is not None and r_day > as_of_day:
            # Future-blind: ignore outcomes dated after `as_of`
            continue
        age_days = (
            max(0, (as_of_day - r_day).days)
            if (as_of_day is not None and r_day is not None)
            else 0
        )
        w = math.exp(-decay_rate * age_days)
        if res == "win":
            alpha += w
            raw_wins += 1
        else:
            beta += w
            raw_losses += 1

    total_ab = alpha + beta
    posterior_mean = alpha / total_ab if total_ab > 0 else 0.5
    posterior_var = (
        (alpha * beta) / ((total_ab ** 2) * (total_ab + 1.0))
        if total_ab > 0 else 0.0
    )
    eff_wins = alpha - prior_alpha
    eff_losses = beta - prior_beta
    eff_n = eff_wins + eff_losses
    raw_n = raw_wins + raw_losses
    wilson_lb = (
        stat_gates.wilson_lower_bound(raw_wins, raw_n) if raw_n > 0 else 0.0
    )
    return {
        "alpha": round(alpha, 4),
        "beta": round(beta, 4),
        "posterior_mean": round(posterior_mean, 4),
        "posterior_std": round(math.sqrt(max(0.0, posterior_var)), 4),
        "effective_n": round(eff_n, 4),
        "raw_n": raw_n,
        "raw_wins": raw_wins,
        "raw_losses": raw_losses,
        "wilson_lb": round(wilson_lb, 4),
    }


def brier_score(predictions: list[tuple[float, int]]) -> float | None:
    """Mean squared probability error `1/N * sum((p_i - y_i)^2)` for
    `predictions = [(p_i, y_i), ...]` with `p_i in [0, 1]` and `y_i in {0, 1}`."""
    if not predictions:
        return None
    return math.fsum((float(p) - int(y)) ** 2 for p, y in predictions) / len(predictions)


def calibration_ship_gate(
    predictions: list[tuple[float, int]],
    min_samples: int = MIN_OOS_BRIER_SAMPLES,
) -> dict:
    """Walk-forward Brier-score ship gate (`docs/self_evolving_brain_map.md` §2 & §4.3):
    compares out-of-sample probability predictions `[(p_i, y_i), ...]` against:
      1. The climatological base-rate baseline `BS_clim = mean((y_bar - y_i)^2)`,
      2. The 50/50 coin-flip baseline `BS_coin = 0.25`.
    Refuses to ship (`ship_allowed = False`) if `len(predictions) < min_samples`
    or if `BS_model >= BS_clim` or `BS_model >= 0.25`."""
    n = len(predictions or [])
    if n < min_samples:
        return {
            "ship_allowed": False,
            "status": "insufficient_oos_samples",
            "n_oos": n,
            "min_samples": min_samples,
            "brier_model": round(brier_score(predictions), 6) if n > 0 else None,
            "brier_climatology": None,
            "brier_skill_score": None,
            "reason": f"{n}/{min_samples} out-of-sample predictions — refusing to ship uncalibrated probabilities",
        }

    bs_model = brier_score(predictions)
    mean_y = math.fsum(int(y) for _, y in predictions) / n
    bs_clim = math.fsum((mean_y - int(y)) ** 2 for _, y in predictions) / n
    bs_coin = 0.25
    bss = (1.0 - (bs_model / bs_clim)) if bs_clim > 1e-12 else 0.0
    ship_ok = (bs_model < bs_clim) and (bs_model < bs_coin)
    return {
        "ship_allowed": ship_ok,
        "status": "calibrated" if ship_ok else "calibration_rejected",
        "n_oos": n,
        "min_samples": min_samples,
        "brier_model": round(bs_model, 6),
        "brier_climatology": round(bs_clim, 6),
        "brier_coin": bs_coin,
        "brier_skill_score": round(bss, 4),
        "reason": (
            f"Calibrated: BS={bs_model:.4f} beats climatology ({bs_clim:.4f}, BSS={bss:+.3f})"
            if ship_ok else
            f"Rejected: BS={bs_model:.4f} does not beat climatology ({bs_clim:.4f}) / coin (0.2500)"
        ),
    }

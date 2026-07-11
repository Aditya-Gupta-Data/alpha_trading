"""
Tests for the anti-hallucination toolkit (Phase 4, P4-1) — including the
noise-injection property: fed pure noise, the composed gates must promote
(almost) nothing. Fully offline, deterministic seeds.

Run either of these from the project folder:
    python tests/test_stat_gates.py
    python -m pytest tests/test_stat_gates.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.validation import stat_gates as sg


def test_wilson_lower_bound_punctures_small_sample_headlines():
    lb = sg.wilson_lower_bound(7, 8)              # the 87% headline
    assert 0.55 < lb < 0.70                       # honest read: ~0.6
    assert sg.wilson_lower_bound(0, 0) == 0.0
    assert sg.wilson_lower_bound(50, 100) < 0.5   # LB always below the point
    big = sg.wilson_lower_bound(600, 1000)
    assert 0.55 < big < 0.60                      # tightens with n


def test_exact_binomial_and_structural_breakeven():
    # 9/10 against a fair coin is rare; against p0=0.8 it is not.
    assert sg.binomial_p_two_sided(9, 10, 0.5) < 0.05
    assert sg.binomial_p_two_sided(9, 10, 0.8) > 0.2
    assert sg.binomial_p_two_sided(0, 0, 0.5) == 1.0
    # A 1.5R-win / 1R-loss profile breaks even at 40%.
    assert abs(sg.breakeven_win_rate(1.5, 1.0) - 0.4) < 1e-9
    assert sg.breakeven_win_rate(0, 0) == 0.5


def test_benjamini_hochberg_step_up():
    pvals = [0.001, 0.008, 0.039, 0.041, 0.30, 0.90]
    out = sg.benjamini_hochberg(pvals, q=0.10)
    assert out[:4] == [True, True, True, True]     # 0.041 <= 0.10*4/6
    assert out[4:] == [False, False]
    assert sg.benjamini_hochberg([]) == []
    # Order-preservation: survivors map back to their input positions.
    assert sg.benjamini_hochberg([0.9, 0.001], q=0.10) == [False, True]


def test_block_permutation_null_calibrates_luck():
    rng = random.Random(1)
    outcomes = [1 if rng.random() < 0.6 else 0 for _ in range(200)]
    null = sg.block_permutation_null(outcomes, k=20, iters=400, seed=3)
    assert len(null) == 400
    p95 = sg.percentile(null, 95)
    # A luck ceiling near-but-above the base expectation of 12/20.
    assert 12 <= p95 <= 20
    # Deterministic per seed.
    assert null == sg.block_permutation_null(outcomes, k=20, iters=400, seed=3)
    assert sg.block_permutation_null([], k=5) == []


def test_stability_and_concentration_primitives():
    assert sg.split_window_stable([1, 2, -1, 3]) is True
    assert sg.split_window_stable([5, 5, -1, -2]) is False   # dies in half 2
    assert sg.split_window_stable([1]) is False
    flat = [0.1] * 40
    assert sg.concentration_veto(flat) is False
    spike = [0.0] * 30 + [5.0] * 5 + [0.0] * 5                # one hot window
    assert sg.concentration_veto(spike) is True


def test_promotable_composes_floor_reality_and_lb():
    # Sim-only mountains never promote.
    v = sg.promotable(0, 0, 80, 100, null_rate=0.4)
    assert v["promote"] is False and "sim-only" in v["reason"]
    # Below the floor (n=5 < the balanced 7): insufficient.
    v = sg.promotable(2, 3, 1, 2, null_rate=0.4)
    assert v["promote"] is False and "insufficient" in v["reason"]
    # Enough n, real evidence present, LB beats the structural null.
    v = sg.promotable(9, 11, 14, 18, null_rate=0.4)
    assert v["promote"] is True and v["real"]["n"] == 11
    # Same counts against a brutal null: refused.
    v = sg.promotable(9, 11, 14, 18, null_rate=0.75)
    assert v["promote"] is False and "does not beat" in v["reason"]


def test_ref_and_tag_exclusions_guard_self_poisoning():
    assert sg.is_learnable_ref("ab12cd34")
    for ref in ("sim:x", "shadow:y", "trial:z", "placebo:q"):
        assert not sg.is_learnable_ref(ref)
    assert sg.is_minable_tag("golden_cross")
    assert not sg.is_minable_tag("auto:7f3a2b1c")


def test_noise_injection_no_faces_in_clouds():
    """The property the whole harness is for: pure-noise batches of mined
    'patterns' must (almost) never promote. 25-seed smoke version of the
    panel's 500-seed nightly."""
    promoted = 0
    batches = 0
    for seed in range(25):
        rng = random.Random(seed)
        base = 0.40                     # structural breakeven of a 1.5R book
        pvals, candidates = [], []
        for _ in range(40):             # one mining batch: 40 noise patterns
            n = rng.randint(15, 40)
            wins = sum(1 for _ in range(n) if rng.random() < base)
            pvals.append(sg.binomial_p_two_sided(wins, n, base))
            candidates.append((wins, n))
        survivors = sg.benjamini_hochberg(pvals, q=sg.FDR_Q)
        for (wins, n), ok in zip(candidates, survivors):
            if not ok:
                continue
            batches += 1
            real_n = max(1, n // 3)
            real_wins = min(wins, real_n)
            v = sg.promotable(real_wins, real_n, wins - real_wins,
                              n - real_n, null_rate=base)
            if v["promote"]:
                promoted += 1
    # 25 seeds x 40 hypotheses = 1000 noise candidates. The composed gate
    # must hold the realized false-promotion count to a handful.
    assert promoted <= 3, f"{promoted} noise patterns promoted"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

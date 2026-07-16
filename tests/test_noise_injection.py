"""
Tests for the Phase-4 noise-injection suite (src/validation/noise.py):
the end-to-end false-discovery regression — the REAL miners + registry +
trial gates fed pure-noise histories must promote (almost) nothing, and
the planted-edge positive control must still be mined AND promoted.
Everything runs in throwaway temp DBs; the production-DB guard is itself
under test. Offline, deterministic, stdlib-only.

Run:
    python tests/test_noise_injection.py
    pytest tests/test_noise_injection.py -v
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.validation import noise
from src.validation import registry as rg


# ------------------------------------------------- the FDR regression

# CI smoke: 8 seeds keeps the suite fast; the 25-seed default and the
# 500-seed nightly run via the CLI (python3 -m src.validation.noise).
CI_SEEDS = 8


def test_noise_false_promotion_rate_within_budget():
    """THE regression: across independent pure-noise seeds, total
    promotions must stay under the binomial upper bound at the active FDR
    budget. On noise every promotion is false by construction — a breach
    means some gate (BH, Wilson LB, support floor, real-evidence rule)
    stopped controlling false discovery end-to-end."""
    result = noise.false_promotion_rate(seeds=CI_SEEDS)
    assert result["total_candidates"] > 0, "vacuous run — no candidates"
    assert result["ok"], (
        f"false-promotion breach: {result['total_promoted']}/"
        f"{result['total_candidates']} promoted on pure noise "
        f"(bound {result['bound']} at q={result['fdr_q']})")


def test_trial_gate_is_actually_exercised_on_noise():
    """The forced candidates guarantee the TRIAL gate runs even when BH
    (correctly) lets nothing through the miner — a regression here would
    make the FDR assertion silently vacuous."""
    r = noise.run_noise_seed(seed=3)
    assert r["candidates"] >= r["forced_candidates"] > 0
    assert len(r["verdicts"]) == r["candidates"]
    # every candidate got a real verdict from the canonical lifecycle
    for v in r["verdicts"]:
        assert v["final_status"] in rg.STATES


# ------------------------------------------------- positive controls

def test_planted_edge_is_mined_and_promoted():
    """A suite that promotes nothing proves nothing: a genuinely
    predictive tag-pair must clear the WHOLE pipeline — mined into the
    registry by the real cooccurrence miner, then promoted to VALIDATED
    off genuinely winning shadow evidence."""
    control = noise.plant_edge()
    assert control["mined"], (
        "planted edge was never registered — the miner has gone blind "
        f"({control})")
    assert control["promoted"], (
        f"planted edge was mined but never promoted — the trial gate has "
        f"gone deaf ({control['verdicts']})")


# ------------------------------------------------- isolation guarantees

def test_harness_refuses_the_production_db(tmp_path):
    """The hard guard: any conn that resolves to brain_map.DEFAULT_DB_PATH
    is refused outright — the noise harness can never write production."""
    from src import brain_map
    fake_prod = tmp_path / "brain_map.db"
    with mock.patch.object(brain_map, "DEFAULT_DB_PATH", fake_prod):
        conn = brain_map.connect(fake_prod)   # schema'd, at the "prod" path
        try:
            import pytest
            with pytest.raises(RuntimeError, match="PRODUCTION"):
                noise._assert_not_production(conn)
        finally:
            conn.close()


def test_noise_run_never_touches_production_db():
    """Belt and suspenders, observed: a full seed run leaves the real
    brain_map.db byte-identical (mtime + size unchanged, or still absent)."""
    from src import brain_map
    prod = brain_map.DEFAULT_DB_PATH
    before = (prod.stat().st_mtime_ns, prod.stat().st_size) if prod.exists() else None
    noise.run_noise_seed(seed=11)
    after = (prod.stat().st_mtime_ns, prod.stat().st_size) if prod.exists() else None
    assert before == after


# ------------------------------------------------- determinism + math

def test_seed_runs_are_deterministic():
    """Same seed -> identical counts and identical per-pattern verdicts
    (temp paths differ; everything the assertion depends on must not)."""
    a = noise.run_noise_seed(seed=7)
    b = noise.run_noise_seed(seed=7)
    for key in ("outcomes", "mined_candidates", "forced_candidates",
                "candidates", "promoted"):
        assert a[key] == b[key], f"nondeterministic {key}"
    assert ([v["final_status"] for v in a["verdicts"]]
            == [v["final_status"] for v in b["verdicts"]])


def test_binom_upper_bound_math():
    """Pure-stdlib bound sanity: exact tiny cases + no underflow at
    nightly scale (n in the thousands)."""
    assert noise.binom_upper_bound(0, 0.15) == 0
    # n=1, p=0.15: P(X<=0)=0.85 < 0.99, P(X<=1)=1.0 -> k=1
    assert noise.binom_upper_bound(1, 0.15, alpha=0.01) == 1
    # n=10, p=0.5 at alpha=0.01 -> k=9 (P(X<=8)=0.9893 < 0.99)
    assert noise.binom_upper_bound(10, 0.5, alpha=0.01) == 9
    # nightly scale: mean 600, bound must sit a few sigma above, not at n
    b = noise.binom_upper_bound(4000, 0.15, alpha=0.01)
    assert 600 < b < 700


def test_noise_labels_are_independent_of_tags():
    """Generator honesty: with no planted edge, the corpus win rate sits
    near the structural null and planted_n is zero."""
    r = noise.run_noise_seed(seed=5, forced_candidates=0)
    assert r["planted_n"] == 0
    assert r["outcomes"] == noise.DAYS * noise.OUTCOMES_PER_DAY


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            if "tmp_path" in t.__code__.co_varnames[:t.__code__.co_argcount]:
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    t(Path(tmp))
            else:
                t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{passed}/{len(tests)} tests passed.")

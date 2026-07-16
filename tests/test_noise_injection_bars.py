"""
Tests for the v2 noise suite (src/validation/noise.py, --mode bars): the
PRICE-path false-discovery regression — block-permuted bars through the
REAL simulator, mined candidates judged on organic window-B evidence
against their FAMILY's own base rate (never 50%/breakeven: the sim corpus
wins far above coin-flip doing nothing clever, decision #65). Offline,
deterministic, throwaway temp DBs only.

Run:
    python tests/test_noise_injection_bars.py
    pytest tests/test_noise_injection_bars.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.validation import noise
from src.validation import registry as rg
from src.validation import trial


CI_SEEDS = 3          # the price path is simulator-heavy; nightly runs 50+


# --------------------------------------------------- the noise-bar null

def test_block_permutation_preserves_marginals_and_destroys_order():
    base = noise.generate_base_bars()
    perm = noise.block_permute_bars(base, seed=1)
    assert len(perm) == len(base)
    assert [b[0] for b in perm] == [b[0] for b in base]      # same date axis
    # Same multiset of daily returns (the marginals ARE preserved)...
    def rets(bars):
        return sorted(round(bars[i][3] / bars[i - 1][3], 9)
                      for i in range(1, len(bars)))
    assert rets(perm) == rets(base)
    # ...but not the same arrangement (the order IS destroyed).
    assert [b[3] for b in perm] != [b[3] for b in base]
    # Bars stay internally consistent: low <= close <= high.
    for _, lo, hi, c in perm:
        assert lo <= c <= hi
    # Different seeds -> different arrangements (per-seed null).
    assert ([b[3] for b in noise.block_permute_bars(base, seed=2)]
            != [b[3] for b in perm])


def test_simulator_actually_trades_on_permuted_bars():
    """Vacuousness guard: the real simulator must propose AND resolve
    organic trades on the permuted bars in BOTH windows — a degenerate
    zero-trade run would make the FDR assertion meaningless."""
    r = noise.run_bars_seed(seed=0)
    assert r["resolved_a"] > 0 and r["resolved_b"] > 0
    assert r["candidates"] > 0
    # At least one candidate reached a real verdict on matched organic
    # evidence (the trial gate is exercised, not skipped).
    assert any(v.get("matched_n") for v in r["verdicts"])


# --------------------------------------------------- the FDR regression

def test_bars_false_promotion_rate_within_budget():
    """THE v2 regression: across seeds, promotions on block-permuted bars
    must stay under the binomial bound at the active FDR budget — and the
    run must be non-vacuous (organic resolutions > 0)."""
    result = noise.false_promotion_rate_bars(seeds=CI_SEEDS)
    assert result["total_resolved_trades"] > 0
    assert result["total_candidates"] > 0
    assert result["ok"], (
        f"price-path false-promotion breach: {result['total_promoted']}/"
        f"{result['total_candidates']} promoted on permuted bars "
        f"(bound {result['bound']} at q={result['fdr_q']})")


def test_family_base_rate_absorbs_simulator_generosity():
    """The v2-specific silent failure, pinned: evidence indistinguishable
    from the family's own (generous) base rate must NOT promote — even
    when its Wilson LB comfortably beats the naive breakeven null that
    would have (wrongly) promoted it."""
    from src.validation import stat_gates as sg
    conn = brain_map.connect(":memory:")
    pid = rg.register(conn, "cooccurrence",
                      {"kind": "cooccurrence", "tags": ["x", "y"]})["pattern_id"]
    w = {"validation_start": "2026-03-01"}
    # 9/12 organic-style wins (rate .75, Wilson LB ~.55) vs family at .75.
    for i in range(12):
        day = f"2026-03-{i + 2:02d}"
        ref = trial.record_shadow_fire(conn, pid, day, f"T#{i}")["ref"]
        res = "win" if i < 9 else "loss"
        trial.resolve_shadow(conn, ref, res, 1.0 if res == "win" else -1.0, day)
    lb = sg.wilson_lower_bound(9, 12)
    naive_null = sg.breakeven_win_rate(1.5, 1.0)
    assert lb > naive_null            # the naive null WOULD have promoted
    verdict = trial.evaluate_trial(conn, pid, w, base_rate=0.75)
    assert verdict["promote"] is False
    assert verdict["final_status"] != "VALIDATED"


def test_genuine_family_superiority_still_promotes():
    """Positive control for the v2 scoring seam: matched evidence that
    GENUINELY beats its family's base rate must promote — the family bar
    raises the null, it doesn't weld the gate shut."""
    conn = brain_map.connect(":memory:")
    pid = rg.register(conn, "cooccurrence",
                      {"kind": "cooccurrence", "tags": ["p", "q"]})["pattern_id"]
    w = {"validation_start": "2026-03-01"}
    for i in range(12):                                   # 12/12 wins
        day = f"2026-03-{i + 2:02d}"
        ref = trial.record_shadow_fire(conn, pid, day, f"T#{i}")["ref"]
        trial.resolve_shadow(conn, ref, "win", 1.0, day)
    verdict = trial.evaluate_trial(conn, pid, w, base_rate=0.62)
    assert verdict["promote"] is True
    assert verdict["final_status"] == "VALIDATED"


# --------------------------------------------------- isolation + determinism

def test_bars_run_never_touches_production_db():
    from src import brain_map as bm
    prod = bm.DEFAULT_DB_PATH
    before = (prod.stat().st_mtime_ns, prod.stat().st_size) if prod.exists() else None
    noise.run_bars_seed(seed=2)
    after = (prod.stat().st_mtime_ns, prod.stat().st_size) if prod.exists() else None
    assert before == after


def test_bars_seed_is_deterministic():
    a = noise.run_bars_seed(seed=1)
    b = noise.run_bars_seed(seed=1)
    for key in ("resolved_a", "resolved_b", "mined_candidates",
                "forced_candidates", "candidates", "promoted"):
        assert a[key] == b[key], f"nondeterministic {key}"
    assert ([v["final_status"] for v in a["verdicts"]]
            == [v["final_status"] for v in b["verdicts"]])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{passed}/{len(tests)} tests passed.")

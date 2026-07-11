"""
src/validation/timelock.py — the as-of contract, mechanically checkable
=======================================================================

One leaked feature silently voids every walk-forward result downstream —
and this repo has been burned by exactly this failure twice, in its own
words (decision #44 named graph-feature backfill "look-ahead leakage";
decision #50 mandated as-of recomputation in response). The contract:

    A discovery-facing computation given `as_of = T` must be FUTURE-BLIND:
    perturbing, adding, or removing any input data dated after T must not
    change its output.

`assert_future_blind` runs that check directly: call the function on base
inputs, then on inputs salted with future-dated rows, and compare outputs
semantically (parsed structures, not formatting). Tests use it per
surface; new discovery features don't merge without one (the
tests/test_no_lookahead.py suite is the enforcement, in the decision-#30
import-guard style).
"""

import json


def semantic_equal(a, b) -> bool:
    """Order-insensitive-where-honest structural equality: dicts compare by
    key, lists elementwise, floats with tolerance — formatting noise (key
    order, float repr) never fails a timelock check; real value drift
    always does."""
    if isinstance(a, dict) and isinstance(b, dict):
        return (a.keys() == b.keys()
                and all(semantic_equal(a[k], b[k]) for k in a))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return (len(a) == len(b)
                and all(semantic_equal(x, y) for x, y in zip(a, b)))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return False
    return a == b


def future_salt(rows: list, as_of: str, make_row, days_ahead=(1, 3, 30)) -> list:
    """Base rows + synthetic rows dated strictly AFTER as_of (built by
    `make_row(iso_date)`) — the perturbation half of the check."""
    from datetime import date, timedelta
    base_day = date.fromisoformat(as_of)
    salted = list(rows)
    for d in days_ahead:
        salted.append(make_row((base_day + timedelta(days=d)).isoformat()))
    return salted


def assert_future_blind(compute, base_inputs: list, salted_inputs: list,
                        label: str = "computation"):
    """Run `compute(rows)` on both input sets; raise AssertionError with a
    diff-ish message when the future changed the output. `compute` must be
    a pure closure over everything except the rows."""
    base_out = compute(base_inputs)
    salted_out = compute(salted_inputs)
    if not semantic_equal(base_out, salted_out):
        raise AssertionError(
            f"TIMELOCK VIOLATION in {label}: output changed when "
            f"future-dated rows were added.\n  base:   "
            f"{json.dumps(base_out, default=str)[:400]}\n  salted: "
            f"{json.dumps(salted_out, default=str)[:400]}")

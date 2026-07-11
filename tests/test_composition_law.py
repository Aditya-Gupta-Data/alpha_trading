"""
Structural guards for the composition law (decision #63): context layers
annotate — they never score, never gate. Enforced source-level, the
decision-#30 import-guard style: if a future change wires affinity/flows/
macro into forecast's scored drivers, or branches proposal flow on the
alignment line, these tests fail before it merges.

Run either of these from the project folder:
    python tests/test_composition_law.py
    python -m pytest tests/test_composition_law.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def test_forecast_scored_drivers_stay_frozen_to_the_sanctioned_set():
    """forecast.py's checklist may only score trend/cross/RSI/news (the
    pre-law vocabulary) plus the Way-A cycle driver (owner decision
    2026-07-11, H3): its points come ONLY from tuner-learned outcome
    evidence (floor-gated, capped, ZERO until earned) — the same learning
    channel as the archetype weights, not a hand-stacked context layer.
    Anything else enters as verdicts via the harness, or not at all."""
    src = (ROOT / "src" / "forecast.py").read_text()
    drivers = set(re.findall(r"def (_[a-z_]+_driver)\(", src))
    assert drivers == {"_trend_driver", "_cross_driver", "_rsi_driver",
                       "_news_driver", "_cycle_driver"}, (
        f"forecast.py grew a scored driver outside decision #63's frozen "
        f"set: {sorted(drivers)} — route it through the harness instead")
    # And the advisory layers are not imported into the scored path.
    for banned in ("entity_affinity", "flows_tracker", "macro_tracker",
                   "confluence"):
        assert banned not in src, (
            f"forecast.py imports {banned} — context layers may not enter "
            "the scored checklist (decision #63)")


def test_alignment_line_annotates_and_never_branches_flow():
    """options_proposer may assign and render alignment_line — nothing
    else. An `if ... alignment` controlling proposal flow is the law's
    canonical violation."""
    src = (ROOT / "src" / "options_proposer.py").read_text()
    uses = [line.strip() for line in src.splitlines()
            if "alignment_line" in line and not line.strip().startswith("#")]
    for line in uses:
        assert (line.startswith("from src.confluence.evidence import")
                or line.startswith("p[\"alignment_line\"]")
                or line.startswith("alignment = p.get(")
                ), f"alignment_line used outside annotate/render: {line!r}"
    # It must never appear in a conditional that gates behavior.
    assert not re.search(r"if[^\n]*alignment_line", src), (
        "alignment_line is being branched on — it is ANNOTATE-only "
        "(decision #63)")


def test_advisory_flagged_status_is_reserved_not_yet_emitted():
    """No layer holds VETO power yet: nothing in src/ may journal an
    advisory_flagged decision until the harness grants it (human-approved
    card, #49 ritual)."""
    for py in (ROOT / "src").rglob("*.py"):
        text = py.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if "advisory_flagged" in stripped and not stripped.startswith("#"):
                raise AssertionError(
                    f"{py.name} emits advisory_flagged before any veto "
                    f"power was granted: {stripped!r}")


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

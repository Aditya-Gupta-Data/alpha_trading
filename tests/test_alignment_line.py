"""
Tests for the descriptive alignment line (Phase 3, P3-1): facts about
macro/news/smart-money vs the proposal's own direction on the alert card —
"evidence, not a gate". Fully offline.

Run either of these from the project folder:
    python tests/test_alignment_line.py
    python -m pytest tests/test_alignment_line.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.confluence import evidence as ev


def _snap(**layer_overrides):
    """A snapshot with chosen layers speaking and the rest abstaining."""
    snap = ev.build_evidence_snapshot("NIFTY 50", today=date(2026, 7, 11))
    by_layer = {e["layer"]: e for e in snap["layers"]}
    for layer, (direction, stance) in layer_overrides.items():
        by_layer[layer].update(direction=direction, stance=stance,
                               abstained=False, strength=abs(direction))
    return snap


def test_alignment_states_aligned_and_opposed_vs_view():
    snap = _snap(macro=(-0.4, "headwind"), news=(0.6, "positive"),
                 affinity=(-1.0, "distribution"))
    line = ev.alignment_line(snap, "bullish")
    assert "macro OPPOSED (headwind)" in line
    assert "news ALIGNED (positive)" in line
    assert "affinity OPPOSED (distribution)" in line
    assert "evidence, not a gate" in line
    # The same facts flip against a bearish proposal.
    line = ev.alignment_line(snap, "bearish")
    assert "macro ALIGNED (headwind)" in line
    assert "news OPPOSED (positive)" in line


def test_all_abstained_or_directionless_yields_none():
    assert ev.alignment_line(_snap(), "bullish") is None       # all abstained
    assert ev.alignment_line(_snap(macro=(-0.4, "headwind")),
                             "neutral") is None                 # no direction
    assert ev.alignment_line(None, "bullish") is None
    # vix/technical layers never enter the alignment line.
    snap = _snap()
    by = {e["layer"]: e for e in snap["layers"]}
    by["vix_regime"].update(direction=-1.0, abstained=False)
    by["technical"].update(direction=1.0, abstained=False)
    assert ev.alignment_line(snap, "bullish") is None


def test_imminent_results_ride_the_line():
    snap = _snap(macro=(0.3, "tailwind"))
    snap["days_to_results"] = 3
    line = ev.alignment_line(snap, "bullish")
    assert "results in 3d" in line
    snap["days_to_results"] = 40                      # far away — not noise
    assert "results" not in ev.alignment_line(snap, "bullish")


def test_alert_card_carries_the_line_only_when_present():
    from src.options_proposer import _format_proposal_alert
    p = {"spread": {"strategy": "iron_condor", "expiry": "2026-07-30",
                    "lot_size": 75, "net_credit": 40.0, "net_debit": None,
                    "max_loss": 5800.0, "max_profit": 4200.0,
                    "margin": {"total_margin": 42000.0},
                    "legs": [{"side": "SELL", "option_type": "CE",
                              "strike": 25200.0, "premium": 90.0}]},
         "vix": 14.5, "view": "neutral", "ticker": "NIFTY 50", "lots": 1,
         "alignment_line": "📐 Alignment vs this proposal — macro OPPOSED "
                           "(headwind)  *(evidence, not a gate)*"}
    text = _format_proposal_alert(p)
    assert "📐 Alignment" in text and "evidence, not a gate" in text
    p.pop("alignment_line")
    assert "📐" not in _format_proposal_alert(p)      # absent -> no block


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

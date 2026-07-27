"""
tests/test_ceo_language.py — Directive 2 (docs/ceo_view_discord_design.md):
the two honesty gates on the macro/strategy sentence, and the plain-English
book summary. Hermetic — every doc is passed in directly, no file I/O.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import ceo_language as cl


def _regime_doc(declared=True, reason="declared", episode="brexit_2016",
                analog_count=10, similarity=0.7346, archetype="A1",
                phase="P1_shock", strategies=None):
    return {
        "horizons": {
            "shock": {
                "declared": declared,
                "reason": reason,
                "phase": phase,
                "best": {"archetype": archetype, "best_episode": episode,
                         "similarity": similarity,
                         "analog_count": analog_count},
                "strategy_slice": {"strategies": strategies or []},
            }
        }
    }


def _strategy(strategy_id="sid1", name="long_energy_oil"):
    return {"strategy_id": strategy_id, "name": name}


def _scoreboard(archetype, phase, strategy_id, status):
    return {"table": {archetype: {phase: [
        {"strategy_id": strategy_id, "status": status}]}}}


def test_no_analog_named_when_not_declared():
    """Gate 1: not declared -> no episode name is ever spoken, only the
    honest reason."""
    doc = _regime_doc(declared=False, reason="analogs 2 < 3")
    line = cl.macro_regime_sentence(regime_doc=doc)
    assert "brexit" not in line.lower()
    assert "still accumulating" in line
    assert "too few comparable episodes" in line


def test_empty_and_cache_miss_reasons_are_readable():
    for reason, expect in [("empty_current_window", "not enough recent"),
                           ("cache_stale_aborted", "cache miss"),
                           ("no_comparable_match", "no historical episode")]:
        doc = _regime_doc(declared=False, reason=reason)
        assert expect in cl.macro_regime_sentence(regime_doc=doc)


def test_declared_names_the_episode_in_plain_english():
    doc = _regime_doc(declared=True, episode="volmageddon_2018")
    line = cl.macro_regime_sentence(regime_doc=doc, scoreboard_doc={})
    assert "Volmageddon 2018" in line
    assert "10 comparable episode" in line
    assert "73%" in line


def test_gate_2_unconfirmed_strategy_never_sounds_executed():
    """Gate 2: a strategy with no FORWARD_CONFIRMED status (including the
    real-world case today, ACCUMULATING / not tracked at all) is labeled
    in-sample-only and 'never executes' — NOT executing (CLAUDE.md Rule 5:
    the macro engine has zero execution authority)."""
    doc = _regime_doc(declared=True,
                      strategies=[_strategy("sid1", "long_energy_oil")])
    line = cl.macro_regime_sentence(
        regime_doc=doc, scoreboard_doc=_scoreboard("A1", "P1_shock",
                                                   "sid1", "ACCUMULATING"))
    assert "executing" not in line.lower()
    assert "favours long_energy_oil" in line
    assert "forward evidence still accumulating" in line
    assert "nothing acts on this automatically" in line


def test_gate_2_untracked_cell_reads_the_same_as_accumulating():
    """A strategy with no scoreboard cell at all (0 cells tracked, the
    real state as of 2026-07-27) must not silently upgrade to a stronger
    claim than an ACCUMULATING cell would get."""
    doc = _regime_doc(declared=True,
                      strategies=[_strategy("sid1", "long_energy_oil")])
    line = cl.macro_regime_sentence(regime_doc=doc, scoreboard_doc={"table": {}})
    assert "favours long_energy_oil" in line
    assert "executing" not in line.lower()


def test_gate_2_forward_confirmed_is_named_separately():
    doc = _regime_doc(declared=True,
                      strategies=[_strategy("sid1", "long_energy_oil")])
    line = cl.macro_regime_sentence(
        regime_doc=doc, scoreboard_doc=_scoreboard("A1", "P1_shock", "sid1",
                                                   "FORWARD_CONFIRMED"))
    assert "Forward-confirmed live: long_energy_oil" in line
    assert "executing" not in line.lower()


def test_missing_regime_file_is_honest_not_silent():
    assert cl.macro_regime_sentence(regime_doc=None,
                                    regime_path="/no/such/file.json") \
        == "🌍 Macro regime: not available yet."


def test_book_summary_sentence_covers_the_signs():
    assert cl.book_summary_sentence(0, 0.0, 0.0) == \
        "no open positions, flat today, book is flat directionally."
    up = cl.book_summary_sentence(3, 1500.0, 2.0)
    assert "3 open positions" in up and "up Rs.1,500" in up and "leaning long" in up
    down = cl.book_summary_sentence(1, -250.0, -1.0)
    assert "1 open position," in down and "down Rs.250" in down and "leaning short" in down

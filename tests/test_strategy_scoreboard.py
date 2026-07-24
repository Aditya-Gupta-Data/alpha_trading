"""
tests/test_strategy_scoreboard.py — SB-3/SB-4 rollup + graduation contract
==========================================================================

Covers the four graduation states, the roll-up into the registry-mirror table,
the honest summary, and the digest lines.
"""
import json
from pathlib import Path

from src.analysis import strategy_scoreboard as SB


# ------------------------------------------------------------- graduation (SB-4)

def test_status_accumulating_below_floor():
    assert SB._status(wins=3, n=3) == "ACCUMULATING"       # n < MIN_FWD_CALLS


def test_status_confirmed_when_lower_bound_clears_null():
    assert SB._status(wins=8, n=8) == "FORWARD_CONFIRMED"  # LB > 0.5


def test_status_contradicted_when_upper_bound_below_null():
    assert SB._status(wins=1, n=10) == "FORWARD_CONTRADICTED"  # UB < 0.5


def test_status_inconclusive_when_ci_straddles_null():
    assert SB._status(wins=5, n=10) == "INCONCLUSIVE"      # enough n, no edge


# ------------------------------------------------------------- rollup (SB-3)

def _call(dd, aid, phase, name, sid, win, verdict="PREFER"):
    return {"declaration_date": dd, "resolved_on": dd, "horizon": "shock",
            "archetype": aid, "phase": phase, "strategy_id": sid, "name": name,
            "in_sample_verdict": verdict, "in_sample_significant": True,
            "realized_return": 0.02 if win else -0.02, "win": win, "null": 0.5}


def _ledger(tmp_path, rows):
    p = tmp_path / "scores.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def test_build_scoreboard_groups_and_grades(tmp_path):
    rows = []
    # A1/P1 energy: 8 live wins -> CONFIRMED
    for i in range(8):
        rows.append(_call(f"2024-01-{i+1:02d}", "A1", "P1_shock",
                          "long_energy_oil", "sid_en", win=True))
    # A2/P3 pharma: 3 calls -> ACCUMULATING
    for i in range(3):
        rows.append(_call(f"2024-02-{i+1:02d}", "A2", "P3_resolution",
                          "long_pharma", "sid_ph", win=(i == 0), verdict="SHOW"))
    board = tmp_path / "board.json"
    doc = SB.build_scoreboard(scores_path=_ledger(tmp_path, rows),
                              out_path=board, clock=lambda: "2024-03-01")

    en = doc["table"]["A1"]["P1_shock"][0]
    assert en["name"] == "long_energy_oil"
    assert en["forward"]["n"] == 8 and en["forward"]["wins"] == 8
    assert en["status"] == "FORWARD_CONFIRMED"
    assert en["in_sample"]["verdict"] == "PREFER"

    ph = doc["table"]["A2"]["P3_resolution"][0]
    assert ph["status"] == "ACCUMULATING"

    s = doc["summary"]
    assert s["cells_tracked"] == 2 and s["confirmed_count"] == 1
    assert s["by_status"]["FORWARD_CONFIRMED"] == 1
    assert board.exists()                                 # atomic write landed


def test_digest_lines_honest_when_empty(tmp_path):
    missing = tmp_path / "nope.json"
    assert SB.digest_lines(scoreboard_path=missing) == ["Forward clock: no scoreboard yet."]


def test_digest_lines_report_confirmed(tmp_path):
    rows = [_call(f"2024-01-{i+1:02d}", "A1", "P1_shock", "long_energy_oil",
                  "sid_en", win=True) for i in range(8)]
    board = tmp_path / "board.json"
    SB.build_scoreboard(scores_path=_ledger(tmp_path, rows), out_path=board,
                        clock=lambda: "2024-03-01")
    lines = SB.digest_lines(scoreboard_path=board)
    assert any("CONFIRMED live" in ln and "long_energy_oil" in ln for ln in lines)

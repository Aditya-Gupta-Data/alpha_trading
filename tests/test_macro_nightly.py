"""
The VM macro heartbeat, fully offline: all three stages run through
injected fns, each stage fails open independently (a dead stage never
aborts the others or raises), and one heartbeat line is written.
"""
import json
from datetime import date

from src.analysis import macro_nightly as MN


def test_run_drives_all_three_stages_and_writes_heartbeat(tmp_path):
    hb = tmp_path / "hb.log"
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT", "DXY"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=lambda: {"declared": True, "horizons": {
            "shock": {"declared": True, "phase": "P3_resolution",
                      "best": {"archetype": "A2"}}}},
        clock=lambda: date(2026, 7, 23), heartbeat_path=hb)
    assert out["as_of"] == "2026-07-23"
    assert out["stages"]["fred"]["ok"] == ["BRENT", "DXY"]
    assert out["stages"]["indices"]["rows_added"] == 1
    assert out["stages"]["declare"]["declared"] is True
    assert out["stages"]["declare"]["horizons"]["shock"]["archetype"] == "A2"
    # exactly one heartbeat line, valid JSON
    lines = hb.read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["as_of"] == "2026-07-23"


def test_a_dead_stage_never_aborts_the_others(tmp_path):
    def boom():
        raise RuntimeError("FRED key missing")
    out = MN.run(
        fred_fn=boom,                                   # FRED explodes
        indices_fn=lambda d: {"no_file": True, "rows_added": {}},
        declare_fn=lambda: {"declared": False, "horizons": {}},
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log")
    assert "error" in out["stages"]["fred"]             # named, not raised
    assert out["stages"]["indices"]["no_file"] is True  # still ran
    assert out["stages"]["declare"]["declared"] is False
    assert (tmp_path / "hb.log").exists()               # heartbeat still fired


def test_declare_failure_is_isolated(tmp_path):
    def boom():
        raise ValueError("templates artifact missing")
    out = MN.run(
        fred_fn=lambda: {"ok": ["BRENT"], "failed": []},
        indices_fn=lambda d: {"no_file": False, "rows_added": {"NIFTY": 1}},
        declare_fn=boom,
        clock=lambda: date(2026, 7, 23), heartbeat_path=tmp_path / "hb.log")
    assert out["stages"]["fred"]["ok"] == ["BRENT"]
    assert "error" in out["stages"]["declare"]

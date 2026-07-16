"""
Tests for the Phase 6 forecast <-> Brain Map wiring: when the current
setup carries active pattern tags, the forecast pulls historical
performance stats from the map and embeds them in its payload -- and
degrades gracefully (memory keys just None) when there's no history.

Everything runs offline: analyze() is patched with fake technicals (same
approach as test_forecast.py) and the Brain Map is an in-memory SQLite
database, so no network and no real data files are touched.

Run either of these from the project folder:
    python tests/test_forecast_memory.py    (simple, no extra installs)
    python -m pytest tests/                 (if you have pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
import src.forecast as forecast_module
from src.forecast import forecast, describe, _active_pattern_tags, _memory_lookup


def make_analysis(ticker="TEST.NS", uptrend=True, fresh_cross=False, rsi=50, price=100.0):
    return {
        "ticker": ticker,
        "uptrend": uptrend,
        "fresh_cross": fresh_cross,
        "rsi": rsi,
        "price": price,
    }


def with_fake_analysis(result):
    forecast_module.analyze = lambda ticker: result


def seeded_brain():
    """In-memory Brain Map holding 2 wins + 1 loss on golden-cross trades
    (win rate 67%, avg R (2+1-1)/3 = +0.67), same shape ingest_existing
    writes: a `fresh_cross` signal event and a `golden_cross` pattern
    event linked to each outcome."""
    conn = brain_map.connect(":memory:")
    for ref, r in [("t1", 2.0), ("t2", 1.0), ("t3", -1.0)]:
        signal_ev = brain_map.record_event(conn, "2026-07-01", "TCS.NS",
                                           "signal", "fresh_cross")
        pattern_ev = brain_map.record_event(conn, "2026-07-01", "TCS.NS",
                                            "pattern", "golden_cross")
        outcome = brain_map.record_outcome(conn, ref, "2026-07-05", "TCS.NS",
                                           archetype="fresh_cross", r_multiple=r)
        brain_map.link_event_outcome(conn, signal_ev, outcome)
        brain_map.link_event_outcome(conn, pattern_ev, outcome)
    return conn


def test_active_tags_cover_cross_and_oversold_setups():
    assert _active_pattern_tags(make_analysis(fresh_cross=True, uptrend=True), 30) == \
        ["fresh_cross", "golden_cross"]
    assert _active_pattern_tags(make_analysis(rsi=25), 30) == ["rsi_oversold"]
    assert _active_pattern_tags(make_analysis(), 30) == []
    # A fresh cross in a DOWNtrend is a Death Cross -- not a bullish pattern tag.
    assert _active_pattern_tags(make_analysis(fresh_cross=True, uptrend=False), 30) == []


def test_forecast_embeds_memory_stats_in_payload():
    with_fake_analysis(make_analysis(fresh_cross=True, uptrend=True))
    result = forecast("TEST.NS", {}, {}, brain=seeded_brain())
    assert result["memory"] == {"tags": ["fresh_cross", "golden_cross"],
                                "count": 3, "win_rate": 0.67,
                                "avg_r_multiple": 0.67}
    assert result["memory_context"] == (
        "Historical Performance for active patterns [fresh_cross, golden_cross]: "
        "Win Rate: 67%, Avg R-Multiple: +0.67 over 3 historical trades.")


def test_describe_includes_the_memory_line():
    with_fake_analysis(make_analysis(fresh_cross=True, uptrend=True))
    result = forecast("TEST.NS", {}, {}, brain=seeded_brain())
    assert "memory: Historical Performance" in describe(result)


def test_memory_adds_no_points_to_the_score():
    with_fake_analysis(make_analysis(fresh_cross=True, uptrend=True))
    with_memory = forecast("TEST.NS", {}, {}, brain=seeded_brain())
    without_memory = forecast("TEST.NS", {}, {}, brain=brain_map.connect(":memory:"))
    assert with_memory["score"] == without_memory["score"]
    assert with_memory["bias"] == without_memory["bias"]
    assert with_memory["confidence"] == without_memory["confidence"]


def test_empty_database_degrades_gracefully():
    with_fake_analysis(make_analysis(fresh_cross=True, uptrend=True))
    result = forecast("TEST.NS", {}, {}, brain=brain_map.connect(":memory:"))
    assert result["memory"] is None
    assert result["memory_context"] is None
    assert result["bias"] == "bullish"  # standard flow continues untouched


def test_no_active_tags_means_no_query_even_with_history():
    with_fake_analysis(make_analysis(fresh_cross=False, rsi=50))
    result = forecast("TEST.NS", {}, {}, brain=seeded_brain())
    assert result["memory"] is None
    assert result["memory_context"] is None


def test_broken_brain_connection_degrades_gracefully():
    conn = seeded_brain()
    conn.close()  # queries on it now raise sqlite3.ProgrammingError
    with_fake_analysis(make_analysis(fresh_cross=True, uptrend=True))
    result = forecast("TEST.NS", {}, {}, brain=conn)
    assert result["memory"] is None
    assert result["bias"] == "bullish"


def test_memory_lookup_reports_missing_avg_r_as_na():
    conn = brain_map.connect(":memory:")
    ev = brain_map.record_event(conn, "2026-07-01", "TCS.NS", "pattern", "golden_cross")
    out = brain_map.record_outcome(conn, "t1", "2026-07-05", "TCS.NS", result="win")
    brain_map.link_event_outcome(conn, ev, out)
    memory = _memory_lookup(["golden_cross"], brain=conn)
    assert memory["avg_r_multiple"] is None
    assert "Avg R-Multiple: n/a over 1 historical trades" in memory["context"]


# --------------------------------- registry-state stamp (salvage #2)

def test_memory_line_carries_registry_state_stamp_when_registered():
    """§7.2: existing surfaces keep rendering, stamped with registry state
    inline. A registered pattern naming an active tag adds the advisory
    [registry: ...] suffix + registry_states — gating nothing."""
    from src.validation import registry as rg
    conn = seeded_brain()
    rg.register(conn, "cooccurrence",
                {"kind": "cooccurrence", "tags": ["golden_cross"]})
    memory = _memory_lookup(["golden_cross"], brain=conn)
    assert memory["registry_states"] == {"golden_cross": "CANDIDATE"}
    assert "[registry: golden_cross=CANDIDATE]" in memory["context"]
    assert "Historical Performance" in memory["context"]   # line still leads


def test_memory_line_unstamped_when_nothing_registered():
    """No registered patterns -> the memory line reads exactly as before
    the stamp existed (registry_states None, no suffix)."""
    memory = _memory_lookup(["golden_cross"], brain=seeded_brain())
    assert memory["registry_states"] is None
    assert "[registry:" not in memory["context"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

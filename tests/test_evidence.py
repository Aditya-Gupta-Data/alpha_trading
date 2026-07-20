"""
Tests for the Evidence Snapshot substrate (Phase 2). Fully offline.

Run either of these from the project folder:
    python tests/test_evidence.py
    python -m pytest tests/test_evidence.py
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.confluence import evidence as ev


def test_technical_adapter_direction_strength_and_abstention():
    e = ev.technical_evidence({"uptrend": True, "fresh_cross": True,
                               "rsi": 28.0})
    assert e["direction"] == 1.0 and e["stance"] == "bullish"
    assert e["strength"] == 1.0 and not e["abstained"]      # 0.4+0.4+0.2
    e = ev.technical_evidence({"uptrend": False, "fresh_cross": False,
                               "rsi": 50.0})
    assert e["direction"] == -1.0 and e["strength"] == 0.4
    assert ev.technical_evidence(None)["abstained"] is True
    assert ev.technical_evidence({})["abstained"] is True


def test_news_adapter_stale_and_neutral_abstain():
    fresh = datetime.now(timezone.utc).isoformat()
    assert ev.news_evidence({"sentiment_score": 3, "stale": True})["abstained"]
    assert ev.news_evidence({"sentiment_score": 0, "stale": False,
                             "last_updated": fresh})["abstained"]
    e = ev.news_evidence({"sentiment_score": -5, "stale": False,
                          "last_updated": fresh,
                          "headline_focus": "fraud probe"})
    assert e["direction"] == -1.0 and "fraud probe" in e["detail"]


def test_news_adapter_abstains_on_aged_reads():
    """stale=false never ages — the adapter judges age via the same
    news_processor.entry_is_fresh gate forecast uses (single source)."""
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    e = ev.news_evidence({"sentiment_score": -5, "stale": False,
                          "last_updated": old, "headline_focus": "old news"})
    assert e["abstained"] and "too old" in e["detail"]
    # Missing timestamp is not fresh either.
    assert ev.news_evidence({"sentiment_score": -5, "stale": False})["abstained"]


def test_macro_adapter_blends_horizons_and_routes_bank_names():
    matrix = {"source": "dhan", "index_impact": {
        "NIFTY 50": {"SHORT": -0.5, "MEDIUM": -0.25},
        "NIFTY BANK": {"SHORT": 0.5, "MEDIUM": 0.5}}}
    e = ev.macro_evidence(matrix, "RELIANCE.NS")
    assert e["direction"] == -0.4 and e["stance"] == "headwind"  # .6*-.5+.4*-.25
    e = ev.macro_evidence(matrix, "HDFCBANK.NS")
    assert e["direction"] == 0.5 and e["stance"] == "tailwind"
    assert ev.macro_evidence({"source": "none"}, "X.NS")["abstained"]
    assert ev.macro_evidence(None, "X.NS")["abstained"]


def test_affinity_adapter_reads_group_bias():
    groups = {"ticker_to_group": {"ADANIENT.NS": "ADANI"}}
    rm = {"groups": {"ADANI": {"net_bias": "distribution",
                               "linked_entities": [
                                   {"recent_direction": "distributing"}]}}}
    e = ev.affinity_evidence(rm, "ADANIENT.NS", groups=groups)
    assert e["direction"] == -1.0 and e["stance"] == "distribution"
    assert e["strength"] == 0.75                       # 0.5 + 0.25 * 1 mover
    assert ev.affinity_evidence(rm, "WIPRO.NS", groups=groups)["abstained"]
    rm_mixed = {"groups": {"ADANI": {"net_bias": "mixed",
                                     "linked_entities": []}}}
    assert ev.affinity_evidence(rm_mixed, "ADANIENT.NS",
                                groups=groups)["abstained"]


def test_flows_and_vix_adapters():
    e = ev.flows_evidence({"fii": {"net": -1500.0}, "dii": {"net": 900.0}})
    assert e["direction"] == -0.5 and e["stance"] == "fii_selling"
    assert ev.flows_evidence({})["abstained"]
    assert ev.vix_evidence(None)["abstained"]
    assert ev.vix_evidence(18.0)["stance"] == "high_vix"
    assert ev.vix_evidence(18.0)["direction"] == -1.0
    assert ev.vix_evidence(12.0)["stance"] == "low_vix"
    assert ev.vix_evidence(14.5)["stance"] == "mid_vix"


def test_snapshot_records_explicit_abstention_never_guessed_neutral():
    snap = ev.build_evidence_snapshot("TCS.NS", today=date(2026, 7, 10),
                                      analysis={"uptrend": True,
                                                "fresh_cross": False,
                                                "rsi": 45.0},
                                      vix=14.0)
    assert snap["ticker"] == "TCS.NS" and len(snap["layers"]) == 6
    by_layer = {e["layer"]: e for e in snap["layers"]}
    assert not by_layer["technical"]["abstained"]
    assert not by_layer["vix_regime"]["abstained"]
    # Everything the caller didn't consult is an EXPLICIT abstention.
    for layer in ("news", "macro", "affinity", "flows"):
        assert by_layer[layer]["abstained"] is True
        assert by_layer[layer]["direction"] == 0.0


def test_days_to_results_rides_the_snapshot():
    snap = ev.build_evidence_snapshot(
        "TCS.NS", today=date(2026, 7, 10),
        earnings_calendar={"TCS.NS": "2026-07-14"})
    assert snap["days_to_results"] == 4


def test_summarize_collapses_abstentions():
    snap = ev.build_evidence_snapshot("TCS.NS", today=date(2026, 7, 10),
                                      analysis={"uptrend": True,
                                                "fresh_cross": True,
                                                "rsi": 25.0})
    text = ev.summarize(snap)
    assert "technical ↑ bullish" in text
    assert "5 layer(s) abstained" in text
    assert "distribution" not in text            # abstained layers collapsed


def test_persist_first_capture_wins_and_loads_back():
    conn = brain_map.connect(":memory:")
    snap = ev.build_evidence_snapshot("TCS.NS", today=date(2026, 7, 10),
                                      vix=14.0)
    assert ev.persist_snapshot(conn, "ref001", snap) is True
    # A later re-stamp must NOT overwrite what the proposal actually saw.
    other = ev.build_evidence_snapshot("TCS.NS", today=date(2026, 7, 11),
                                       vix=20.0)
    ev.persist_snapshot(conn, "ref001", other)
    stored = ev.load_snapshot(conn, "ref001")
    assert stored["as_of"] == "2026-07-10"
    assert ev.load_snapshot(conn, "ghost") is None
    assert ev.persist_snapshot(conn, "", snap) is False


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

"""
Tests for dual-horizon news sentiment + reversal flags (news_processor v3).

The owner's spec (2026-07-20): every share gets TWO sentiment reads —
short_term_catalyst_score (days; the swing driver) and long_term_macro_score
(months/structural) — and each new read is LINKED to the previous fresh read
so a drastic overnight narrative change gets flagged, on either horizon.

Back-compat contract: `sentiment_score` remains present and IS the
short-term score, so forecast/evidence/brain_map consumers keep working
unchanged.

Fully offline. Run either of these from the project folder:
    python -m pytest tests/test_news_dual_horizon.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.news_processor as np_module
from src.news_processor import (_clean_entry, _neutral_entry, _now,
                                build_sentiment, detect_reversal,
                                link_previous)


def _iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ------------------------------------------------------- entry schema

def test_clean_entry_carries_both_horizons_and_backcompat_score():
    raw = {"short_term_catalyst_score": 4, "long_term_macro_score": -2,
           "headline_focus": "earnings beat but demerger overhang"}
    e = _clean_entry(raw, _now())
    assert e["short_term_catalyst_score"] == 4
    assert e["long_term_macro_score"] == -2
    assert e["sentiment_score"] == 4          # back-compat = short-term
    assert e["stale"] is False


def test_clean_entry_clamps_and_coerces_both_scores():
    raw = {"short_term_catalyst_score": "9", "long_term_macro_score": -7.6}
    e = _clean_entry(raw, _now())
    assert e["short_term_catalyst_score"] == 5
    assert e["long_term_macro_score"] == -5


def test_clean_entry_legacy_model_reply_falls_back_to_sentiment_score():
    # A model that answers the OLD schema (sentiment_score only) must not
    # zero the read: short-term inherits it, long-term stays None (unknown
    # is not neutral — None never fires a reversal flag).
    e = _clean_entry({"sentiment_score": -3, "headline_focus": "probe"}, _now())
    assert e["sentiment_score"] == -3
    assert e["short_term_catalyst_score"] == -3
    assert e["long_term_macro_score"] is None


def test_neutral_entry_has_both_horizons_zero_and_stale():
    e = _neutral_entry(_now())
    assert e["short_term_catalyst_score"] == 0
    assert e["long_term_macro_score"] == 0
    assert e["stale"] is True


# ------------------------------------------------------- reversal rule

def test_reversal_flags_on_sign_cross_with_meaningful_delta():
    # Crossed (or touched) neutral AND moved >= 3 points -> drastic.
    assert detect_reversal(prev=2, today=-2) is True     # +2 -> -2
    assert detect_reversal(prev=-3, today=1) is True     # -3 -> +1
    assert detect_reversal(prev=3, today=0) is True      # strong story evaporated
    assert detect_reversal(prev=0, today=-3) is True     # new strong negative


def test_no_reversal_on_same_side_softening_or_small_moves():
    assert detect_reversal(prev=5, today=2) is False     # softer, same side
    assert detect_reversal(prev=-5, today=-1) is False
    assert detect_reversal(prev=1, today=-1) is False    # crossed but tiny
    assert detect_reversal(prev=2, today=2) is False
    assert detect_reversal(prev=None, today=4) is False  # unknown prev
    assert detect_reversal(prev=3, today=None) is False  # unknown today


# ------------------------------------------------------- prev linking

def test_link_previous_records_prev_and_flags_both_horizons():
    prev_entry = {"sentiment_score": 3, "short_term_catalyst_score": 3,
                  "long_term_macro_score": 4, "headline_focus": "capex cycle",
                  "last_updated": _iso_ago(24), "stale": False}
    today = _clean_entry({"short_term_catalyst_score": -2,
                          "long_term_macro_score": 4,
                          "headline_focus": "fraud probe"}, _now())
    linked = link_previous(today, prev_entry)
    assert linked["prev"]["short_term"] == 3
    assert linked["prev"]["long_term"] == 4
    assert linked["prev"]["read_at"] == prev_entry["last_updated"]
    assert linked["reversal"] == {"short_term": True, "long_term": False}


def test_link_previous_ignores_aged_or_stale_prev():
    today = _clean_entry({"short_term_catalyst_score": -4,
                          "long_term_macro_score": -4,
                          "headline_focus": "crash"}, _now())
    # 11-day-old prev is not "kal" — no link, no flag (the Issue 22 lesson:
    # age judgments use news_processor.entry_is_fresh, the single source).
    aged = {"sentiment_score": 4, "short_term_catalyst_score": 4,
            "long_term_macro_score": 4, "last_updated": _iso_ago(11 * 24),
            "stale": False}
    linked = link_previous(dict(today), aged)
    assert linked["prev"] is None
    assert linked["reversal"] == {"short_term": False, "long_term": False}
    # A stale prev (fallback neutral) is a fake number — never a baseline.
    stale = {"sentiment_score": 0, "short_term_catalyst_score": 0,
             "long_term_macro_score": 0, "last_updated": _iso_ago(24),
             "stale": True}
    linked = link_previous(dict(today), stale)
    assert linked["prev"] is None
    assert linked["reversal"] == {"short_term": False, "long_term": False}


def test_link_previous_from_legacy_single_score_file():
    # The file on disk today is v2 (single sentiment_score). First v3 run
    # must still link short-term from it; long-term prev is unknown -> no
    # long-term flag possible.
    legacy_prev = {"sentiment_score": 4, "headline_focus": "rally",
                   "last_updated": _iso_ago(24), "stale": False}
    today = _clean_entry({"short_term_catalyst_score": -1,
                          "long_term_macro_score": -5,
                          "headline_focus": "scam unearthed"}, _now())
    linked = link_previous(today, legacy_prev)
    assert linked["prev"]["short_term"] == 4
    assert linked["prev"]["long_term"] is None
    assert linked["reversal"]["short_term"] is True   # +4 -> -1
    assert linked["reversal"]["long_term"] is False   # no baseline, no flag


# ------------------------------------------------------- pipeline seam

def _fake_headlines(ticker):
    return [f"{ticker} headline"]


def test_build_sentiment_links_against_previous_file_and_notifies(monkeypatch):
    monkeypatch.setattr(np_module, "fetch_headlines", _fake_headlines)
    monkeypatch.setattr(np_module, "_call_gemini", lambda prompt, key: {
        "TCS.NS": {"short_term_catalyst_score": -4, "long_term_macro_score": 2,
                   "headline_focus": "guidance cut"}})
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with tempfile.TemporaryDirectory() as tmp:
        prev_path = Path(tmp) / "news_sentiment.json"
        prev_path.write_text(json.dumps({
            "generated": _iso_ago(24), "source": "gemini",
            "tickers": {"TCS.NS": {"sentiment_score": 3,
                                   "short_term_catalyst_score": 3,
                                   "long_term_macro_score": 2,
                                   "headline_focus": "deal wins",
                                   "last_updated": _iso_ago(24),
                                   "stale": False}}}))
        notes = []
        data = build_sentiment(["TCS.NS"], previous_path=prev_path,
                               notify_fn=notes.append)
        entry = data["tickers"]["TCS.NS"]
        assert entry["reversal"]["short_term"] is True      # +3 -> -4
        assert entry["reversal"]["long_term"] is False      # +2 -> +2
        assert entry["prev"]["short_term"] == 3
        # One compact Discord note, naming the ticker and both readings.
        assert len(notes) == 1
        assert "TCS.NS" in notes[0] and "+3" in notes[0] and "-4" in notes[0]


def test_build_sentiment_quiet_when_no_reversals(monkeypatch):
    monkeypatch.setattr(np_module, "fetch_headlines", _fake_headlines)
    monkeypatch.setattr(np_module, "_call_gemini", lambda prompt, key: {
        "TCS.NS": {"short_term_catalyst_score": 3, "long_term_macro_score": 2,
                   "headline_focus": "steady demand"}})
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    with tempfile.TemporaryDirectory() as tmp:
        prev_path = Path(tmp) / "news_sentiment.json"
        prev_path.write_text(json.dumps({
            "generated": _iso_ago(24), "source": "gemini",
            "tickers": {"TCS.NS": {"sentiment_score": 2,
                                   "short_term_catalyst_score": 2,
                                   "long_term_macro_score": 2,
                                   "headline_focus": "deal wins",
                                   "last_updated": _iso_ago(24),
                                   "stale": False}}}))
        notes = []
        data = build_sentiment(["TCS.NS"], previous_path=prev_path,
                               notify_fn=notes.append)
        assert notes == []
        assert data["tickers"]["TCS.NS"]["reversal"] == {
            "short_term": False, "long_term": False}


def test_build_sentiment_first_run_has_no_prev(monkeypatch):
    monkeypatch.setattr(np_module, "fetch_headlines", _fake_headlines)
    monkeypatch.setattr(np_module, "_call_gemini", lambda prompt, key: {
        "TCS.NS": {"short_term_catalyst_score": 5, "long_term_macro_score": 5,
                   "headline_focus": "blowout quarter"}})
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with tempfile.TemporaryDirectory() as tmp:
        notes = []
        data = build_sentiment(["TCS.NS"],
                               previous_path=Path(tmp) / "ghost.json",
                               notify_fn=notes.append)
        entry = data["tickers"]["TCS.NS"]
        assert entry["prev"] is None
        assert entry["reversal"] == {"short_term": False, "long_term": False}
        assert notes == []


def test_gemini_failure_never_links_or_flags(monkeypatch):
    monkeypatch.setattr(np_module, "fetch_headlines", _fake_headlines)
    def _boom(prompt, key):
        raise RuntimeError("api down")
    monkeypatch.setattr(np_module, "_call_gemini", _boom)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with tempfile.TemporaryDirectory() as tmp:
        prev_path = Path(tmp) / "news_sentiment.json"
        prev_path.write_text(json.dumps({
            "generated": _iso_ago(24), "source": "gemini",
            "tickers": {"TCS.NS": {"sentiment_score": 3,
                                   "short_term_catalyst_score": 3,
                                   "long_term_macro_score": 3,
                                   "last_updated": _iso_ago(24),
                                   "stale": False}}}))
        notes = []
        data = build_sentiment(["TCS.NS"], previous_path=prev_path,
                               notify_fn=notes.append)
        entry = data["tickers"]["TCS.NS"]
        assert entry["stale"] is True
        # A fallback 0 is a fake number — it must not read as "collapsed
        # from +3" (that would flag a reversal that never happened).
        assert entry["reversal"] == {"short_term": False, "long_term": False}
        assert notes == []


def test_as_mapping_coerces_gemini_array_shapes():
    """Seen live on the VM's first v3 run: Gemini answered with a JSON
    array and 'list'.get crashed the whole run. Both array shapes coerce;
    junk contributes nothing (stale-neutral downstream), never a crash."""
    entry = {"short_term_catalyst_score": 2, "long_term_macro_score": 1}
    assert np_module._as_mapping({"TCS.NS": entry}) == {"TCS.NS": entry}
    rows = [dict(entry, ticker="TCS.NS"), {"INFY.NS": entry},
            "junk", {"a": 1, "b": 2}, 42]
    m = np_module._as_mapping(rows)
    assert set(m) == {"TCS.NS", "INFY.NS"}
    assert m["TCS.NS"]["short_term_catalyst_score"] == 2
    assert np_module._as_mapping(None) == {}


def test_build_sentiment_survives_gemini_array_reply(monkeypatch):
    monkeypatch.setattr(np_module, "fetch_headlines", _fake_headlines)
    monkeypatch.setattr(np_module, "_call_gemini", lambda prompt, key: [
        {"ticker": "TCS.NS", "short_term_catalyst_score": -3,
         "long_term_macro_score": 1, "headline_focus": "guidance cut"}])
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with tempfile.TemporaryDirectory() as tmp:
        data = build_sentiment(["TCS.NS", "INFY.NS"],
                               previous_path=Path(tmp) / "ghost.json",
                               notify_fn=lambda t: None)
        assert data["tickers"]["TCS.NS"]["short_term_catalyst_score"] == -3
        assert data["tickers"]["TCS.NS"]["stale"] is False
        # The ticker the array never mentioned: honest stale-neutral.
        assert data["tickers"]["INFY.NS"]["stale"] is True


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_news_dual_horizon.py")

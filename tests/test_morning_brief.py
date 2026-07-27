"""
tests/test_morning_brief.py — Directive 2: the distinct pre-open card.
Hermetic — every data source is injected; no live file/network reads.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import morning_brief as mb


def test_no_halt_no_regime_gives_a_clean_minimal_card(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_read_journal", lambda path=None: [])
    card = mb.build_morning_brief(
        journal_path=tmp_path / "none.jsonl", calendar={},
        watchlist_path=tmp_path / "none.yaml",
        regime_sentence_fn=lambda: None, halt_lines_fn=lambda: [])
    assert card["event"] == "morning_brief"
    names = [f["name"] for f in card["fields"]]
    assert "🔴 SYSTEM PAUSED" not in names
    assert "🌍 Overnight Macro Read" not in names   # honest: nothing to say
    assert "📅 Today's Watchlist Events" in names
    assert "💼 Book Going Into Today" in names


def test_halt_banner_leads_when_injected(tmp_path):
    card = mb.build_morning_brief(
        journal_path=tmp_path / "none.jsonl", calendar={},
        watchlist_path=tmp_path / "none.yaml",
        regime_sentence_fn=lambda: None,
        halt_lines_fn=lambda: ["🔴 SYSTEM PAUSED — test halt"])
    assert card["fields"][0]["name"] == "🔴 SYSTEM PAUSED"


def test_regime_sentence_surfaces_verbatim_from_the_gated_source(tmp_path):
    """morning_brief never re-derives the honesty gates itself — it just
    renders whatever ceo_language.macro_regime_sentence (the one gated
    function) hands it."""
    card = mb.build_morning_brief(
        journal_path=tmp_path / "none.jsonl", calendar={},
        watchlist_path=tmp_path / "none.yaml",
        regime_sentence_fn=lambda: "🌍 Macro regime: still accumulating — x.",
        halt_lines_fn=lambda: [])
    field = [f for f in card["fields"]
            if f["name"] == "🌍 Overnight Macro Read"][0]
    assert "still accumulating" in field["value"]


def test_events_field_reports_only_known_dates_within_lookahead(tmp_path,
                                                                 monkeypatch):
    rows = [{"decision": "approved", "outcome": None,
            "spread": {"underlying": "NIFTY"}}]
    monkeypatch.setattr(mb, "_read_journal", lambda path=None: rows)
    calendar = {"NIFTY": "2026-07-28", "TCS.NS": "2026-08-15"}  # 1d and 19d out
    card = mb.build_morning_brief(
        journal_path=tmp_path / "none.jsonl", calendar=calendar,
        watchlist_path=tmp_path / "none.yaml",
        regime_sentence_fn=lambda: None, halt_lines_fn=lambda: [],
        clock=lambda: "2026-07-27")
    field = [f for f in card["fields"]
            if f["name"] == "📅 Today's Watchlist Events"][0]
    assert "NIFTY" in field["value"] and "1 day" in field["value"]
    assert "TCS" not in field["value"]     # outside the lookahead — honest omission


def test_book_field_never_fabricates_when_journal_unreadable(tmp_path,
                                                              monkeypatch):
    def boom(path=None):
        raise OSError("disk gone")
    monkeypatch.setattr(mb, "_read_journal", boom)
    card = mb.build_morning_brief(
        journal_path=tmp_path / "none.jsonl", calendar={},
        watchlist_path=tmp_path / "none.yaml",
        regime_sentence_fn=lambda: None, halt_lines_fn=lambda: [])
    # fails open: the book field is simply absent, never a crash / a guess
    assert all(f["name"] != "💼 Book Going Into Today" for f in card["fields"])

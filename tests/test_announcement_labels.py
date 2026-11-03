"""Deterministic label triage in front of corporate_risk_halt. Hermetic: tmp lakes, injected freshness."""
import gzip
import json
from datetime import date

from src.analysis import announcement_labels as AL
from src.analysis import equity_entry_checks as EEC

FRESH = {"state": "fresh", "reason": "events lake fresh"}
TODAY = date(2026, 11, 2)


def _lake(tmp_path, rows):
    part = tmp_path / "lake" / "events" / "date=2026-10-30"
    part.mkdir(parents=True)
    with gzip.open(part / "part.jsonl.gz", "wt") as f:
        for r in rows:
            f.write(json.dumps({"as_of": "2026-10-30", "ticker": f"{r['symbol']}.NS",
                                "attachment": None, **r}) + "\n")
    return tmp_path / "lake"


def _halt(root, symbol, direction="long"):
    return EEC.corporate_risk_halt({"symbol": symbol, "direction": direction, "instrument": "delivery"},
                                   today=TODAY, lake_root=root,
                                   staleness_fn=lambda name, as_of=None: FRESH)


def test_tiers_ignore_case_and_spacing_and_unknown_labels_fall_back():
    assert AL.tier("Corporate Insolvency Resolution Process") == AL.HARD_BLOCK
    assert AL.tier("  copy of  newspaper publication ") == AL.ALLOW_ROUTINE
    assert AL.tier("General updates") == AL.tier("General Updates") == AL.KEYWORD_FALLBACK
    assert AL.tier("A label NSE invents next year") == AL.KEYWORD_FALLBACK
    assert AL.tier(None) == AL.KEYWORD_FALLBACK


def test_no_label_sits_in_two_tiers():
    hard = {AL._norm(x) for x in AL.HARD_BLOCK_LABELS}
    allow = {AL._norm(x) for x in AL.ALLOW_ROUTINE_LABELS}
    fallback = {AL._norm(x) for x in AL.KEYWORD_FALLBACK_LABELS}
    assert not (hard & allow) and not (hard & fallback) and not (allow & fallback)


def test_allow_routine_overrides_a_false_positive_legal_keyword(tmp_path):
    root = _lake(tmp_path, [{"symbol": "ABC", "subject": "Copy of Newspaper Publication",
                             "flags": ["LEGAL_RISK"]}])
    assert EEC.recent_risk_events("ABC", today=TODAY, lake_root=root) == []
    assert _halt(root, "ABC") == (True, None)


def test_allow_routine_still_blocks_a_structural_flag(tmp_path):
    root = _lake(tmp_path, [{"symbol": "ABC", "subject": "Copy of Newspaper Publication",
                             "flags": ["LEGAL_RISK", "STRUCTURAL_RISK"]}])
    ok, reason = _halt(root, "ABC")
    assert ok is False and reason.startswith("STRUCTURAL_RISK filed 2026-10-30")


def test_hard_block_label_blocks_even_without_a_keyword_flag(tmp_path):
    root = _lake(tmp_path, [{"symbol": "XYZ", "subject": "Defaults on Payment of Interest/Principal",
                             "flags": []}])
    ok, reason = _halt(root, "XYZ")
    assert ok is False
    assert "LEGAL_RISK (HARD_BLOCK label)" in reason and "Defaults on Payment" in reason
    hit = EEC.recent_risk_events("XYZ", today=TODAY, lake_root=root)[0]
    assert hit["triage"] == AL.HARD_BLOCK and hit["risk_flags"] == ["LEGAL_RISK"]


def test_keyword_fallback_keeps_the_legacy_verdict(tmp_path):
    root = _lake(tmp_path, [{"symbol": "FLG", "subject": "Updates", "flags": ["LEGAL_RISK"]},
                            {"symbol": "CLN", "subject": "Updates", "flags": ["CATALYST"]}])
    ok, reason = _halt(root, "FLG")
    assert ok is False and reason.startswith("LEGAL_RISK filed")
    assert _halt(root, "CLN") == (True, None)


def test_one_routine_filing_never_clears_a_real_one(tmp_path):
    root = _lake(tmp_path, [{"symbol": "MIX", "subject": "Shareholders meeting", "flags": ["LEGAL_RISK"]},
                            {"symbol": "MIX", "subject": "Corporate Insolvency Resolution Process",
                             "flags": ["LEGAL_RISK"]}])
    ok, reason = _halt(root, "MIX")
    assert ok is False and "Corporate Insolvency" in reason


def test_shorts_and_a_stale_feed_are_unchanged(tmp_path):
    root = _lake(tmp_path, [{"symbol": "XYZ", "subject": "Liquidation", "flags": []}])
    assert _halt(root, "XYZ", direction="short") == (True, None)
    ok, reason = EEC.corporate_risk_halt({"symbol": "ABC", "direction": "long"}, today=TODAY,
                                         lake_root=root,
                                         staleness_fn=lambda name, as_of=None: {"state": "stale",
                                                                                "reason": "old"})
    assert ok is False and "stale" in reason

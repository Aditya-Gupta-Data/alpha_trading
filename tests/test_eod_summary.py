"""
tests/test_eod_summary.py — Directive 2 (CEO-View Discord): the EOD card's
plain-English lead line. Hermetic: journal/db paths point at tmp fixtures.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import eod_summary


def test_plain_english_field_leads_with_no_positions(monkeypatch):
    monkeypatch.setattr(eod_summary, "_read_journal", lambda path=None: [])
    monkeypatch.setattr(eod_summary, "query_todays_resolutions",
                        lambda db_path=None: [])
    card = eod_summary.build_eod_card()
    plain = [f for f in card["fields"] if f["name"] == "📋 Plain English"]
    assert plain and plain[0]["value"] == \
        "no open positions, flat today, book is flat directionally."


def test_plain_english_field_reflects_open_positions_and_pnl(monkeypatch):
    rows = [{"decision": "approved", "outcome": None,
            "plan": {"ticker": "TCS.NS"}, "ticker": "TCS.NS"}]
    monkeypatch.setattr(eod_summary, "_read_journal", lambda path=None: rows)
    monkeypatch.setattr(eod_summary, "query_todays_resolutions",
                        lambda db_path=None: [])
    card = eod_summary.build_eod_card()
    plain = [f for f in card["fields"] if f["name"] == "📋 Plain English"][0]
    assert "1 open position," in plain["value"]
    assert "flat today" in plain["value"]

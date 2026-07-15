"""
Tests for src/book_context.py — the "what we hold and why" read-model.

Offline: journal entries injected everywhere; no file/Dhan/DB access.

    python -m pytest tests/test_book_context.py -q
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import book_context as bc

TODAY = date(2026, 7, 15)


def spread_entry(short_id="abc123", ticker="NIFTY 50", strategy="bear_put_spread",
                 direction="bearish", signal="bearish trend read — test",
                 opened="2026-07-12", expiry="2026-07-28", lots=1,
                 max_loss=6000.0, decision="approved", outcome=None,
                 regime=None):
    return {
        "short_id": short_id, "ticker": ticker, "decision": decision,
        "outcome": outcome, "date": opened, "signal": signal,
        "regime": regime or {},
        "spread": {"strategy": strategy, "direction": direction,
                   "expiry": expiry, "lots": lots, "lot_size": 65,
                   "max_loss": max_loss, "max_profit": 4000.0,
                   "legs": [{"side": "BUY", "option_type": "PE",
                             "strike": 25000.0, "premium": 100.0}]},
    }


# ------------------------------------------------------------- dossier

def test_dossier_carries_why_and_status():
    e = spread_entry(regime={"regime_trend": "bearish", "regime_vix": "mid"})
    d = bc.position_dossier(e, TODAY)
    assert d["why"] == "bearish trend read — test"
    assert d["direction"] == "bearish"
    assert d["days_in_trade"] == 3            # 07-12 -> 07-15
    assert d["days_to_expiry"] == 13          # 07-15 -> 07-28
    assert d["max_loss_rs"] == 6000.0
    assert d["entry_regime"] == {"regime_trend": "bearish", "regime_vix": "mid"}


def test_dossier_none_for_resolved_rejected_or_equity():
    assert bc.position_dossier(spread_entry(outcome={"resolution": "x"}), TODAY) is None
    assert bc.position_dossier(spread_entry(decision="rejected"), TODAY) is None
    assert bc.position_dossier({"decision": "approved", "outcome": None,
                                "plan": {}}, TODAY) is None


# ------------------------------------------------------------- summary

def test_book_summary_counts_shape():
    entries = [
        spread_entry("a1", direction="bearish"),
        spread_entry("a2", ticker="NIFTY BANK", strategy="iron_condor",
                     direction="neutral", max_loss=4000.0),
        spread_entry("a3", outcome={"resolution": "profit_take"}),  # closed
    ]
    s = bc.book_summary(entries, TODAY)
    assert s["count"] == 2
    assert s["by_direction"] == {"bearish": 1, "neutral": 1}
    assert s["by_ticker"] == {"NIFTY 50": 1, "NIFTY BANK": 1}
    assert s["total_max_loss_rs"] == 10000.0


def test_book_summary_survives_malformed_entries():
    s = bc.book_summary([{"garbage": True}, None and {}, spread_entry()], TODAY)
    assert s["count"] == 1


# ------------------------------------------------------------- render

def test_render_book_empty_and_full():
    assert "empty" in bc.render_book(bc.book_summary([], TODAY))
    out = bc.render_book(bc.book_summary([spread_entry()], TODAY))
    assert "abc123" in out and "why: bearish trend read — test" in out


# ------------------------------------------------------------- book_line

def test_book_line_none_on_empty_book():
    assert bc.book_line("NIFTY 50", "bullish", entries=[], today=TODAY) is None


def test_book_line_mentions_same_ticker_holdings_with_why():
    line = bc.book_line("NIFTY 50", "bullish",
                        entries=[spread_entry()], today=TODAY)
    assert "Already on NIFTY 50" in line
    assert "abc123" in line and "bearish trend read — test" in line


def test_book_line_first_slot_phrasing_for_new_ticker():
    line = bc.book_line("NIFTY BANK", "bullish",
                        entries=[spread_entry()], today=TODAY)
    assert "first bullish NIFTY BANK position" in line


# ------------------------------------------------- proposal-card wiring

def test_proposal_card_includes_book_block():
    from src.options_proposer import _format_proposal_alert
    p = {"ticker": "NIFTY 50", "view": "bullish", "vix": 13.0, "lots": 1,
         "book_line": "📖 **Book**: 1 open (1 bearish).",
         "spread": {"strategy": "bull_call_spread", "expiry": "2026-07-28",
                    "lot_size": 65, "net_credit": None, "net_debit": 60.0,
                    "max_loss": 3900.0, "max_profit": 9100.0,
                    "margin": {"total_margin": 3900.0},
                    "legs": [{"side": "BUY", "option_type": "CE",
                              "strike": 25000.0, "premium": 100.0},
                             {"side": "SELL", "option_type": "CE",
                              "strike": 25200.0, "premium": 40.0}]}}
    card = _format_proposal_alert(p)
    assert "📖 **Book**: 1 open (1 bearish)." in card
    # and its absence leaves the card clean
    p2 = dict(p, book_line=None)
    assert "📖" not in _format_proposal_alert(p2)


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python", "-m", "pytest", __file__, "-q"]))

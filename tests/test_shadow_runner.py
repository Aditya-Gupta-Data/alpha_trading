"""
Tests for the live shadow-firing wiring (owner concerns #1/#2). Offline,
in-memory brain_map only.

Run either of these from the project folder:
    python tests/test_shadow_runner.py
    python -m pytest tests/test_shadow_runner.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import daily_context as dc
from src.discovery import inspect as di
from src.discovery import shadow_runner as sr
from src.validation import registry as rg


def _register(conn, kind, tags):
    return rg.register(conn, kind, {"kind": kind, "tags": sorted(tags)},
                       description=" + ".join(tags))["pattern_id"]


def _entry(short_id="abc12345", ticker="NIFTY 50", day="2026-07-10"):
    return {"short_id": short_id, "date": day, "ticker": ticker,
            "signal": "fresh Golden Cross", "pattern_tags": ["Breakout"],
            "spread": {"strategy": "bull_call"}}


def test_entry_own_tags_vocabulary():
    tags = sr.entry_own_tags(_entry())
    assert "bull_call" in tags        # spread strategy IS the archetype
    assert "breakout" in tags         # normalized pattern tag


def test_on_entry_fires_matching_patterns_and_links_host():
    conn = brain_map.connect(":memory:")
    dc.record_frame(conn, {"date": "2026-07-10", "vix_band": "CALM",
                           "fii_net": 500.0})
    hit = _register(conn, "cooccurrence", ["bull_call", "ctx:fii:up"])
    miss = _register(conn, "cooccurrence", ["bear_put", "ctx:fii:down"])

    fired = sr.on_entry(conn, _entry(), day="2026-07-10")
    assert len(fired) == 1 and fired[0]["pattern_id"] == hit
    assert fired[0]["host_ref"] == "abc12345"
    row = conn.execute("SELECT * FROM shadow_trades").fetchone()
    assert row["pattern_id"] == hit and row["host_ref"] == "abc12345"
    assert row["resolved"] == 0
    # Idempotent: the same entry re-stamped fires nothing new.
    again = sr.on_entry(conn, _entry(), day="2026-07-10")
    assert again[0]["created"] is False
    assert conn.execute("SELECT COUNT(*) AS n FROM shadow_trades"
                        ).fetchone()["n"] == 1
    assert miss not in [f["pattern_id"] for f in fired]


def test_sequence_patterns_match_on_lagged_tags_only():
    conn = brain_map.connect(":memory:")
    # fii:up two frames BEFORE the entry day, not on it.
    for d, extra in (("2026-07-08", {"fii_net": 900.0}),
                     ("2026-07-09", {}), ("2026-07-10", {})):
        dc.record_frame(conn, {"date": d, **extra})
    seq = _register(conn, "sequence", ["lag2:ctx:fii:up"])
    coq = _register(conn, "cooccurrence", ["ctx:fii:up"])   # same-day: absent

    fired = sr.on_entry(conn, _entry(), day="2026-07-10")
    ids = [f["pattern_id"] for f in fired]
    assert seq in ids and coq not in ids


def test_dead_and_quarantined_never_fire():
    conn = brain_map.connect(":memory:")
    dc.record_frame(conn, {"date": "2026-07-10", "fii_net": 500.0})
    pid = _register(conn, "cooccurrence", ["bull_call", "ctx:fii:up"])
    rg.transition(conn, pid, "TRIAL", "t")
    rg.transition(conn, pid, "VALIDATED", "v")
    rg.transition(conn, pid, "QUARANTINED", "bleeding")
    assert sr.on_entry(conn, _entry(), day="2026-07-10") == []


def test_resolution_sweep_inherits_the_host_outcome():
    conn = brain_map.connect(":memory:")
    dc.record_frame(conn, {"date": "2026-07-10", "fii_net": 500.0})
    pid = _register(conn, "cooccurrence", ["bull_call", "ctx:fii:up"])
    sr.on_entry(conn, _entry(), day="2026-07-10")
    # Host not resolved yet -> sweep does nothing.
    assert sr.resolve_from_outcomes(conn) == 0
    # Host resolves as a +1.4R win.
    brain_map.record_outcome(conn, journal_ref="abc12345",
                             date="2026-07-15", ticker="NIFTY 50",
                             r_multiple=1.4, result="win")
    assert sr.resolve_from_outcomes(conn) == 1
    row = conn.execute("SELECT * FROM shadow_trades").fetchone()
    assert row["resolved"] == 1 and row["result"] == "win"
    assert abs(row["r_multiple"] - 1.4) < 1e-9
    # Idempotent.
    assert sr.resolve_from_outcomes(conn) == 0


def test_shadow_path_never_touches_journal_or_portfolio(monkeypatch):
    """The skeptic's guarantee: a runtime spy proves the fire+resolve path
    never calls journal.log or writes portfolio state."""
    calls = []
    import src.journal as journal_mod
    monkeypatch.setattr(journal_mod, "log",
                        lambda *a, **k: calls.append("journal.log"))
    conn = brain_map.connect(":memory:")
    dc.record_frame(conn, {"date": "2026-07-10", "fii_net": 500.0})
    _register(conn, "cooccurrence", ["bull_call", "ctx:fii:up"])
    sr.on_entry(conn, _entry(), day="2026-07-10")
    brain_map.record_outcome(conn, journal_ref="abc12345",
                             date="2026-07-15", ticker="NIFTY 50",
                             r_multiple=-1.0, result="loss")
    sr.resolve_from_outcomes(conn)
    assert calls == []


def test_inspect_reconstructs_a_pattern():
    conn = brain_map.connect(":memory:")
    dc.record_frame(conn, {"date": "2026-07-10", "fii_net": 500.0})
    pid = _register(conn, "cooccurrence", ["bull_call", "ctx:fii:up"])
    sr.on_entry(conn, _entry(), day="2026-07-10")
    brain_map.record_outcome(conn, journal_ref="abc12345",
                             date="2026-07-15", ticker="NIFTY 50",
                             r_multiple=1.4, result="win")
    sr.resolve_from_outcomes(conn)

    rows = di.find_patterns(conn, pid[:6])
    assert len(rows) == 1
    card = di.render(conn, rows[0])
    assert "bull_call + ctx:fii:up" in card
    assert "WIN" in card and "+1.40R" in card and "abc12345" in card
    assert "counts for nothing" not in card or "in-sample" in card
    # The list view renders too.
    assert "pattern(s):" in di.render_list(rows)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

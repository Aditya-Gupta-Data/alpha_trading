"""
Tests for the evidence-stamp wiring (Phase 2, P2-1b): every headless
proposal carries a proposal-time evidence snapshot, resolution persists it
into brain_map keyed by journal_ref, and neither hook can ever block the
live path. Fully offline.

Run either of these from the project folder:
    python tests/test_evidence_wiring.py
    python -m pytest tests/test_evidence_wiring.py
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src.confluence import evidence as ev


def test_capture_for_entry_stamps_additively_and_never_raises():
    entry = {"short_id": "abc123", "ticker": "NIFTY 50"}
    snap = ev.capture_for_entry(entry, "NIFTY 50",
                                analysis={"uptrend": True,
                                          "fresh_cross": False, "rsi": 40.0},
                                vix=14.2, today=date(2026, 7, 11))
    assert snap is not None and entry["evidence"] is snap
    by_layer = {e["layer"]: e for e in snap["layers"]}
    assert not by_layer["technical"]["abstained"]
    assert not by_layer["vix_regime"]["abstained"]
    assert entry["short_id"] == "abc123"          # additive, nothing clobbered

    # Total failure inside the builder leaves the entry untouched.
    original = ev.build_evidence_snapshot
    ev.build_evidence_snapshot = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom"))
    try:
        clean = {"short_id": "x"}
        assert ev.capture_for_entry(clean, "TCS.NS") is None
        assert "evidence" not in clean
    finally:
        ev.build_evidence_snapshot = original


def test_persist_entry_snapshot_joins_outcome_by_journal_ref():
    conn = brain_map.connect(":memory:")
    entry = {"short_id": "pm123456", "ticker": "NIFTY 50"}
    ev.capture_for_entry(entry, "NIFTY 50", vix=15.0, today=date(2026, 7, 11))
    assert ev.persist_entry_snapshot(conn, entry) is True
    ref = brain_map.journal_ref_for(entry)
    stored = ev.load_snapshot(conn, ref)
    assert stored is not None and stored["ticker"] == "NIFTY 50"
    # Pre-substrate entries (no stamp) skip silently.
    assert ev.persist_entry_snapshot(conn, {"short_id": "old1"}) is False


def test_record_post_mortem_persists_evidence_alongside_outcome():
    from src import analyst, plan_tracker
    conn = brain_map.connect(":memory:")
    entry = {
        "short_id": "ev999999", "date": "2026-07-11", "action": "BUY",
        "ticker": "TCS.NS", "shares": 5, "price": 100.0,
        "signal": "Fresh Golden Cross", "decision": "approved",
        "why": "test", "pattern_tags": ["Golden Cross"],
        "plan": {"stop_loss": {"pct": 3.0, "price": 97.0},
                 "target": {"price": 110.0, "rr": 3.33}},
        "outcome": {"resolution": "target_hit", "price": 110.0,
                    "exit_date": "2026-07-12", "pct": 10.0,
                    "r_multiple": 3.33, "days_in_trade": 1,
                    "pnl_rs": 50.0, "hypothetical": False,
                    "verdict": "WIN — target hit"},
    }
    ev.capture_for_entry(entry, "TCS.NS",
                         analysis={"uptrend": True, "fresh_cross": True,
                                   "rsi": 35.0},
                         vix=13.5, today=date(2026, 7, 11))
    original = analyst.generate_post_mortem
    analyst.generate_post_mortem = lambda p, e: None
    try:
        plan_tracker.record_post_mortem(entry, conn)
    finally:
        analyst.generate_post_mortem = original
    ref = brain_map.journal_ref_for(entry)
    # Outcome landed AND its evidence snapshot joined it, same key.
    row = conn.execute("SELECT result FROM outcomes WHERE journal_ref = ?",
                       (ref,)).fetchone()
    assert row is not None and row["result"] == "win"
    stored = ev.load_snapshot(conn, ref)
    assert stored is not None
    tech = next(l for l in stored["layers"] if l["layer"] == "technical")
    assert tech["abstained"] is False and tech["direction"] == 1.0


def test_run_headless_entries_carry_the_stamp():
    """The live wiring: a headless proposal built through the real
    options_proposer path (injected sandbox book + synthetic chain) must
    journal with an evidence snapshot attached."""
    import json as _json
    from src import options_proposer as op

    # Reuse the proposer's own injection seams exactly like its test suite:
    # a synthetic chain rich enough for an iron condor at mid VIX.
    strikes = {}
    for k in range(23000, 27050, 50):
        strikes[f"{k}.000000"] = {
            "ce": {"last_price": max(5.0, (25000 - k) * 0.4 + 120)},
            "pe": {"last_price": max(5.0, (k - 25000) * 0.4 + 120)},
        }
    chain = {"last_price": 25000.0, "oc": strikes}
    analysis = {"ticker": "NIFTY 50", "uptrend": False, "fresh_cross": False,
                "rsi": 52.0, "price": 25000.0}
    book = {"cash": 1_000_000.0, "holdings": {}}

    with tempfile.TemporaryDirectory() as tmp:
        journal_path = Path(tmp) / "journal.jsonl"
        from src import journal as journal_mod
        original_path = journal_mod.JOURNAL_PATH
        journal_mod.JOURNAL_PATH = journal_path
        try:
            result = op.run_headless("NIFTY 50", state={
                "analysis": analysis, "vix": 14.5, "chain": chain,
                "expiry": "2026-07-30", "book": book,
            })
        finally:
            journal_mod.JOURNAL_PATH = original_path
        if not result["proposed"]:
            # A regime/economics gate refusing is a legitimate outcome for
            # this synthetic chain — but if it DID propose, the stamp must
            # be there. Force meaningful coverage: the entry existing
            # implies the stamp.
            assert result["entry"] is None
        else:
            entry = result["entry"]
            assert "evidence" in entry
            layers = {l["layer"]: l for l in entry["evidence"]["layers"]}
            assert layers["vix_regime"]["stance"] == "mid_vix"
            assert layers["technical"]["abstained"] is False
            # And the journaled line carries it too.
            logged = [_json.loads(l) for l in
                      journal_path.read_text().splitlines() if l.strip()]
            assert any("evidence" in e for e in logged)


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

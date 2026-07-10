"""
Tests for the decision receipt + src.explain (Phase 2, P2-2). Offline.

Run either of these from the project folder:
    python tests/test_explain.py
    python -m pytest tests/test_explain.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, explain as ex
from src.confluence import evidence as ev


def _entry(short_id="ex123456", with_extras=True):
    e = {"short_id": short_id, "date": "2026-07-11", "ticker": "NIFTY 50",
         "decision": "approved", "signal": "iron_condor mid-VIX",
         "why": "auto-proposed",
         "spread": {"strategy": "iron_condor", "legs": [1, 2, 3, 4],
                    "max_profit_rs": 4200.0, "max_loss_rs": 5800.0}}
    if with_extras:
        e["receipt"] = {"underlying": "NIFTY 50", "vix": 14.2,
                        "analysis": {"uptrend": False, "fresh_cross": False,
                                     "rsi": 51.0, "price": 25000.0},
                        "vol_overrides": {"risk_pct": 7.0},
                        "book": "real", "memory_context": "",
                        "skeptic_note": ""}
        ev.capture_for_entry(e, "NIFTY 50",
                             analysis=e["receipt"]["analysis"], vix=14.2,
                             today=date(2026, 7, 11))
    return e


def test_explain_renders_receipt_evidence_and_absences():
    conn = brain_map.connect(":memory:")
    text = ex.explain("ex123456", entries=[_entry()], conn=conn)
    assert "NIFTY 50" in text and "iron_condor" in text
    assert "vix 14.2" in text and "downtrend" in text and "rsi 51.0" in text
    assert "vol_bridge overrides" in text
    assert "vix_regime" in text                      # evidence summary line
    assert "unresolved" in text                      # honest absence
    assert "evidence_snapshots" in text              # brain-map join line


def test_explain_pre_substrate_entry_shows_honest_gaps():
    conn = brain_map.connect(":memory:")
    text = ex.explain("ex123456", entries=[_entry(with_extras=False)],
                      conn=conn)
    assert "no receipt" in text and "no evidence snapshot" in text


def test_explain_resolved_entry_and_missing_id():
    conn = brain_map.connect(":memory:")
    e = _entry()
    e["outcome"] = {"resolution": "profit_take", "exit_date": "2026-07-15",
                    "pnl_rs": 2700.0, "r_multiple": 0.47, "verdict": "WIN"}
    text = ex.explain("ex123456", entries=[e], conn=conn)
    assert "profit_take" in text and "WIN" in text
    assert "No journal entry" in ex.explain("nope", entries=[e], conn=conn)


def test_recent_lists_newest_first():
    entries = [_entry(f"id{i:06d}", with_extras=False) for i in range(3)]
    entries[0]["outcome"] = {"resolution": "target_hit"}
    text = ex.recent(entries=entries, n=2)
    lines = text.splitlines()
    assert "id000002" in lines[1] and "id000001" in lines[2]
    assert "resolved" not in lines[1]
    assert ex.recent(entries=[]) == "(journal is empty)"


def test_run_headless_attaches_the_receipt():
    """The live wire: the same offline run_headless path from the evidence
    wiring test must now carry receipt fields too."""
    import tempfile
    from src import options_proposer as op
    from src import journal as journal_mod
    strikes = {}
    for k in range(23000, 27050, 50):
        strikes[f"{k}.000000"] = {
            "ce": {"last_price": max(5.0, (25000 - k) * 0.4 + 120)},
            "pe": {"last_price": max(5.0, (k - 25000) * 0.4 + 120)}}
    state = {"analysis": {"ticker": "NIFTY 50", "uptrend": False,
                          "fresh_cross": False, "rsi": 52.0,
                          "price": 25000.0},
             "vix": 14.5, "chain": {"last_price": 25000.0, "oc": strikes},
             "expiry": "2026-07-30",
             "book": {"cash": 1_000_000.0, "holdings": {}}}
    with tempfile.TemporaryDirectory() as tmp:
        original = journal_mod.JOURNAL_PATH
        journal_mod.JOURNAL_PATH = Path(tmp) / "journal.jsonl"
        try:
            result = op.run_headless("NIFTY 50", state=state)
        finally:
            journal_mod.JOURNAL_PATH = original
    if result["proposed"]:
        r = result["entry"]["receipt"]
        assert r["vix"] == 14.5 and r["book"] == "sandbox"
        assert r["analysis"]["rsi"] == 52.0


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

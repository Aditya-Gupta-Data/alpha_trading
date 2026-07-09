"""
Tests for the Phase 6 core loop: plan resolution -> post-mortem analyst
-> Brain Map write. A fake trade hits its target with every external
surface patched out (no Dhan prices, no Gemini call, no email, no real
journal/portfolio/brain_map.db files), proving the tracker records the
outcome + post-mortem into SQLite automatically and that every failure
mode degrades without blocking resolution.

Run either of these from the project folder:
    python tests/test_tracker_loop.py      (simple, no extra installs)
    python -m pytest tests/                (if you have pytest)
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import analyst
from src import brain_map
import src.plan_tracker as plan_tracker


FAKE_POST_MORTEM = {
    "variance_analysis": "Planned a Golden Cross swing to 110; it hit target in 1 day.",
    "unexpected_variables": "none observed",
    "future_guardrails": "Fresh crosses this clean can be trusted at 3R.",
}


class FakeJournal:
    def __init__(self, entries):
        self.entries = entries
        self.rewritten = None

    def read_all(self):
        return self.entries

    def rewrite_all(self, entries):
        self.rewritten = entries


def make_open_plan(short_id="pm123456"):
    """An approved BUY with a live 4B plan: entry 100, stop 97, target 110."""
    return {
        "short_id": short_id,
        "date": "2026-07-01", "action": "BUY", "ticker": "TCS.NS",
        "shares": 5, "price": 100.0, "signal": "Fresh Golden Cross",
        "decision": "approved", "why": "clean breakout",
        "pattern_tags": ["Golden Cross"],
        "plan": {"stop_loss": {"pct": 3.0, "price": 97.0},
                 "target": {"price": 110.0, "rr": 3.33}},
        "outcome": None,
    }


def run_tracker_offline(tmp, entries, post_mortem_fn=lambda plan, execution: dict(FAKE_POST_MORTEM),
                        brain_connect=None):
    """run_tracker with every external surface patched out. Returns
    (resolved_count, fake_journal, brain_db_path)."""
    db_path = Path(tmp) / "brain_map.db"
    fake_journal = FakeJournal(entries)
    plan_tracker.journal = fake_journal
    # Target 110 trades on 2026-07-02 (high 111) -> target_hit.
    plan_tracker._daily_bars = lambda ticker, start: [("2026-07-02", 99.0, 111.0, 110.5)]
    plan_tracker._close_paper_position = lambda entry, price, *args, **kwargs: True
    plan_tracker._brain_connect = brain_connect or (lambda: brain_map.connect(db_path))
    analyst.generate_post_mortem = post_mortem_fn
    resolved = plan_tracker.run_tracker(email=False)
    return resolved, fake_journal, db_path


def test_target_hit_triggers_post_mortem_into_brain_map():
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fake_journal, db_path = run_tracker_offline(tmp, [make_open_plan()])
        assert resolved == 1
        # The journal outcome is what Phase 4C always wrote:
        outcome = fake_journal.rewritten[0]["outcome"]
        assert outcome["resolution"] == "target_hit"
        assert outcome["r_multiple"] == 3.33
        # ...and the Brain Map now holds the outcome, keyed by short_id,
        # with the analyst's post-mortem attached:
        conn = brain_map.connect(db_path)
        row = conn.execute("SELECT * FROM outcomes").fetchone()
        assert row["journal_ref"] == "pm123456"
        assert row["result"] == "win"
        assert json.loads(row["post_mortem"]) == FAKE_POST_MORTEM
        # ...linked to the entry's pattern events, queryable immediately:
        stats = brain_map.query_similar_events(conn, ["golden_cross"])
        assert stats["count"] == 1 and stats["win_rate"] == 1.0
        assert brain_map.query_similar_events(conn, ["fresh_cross"])["count"] == 1
        conn.close()


def test_offline_analyst_still_records_the_outcome():
    with tempfile.TemporaryDirectory() as tmp:
        resolved, _, db_path = run_tracker_offline(
            tmp, [make_open_plan()], post_mortem_fn=lambda p, e: None)
        assert resolved == 1
        conn = brain_map.connect(db_path)
        row = conn.execute("SELECT journal_ref, post_mortem FROM outcomes").fetchone()
        assert row["journal_ref"] == "pm123456"
        assert row["post_mortem"] is None
        conn.close()


def test_analyst_exception_never_blocks_resolution():
    def exploding(plan, execution):
        raise RuntimeError("simulated analyst crash")
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fake_journal, _ = run_tracker_offline(
            tmp, [make_open_plan()], post_mortem_fn=exploding)
        assert resolved == 1
        assert fake_journal.rewritten[0]["outcome"]["resolution"] == "target_hit"


def test_unavailable_brain_map_never_blocks_resolution():
    def broken_connect():
        raise sqlite3.OperationalError("simulated: db locked")
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fake_journal, _ = run_tracker_offline(
            tmp, [make_open_plan()], brain_connect=broken_connect)
        assert resolved == 1
        assert fake_journal.rewritten[0]["outcome"]["resolution"] == "target_hit"


def test_post_mortem_payloads_capture_plan_and_execution():
    entry = make_open_plan()
    entry["outcome"] = {"resolution": "target_hit", "price": 110.0,
                        "exit_date": "2026-07-02", "pct": 10.0,
                        "r_multiple": 3.33, "days_in_trade": 1,
                        "pnl_rs": 50.0, "hypothetical": False,
                        "verdict": "WIN — target hit"}
    initial_plan, actual_execution = plan_tracker._post_mortem_payloads(entry)
    assert initial_plan["thesis_signal"] == "Fresh Golden Cross"
    assert initial_plan["user_reasoning"] == "clean breakout"
    assert initial_plan["plan"]["target"]["price"] == 110.0
    assert initial_plan["entry_price"] == 100.0
    assert actual_execution["trigger"] == "target_hit"
    assert actual_execution["exit_price"] == 110.0
    assert actual_execution["r_multiple"] == 3.33
    assert actual_execution["days_in_trade"] == 1


def test_record_outcome_backfills_post_mortem_on_existing_row():
    conn = brain_map.connect(":memory:")
    first = brain_map.record_outcome(conn, "ref1", "2026-07-02", "TCS.NS", r_multiple=2.0)
    again = brain_map.record_outcome(conn, "ref1", "2026-07-02", "TCS.NS",
                                     r_multiple=2.0, post_mortem=FAKE_POST_MORTEM)
    assert first == again
    row = conn.execute("SELECT post_mortem FROM outcomes").fetchone()
    assert json.loads(row["post_mortem"]) == FAKE_POST_MORTEM


def test_connect_upgrades_a_pre_post_mortem_database():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "old.db"
        old = sqlite3.connect(str(db_path))
        old.execute("CREATE TABLE outcomes ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "journal_ref TEXT NOT NULL UNIQUE, date TEXT NOT NULL, "
                    "ticker TEXT NOT NULL, archetype TEXT, r_multiple REAL, "
                    "result TEXT NOT NULL CHECK (result IN ('win','loss','scratch')))")
        old.commit()
        old.close()
        conn = brain_map.connect(db_path)  # must ALTER in the new column
        brain_map.record_outcome(conn, "ref1", "2026-07-02", "TCS.NS",
                                 r_multiple=1.0, post_mortem=FAKE_POST_MORTEM)
        row = conn.execute("SELECT post_mortem FROM outcomes").fetchone()
        assert json.loads(row["post_mortem"]) == FAKE_POST_MORTEM
        conn.close()


def test_coerce_post_mortem_enforces_the_schema():
    good = analyst._coerce_post_mortem({
        "variance_analysis": "planned vs actual",
        "unexpected_variables": "  volume   spike ",
        "future_guardrails": "watch gaps"})
    assert good == {"variance_analysis": "planned vs actual",
                    "unexpected_variables": "volume spike",
                    "future_guardrails": "watch gaps"}
    partial = analyst._coerce_post_mortem({"variance_analysis": "only this"})
    assert partial["variance_analysis"] == "only this"
    assert partial["unexpected_variables"] == "n/a"
    assert analyst._coerce_post_mortem("not json object") is None
    assert analyst._coerce_post_mortem({}) is None
    assert analyst._coerce_post_mortem({"variance_analysis": ""}) is None


def test_outcome_lines_survive_none_r_multiple_and_pct():
    """Regression (found live 2026-07-09): a hypothetical resolution with
    r_multiple=None crashed the digest formatting mid-sweep BEFORE the
    journal rewrite, so the same resolutions re-broadcast to Discord
    every hour. The digest lines must be total functions."""
    entry = {"decision": "rejected", "ticker": "MARUTI",
             "date": "2026-07-07", "price": 12000.0,
             "signal": "test", "why": "test",
             "outcome": {"resolution": "target_hit", "price": 12500.0,
                         "exit_date": "2026-07-08", "pnl_rs": 500.0,
                         "pct": None, "r_multiple": None,
                         "days_in_trade": 1, "position_closed": False,
                         "verdict": "MISSED GAIN"}}
    line = plan_tracker._outcome_line(entry)
    assert "n/a" in line and "MISSED GAIN" in line   # no raise, honest text

    spread_entry = {"decision": "approved", "ticker": "NIFTY BANK",
                    "date": "2026-07-07", "signal": "s", "why": "w",
                    "spread": {"strategy": "iron_condor",
                               "legs": [1, 2, 3, 4]},
                    "outcome": {"resolution": "profit_take",
                                "exit_date": "2026-07-08",
                                "pnl_rs": 900.0, "r_multiple": None,
                                "days_in_trade": 1, "frictions_rs": 10.0,
                                "slippage_rs": 5.0, "verdict": "WIN"}}
    line = plan_tracker._spread_outcome_line(spread_entry)
    assert "n/a" in line and "WIN" in line


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

"""
Tests for the journal-derived cooldown persistence (ledger Issue 8,
2026-07-09: a mid-session restart wiped the in-memory CooldownRegistry
and the new session immediately re-proposed both indices — live
positions doubled).

Offline — injected entry lists and clocks only; data/journal.jsonl is
never read or written except through mocks.

Run:
    python tests/test_cooldown_persistence.py
    pytest tests/test_cooldown_persistence.py -v
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import journal
from src.market_loop import IST, CooldownRegistry, run_market_loop

NOW = datetime(2026, 7, 10, 12, 40, tzinfo=IST)


def _entry(ticker: str, minutes_ago: float, decision="pending_approval"):
    created = NOW - timedelta(minutes=minutes_ago)
    return {"short_id": "x", "ticker": ticker, "decision": decision,
            "date": created.date().isoformat(),
            "created_at": created.isoformat(timespec="seconds")}


# ------------------------------------------------- journal timestamping

def test_journal_log_stamps_created_at(tmp_path=None):
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    with mock.patch.object(journal, "DATA_DIR", tmp), \
         mock.patch.object(journal, "JOURNAL_PATH", tmp / "journal.jsonl"):
        journal.log({"ticker": "NIFTY 50", "decision": "pending_approval"})
        line = journal.read_all()[0]
    assert "created_at" in line
    parsed = datetime.fromisoformat(line["created_at"])
    assert parsed.tzinfo is not None          # timezone-aware IST timestamp
    assert line["short_id"]                   # the existing safety net held


def test_journal_log_never_overwrites_a_caller_timestamp():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    with mock.patch.object(journal, "DATA_DIR", tmp), \
         mock.patch.object(journal, "JOURNAL_PATH", tmp / "journal.jsonl"):
        journal.log({"ticker": "X", "created_at": "2026-07-09T09:30:00+05:30"})
        assert journal.read_all()[0]["created_at"] == "2026-07-09T09:30:00+05:30"


# ------------------------------------------------- seeding the registry

def test_recent_proposal_rearms_the_cooldown():
    """The Issue 8 scenario: proposals fired ~25 minutes before a restart
    must still be cooling down in the NEW registry."""
    reg = CooldownRegistry(seconds=7200)
    armed = reg.seed_from_journal(
        ("NIFTY 50", "NIFTY BANK"), now=NOW,
        entries=[_entry("NIFTY 50", 25), _entry("NIFTY BANK", 24)])
    assert sorted(armed) == ["NIFTY 50", "NIFTY BANK"]
    assert not reg.ready("NIFTY 50", NOW)
    assert not reg.ready("NIFTY BANK", NOW)
    # ...and naturally expires when the original window would have:
    later = NOW + timedelta(hours=2)
    assert reg.ready("NIFTY 50", later)


def test_old_proposals_do_not_rearm():
    reg = CooldownRegistry(seconds=7200)
    armed = reg.seed_from_journal(
        ("NIFTY 50",), now=NOW, entries=[_entry("NIFTY 50", 121)])  # >2h ago
    assert armed == []
    assert reg.ready("NIFTY 50", NOW)


def test_rejected_and_approved_entries_both_count_as_fired_proposals():
    reg = CooldownRegistry(seconds=7200)
    armed = reg.seed_from_journal(
        ("NIFTY 50", "NIFTY BANK"), now=NOW,
        entries=[_entry("NIFTY 50", 30, decision="rejected"),
                 _entry("NIFTY BANK", 40, decision="approved")])
    assert sorted(armed) == ["NIFTY 50", "NIFTY BANK"]


def test_newest_entry_per_underlying_wins():
    reg = CooldownRegistry(seconds=7200)
    reg.seed_from_journal(
        ("NIFTY 50",), now=NOW,
        entries=[_entry("NIFTY 50", 110), _entry("NIFTY 50", 10)])
    # armed from the 10-minute-old entry: still cooling 100 minutes later
    assert not reg.ready("NIFTY 50", NOW + timedelta(minutes=100))
    assert reg.ready("NIFTY 50", NOW + timedelta(minutes=115))


def test_legacy_lines_and_garbage_timestamps_seed_nothing():
    reg = CooldownRegistry(seconds=7200)
    legacy = {"ticker": "NIFTY 50", "date": "2026-07-10"}   # pre-created_at
    junk = {"ticker": "NIFTY 50", "created_at": "not-a-timestamp"}
    other = _entry("TCS.NS", 5)                             # not an underlying
    armed = reg.seed_from_journal(("NIFTY 50",), now=NOW,
                                  entries=[legacy, junk, other])
    assert armed == []
    assert reg.ready("NIFTY 50", NOW)


def test_naive_timestamps_are_treated_as_ist():
    reg = CooldownRegistry(seconds=7200)
    naive = {"ticker": "NIFTY 50",
             "created_at": (NOW - timedelta(minutes=15))
             .replace(tzinfo=None).isoformat(timespec="seconds")}
    armed = reg.seed_from_journal(("NIFTY 50",), now=NOW, entries=[naive])
    assert armed == ["NIFTY 50"]


def test_unreadable_journal_fails_open():
    reg = CooldownRegistry(seconds=7200)
    with mock.patch("src.journal.read_all",
                    side_effect=RuntimeError("disk gone")):
        armed = reg.seed_from_journal(("NIFTY 50",), now=NOW)
    assert armed == []
    assert reg.ready("NIFTY 50", NOW)


# ------------------------------------- the loop seeds itself on startup

def test_run_market_loop_restores_cooldown_across_a_restart():
    """End-to-end restart simulation: with a 25-minute-old journaled
    proposal, a FRESH loop (no injected registry — exactly the restart
    path) must not re-propose that underlying on its first cycle."""
    proposals = []

    def fake_fetch(underlying):
        return {"analysis": {}, "vix": 14.0}

    def fake_propose(underlying, state):
        proposals.append(underlying)
        return {"proposed": True}

    async def one_cycle():
        with mock.patch("src.journal.read_all",
                        return_value=[_entry("NIFTY 50", 25)]), \
             mock.patch("src.market_loop.is_market_open", return_value=True):
            task = asyncio.get_event_loop().create_task(run_market_loop(
                underlyings=("NIFTY 50", "NIFTY BANK"),
                interval=0.01, cooldown=None,          # the restart path
                fetch_fn=fake_fetch, propose_fn=fake_propose,
                now_fn=lambda: NOW))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(one_cycle())
    # NIFTY BANK (no recent proposal) fires; NIFTY 50 stays cooled down.
    assert "NIFTY BANK" in proposals
    assert "NIFTY 50" not in proposals


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

"""
Discord daily budget (Directive 4, decision #84) — hermetic tests.

One gate at the one door: crashes always page, scheduled digests spend
the budget, the 2-hourly snapshot drops, everything else spools into the
digest queue and surfaces in the next EOD/CEO card's 📦 section. Run:
    python -m pytest tests/test_discord_budget.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.notifier as nt

IST = timezone(timedelta(hours=5, minutes=30))


def _paths(tmp):
    return Path(tmp) / "state.json", Path(tmp) / "queue.jsonl"


def _state(sent, date=None):
    return json.dumps({"date": date or datetime.now(IST).date().isoformat(),
                       "sent": sent})


def test_crash_always_pages_even_past_budget():
    with tempfile.TemporaryDirectory() as tmp:
        state, queue = _paths(tmp)
        state.write_text(_state(sent=5))              # budget spent
        v = nt.budget_gate({"event": "system_crash"}, state, queue, enabled=True)
        assert v == "send"


def test_scheduled_digests_spend_the_budget():
    with tempfile.TemporaryDirectory() as tmp:
        state, queue = _paths(tmp)
        assert nt.budget_gate({"event": "eod"}, state, queue, enabled=True) == "send"
        state.write_text(_state(sent=5))              # exhausted -> spool
        assert nt.budget_gate({"event": "eod"}, state, queue, enabled=True) == "spool"
        assert queue.exists()                          # queued, not lost


def test_snapshot_drops_and_signal_spools():
    with tempfile.TemporaryDirectory() as tmp:
        state, queue = _paths(tmp)
        assert nt.budget_gate({"event": "portfolio_report"}, state, queue, enabled=True) == "drop"
        assert not queue.exists()                      # dropped = no spool
        v = nt.budget_gate({"event": "equity_desk", "ticker": "EQUITY DESK",
                            "description": "BUY X\nmore"}, state, queue, enabled=True)
        assert v == "spool"
        [row] = [json.loads(l) for l in queue.read_text().splitlines()]
        assert row["event"] == "equity_desk"
        # Unknown events spool too — nothing signal-bearing dies silent.
        assert nt.budget_gate({"event": "totally_new_thing"}, state, queue, enabled=True) == "spool"


def test_budget_resets_on_a_new_ist_day():
    with tempfile.TemporaryDirectory() as tmp:
        state, queue = _paths(tmp)
        yesterday = (datetime.now(IST).date()
                     - timedelta(days=1)).isoformat()
        state.write_text(_state(sent=5, date=yesterday))
        assert nt.budget_gate({"event": "eod"}, state, queue, enabled=True) == "send"


def test_kill_switch_sends_everything():
    with tempfile.TemporaryDirectory() as tmp:
        state, queue = _paths(tmp)
        state.write_text(_state(sent=99))
        assert nt.budget_gate({"event": "anything"}, state, queue,
                              enabled=False) == "send"
        assert not queue.exists()              # disabled = nothing spools


def test_drain_renders_truncates_and_archives():
    with tempfile.TemporaryDirectory() as tmp:
        _, queue = _paths(tmp)
        assert nt.drain_digest_queue(queue) is None    # nothing waiting
        for i in range(15):
            nt._spool({"event": f"ev{i}",
                       "description": f"line one {i}\nline two"}, queue)
        out = nt.drain_digest_queue(queue, max_lines=12)
        assert "ev0: line one 0" in out and "line two" not in out
        assert "…and 3 more" in out
        assert queue.read_text() == ""                 # drained
        archived = (queue.parent / (queue.name + ".drained")).read_text()
        assert archived.count("\n") == 15              # nothing lost
        assert nt.drain_digest_queue(queue) is None    # idempotent


def test_eod_and_ceo_cards_carry_the_batched_section():
    from src import ceo_brief, eod_summary
    real_drain = nt.drain_digest_queue
    real_read = eod_summary._read_journal
    real_q = eod_summary.query_todays_resolutions
    try:
        nt.drain_digest_queue = lambda *a, **k: "12:01 · opened: BUY X"
        eod_summary._read_journal = lambda path=None: []
        eod_summary.query_todays_resolutions = lambda db_path=None: []
        card = eod_summary.build_eod_card()
        [f] = [f for f in card["fields"] if f["name"] == "📦 Batched signals"]
        assert "opened: BUY X" in f["value"]
        with tempfile.TemporaryDirectory() as tmp:
            brief = ceo_brief.build_brief_card(
                logs_dir=Path(tmp), state_path=Path(tmp) / "s.json",
                deploy_log_path=Path(tmp) / "d.jsonl",
                repo_root=Path(tmp))
        [f] = [f for f in brief["fields"]
               if f["name"] == "📦 Batched signals"]
        assert "opened: BUY X" in f["value"]
    finally:
        nt.drain_digest_queue = real_drain
        eod_summary._read_journal = real_read
        eod_summary.query_todays_resolutions = real_q


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_discord_budget.py")

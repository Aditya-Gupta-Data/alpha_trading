"""Ledger Issue 34 — the one-off PAPER_2L verdict repair (MANUAL OFFLINE TOOL).
Hermetic: ':memory:' DB, the journal seam patched, no file backup."""
import importlib.util
import json
from pathlib import Path

from src import brain_map, portfolio_manager as pm

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("repair34", ROOT / "scripts" / "repair_issue34_2l_verdicts.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)

REJ = {"status": "rejected", "lots": 0, "margin_rs": None,
       "reason": "sizing refused: SPAN margin Rs.37,835/lot exceeds liquid cash Rs.1,231"}


def _world():
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_paper_account(c, "PAPER_2L")
    pm.paper_request_entry(c, "PAPER_2L", "open1", 32442.0, lots=2, primary_lots=2)
    pm.paper_request_entry(c, "PAPER_2L", "done1", 37835.0, lots=1, primary_lots=1)
    pm.paper_release_margin(c, "PAPER_2L", "done1", 3338.6)          # settled off the primary
    pm.paper_request_entry(c, "PAPER_2L", "fine1", 17368.0, lots=1, primary_lots=3)
    entries = [
        {"short_id": "open1", "ticker": "NIFTY BANK", "accounts": {"PAPER_2L": dict(REJ)}},
        {"short_id": "done1", "ticker": "ICICIBANK.NS", "outcome": {"resolution": "ratchet_hit"},
         "accounts": {"PAPER_2L": dict(REJ)}},
        {"short_id": "fine1", "ticker": "NIFTY 50",
         "accounts": {"PAPER_2L": {"status": "approved", "lots": 1, "margin_rs": 17368.0}}},
        {"short_id": "nolock", "ticker": "TCS.NS", "accounts": {"PAPER_2L": dict(REJ)}},
    ]
    return c, entries


def test_plan_targets_only_held_but_rejected_rows_open_or_settled():
    c, entries = _world()
    p = repair.plan(c, entries)
    assert [(x["journal_ref"], x["lots"], x["margin_rs"], bool(x["released_at"])) for x in p] == [
        ("open1", 2, 32442.0, False), ("done1", 1, 37835.0, True)]
    assert p[0]["was"] == REJ                                  # archived verbatim
    c.close()


def test_apply_rewrites_the_verdict_logs_an_event_and_is_idempotent(monkeypatch):
    c, entries = _world()

    def update_entry(sid, fn):
        e = next((r for r in entries if r["short_id"] == sid), None)
        return e if e is not None and fn(e) else None
    monkeypatch.setattr(repair.journal, "update_entry", update_entry)
    done = repair.apply(c, repair.plan(c, entries), now="2026-09-28T22:30:00")
    assert done == ["open1", "done1"]
    v = entries[0]["accounts"]["PAPER_2L"]
    assert v["status"] == "approved" and v["lots"] == 2 and v["margin_rs"] == 32442.0
    assert v["repaired_from"] == REJ and v["repaired_at"] == "2026-09-28T22:30:00"
    assert entries[1]["outcome"] == {"resolution": "ratchet_hit"}          # settled row untouched otherwise
    assert entries[3]["accounts"]["PAPER_2L"] == REJ                        # no lock -> not touched
    ev = c.execute("SELECT journal_ref, detail FROM paper_account_events WHERE event_type = 'issue34_repair' "
                   "ORDER BY rowid").fetchall()
    assert [r[0] for r in ev] == ["open1", "done1"]
    assert json.loads(ev[1][1])["released_at"] and json.loads(ev[0][1])["was"] == "rejected"
    assert repair.plan(c, entries) == []                                     # second run: nothing
    c.close()

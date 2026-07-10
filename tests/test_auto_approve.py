"""
Tests for the PAPER_AUTO_APPROVE gate (Phase 4 scratchpad build): the
opt-in switch that lets headless proposals journal themselves as APPROVED
immediately, through the exact decide_pending path a human tap takes.

The contract under test:
  * DEFAULT IS OFF — with the env var absent, run_headless behaves
    byte-for-byte as before (pending_approval, human gate intact).
  * ON — the fresh pending entry is auto-approved via decide_pending
    (same margin gate, same journal rewrite, same broadcasts), the audit
    trail lands in `why`, and the result reports auto_approved=True.
  * a margin-blocked auto-approval leaves the entry PENDING and says so.

Offline — temp journal files, every network/margin seam mocked.

Run:
    python tests/test_auto_approve.py
    pytest tests/test_auto_approve.py -v
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import journal
from src import options_proposer as op


def _canned_proposal():
    return {
        "action": "SPREAD", "ticker": "NIFTY 50", "shares": 75, "price": 72.3,
        "signal": "bearish trend read — bear put spread", "view": "bearish",
        "vix": 14.2, "lots": 1,
        "spread": {"strategy": "bear_put_spread", "direction": "bearish",
                   "legs": [{"side": "BUY", "option_type": "PE",
                             "strike": 24150.0, "premium": 181.75},
                            {"side": "SELL", "option_type": "PE",
                             "strike": 23950.0, "premium": 109.45}],
                   "lot_size": 75, "lots": 1, "expiry": "2026-07-21",
                   "net_debit": 72.3, "net_credit": None,
                   "max_loss": 5422.5, "max_profit": 9577.5,
                   "spread_width": 200.0, "entry_spot": 24169.45,
                   "margin": {"total_margin": 20422.5}},
    }


def _headless(env: dict, gate=None, state=None):
    """Run one run_headless cycle in a temp journal with every seam
    mocked; returns (result, journal_entries, notes) where notes are the
    _notify_discord messages and captured action_notes."""
    tmp = Path(tempfile.mkdtemp())
    notes = {"discord": [], "action_notes": []}

    def fake_alert(p, action_note=""):
        notes["action_notes"].append(action_note)
        return "alert"

    gate = gate or mock.Mock(return_value=(True, "margin locked"))
    with mock.patch.object(journal, "DATA_DIR", tmp), \
         mock.patch.object(journal, "JOURNAL_PATH", tmp / "journal.jsonl"), \
         mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(op, "build_proposal",
                           return_value={"proposal": _canned_proposal(),
                                         "reason": "ok"}), \
         mock.patch.object(op, "_memory_context_for", return_value=""), \
         mock.patch.object(op, "_skeptic_note_for", return_value=""), \
         mock.patch.object(op, "_format_proposal_alert", fake_alert), \
         mock.patch.object(op, "_notify_discord",
                           side_effect=notes["discord"].append), \
         mock.patch("src.portfolio_manager.gate_headless_entry", gate), \
         mock.patch("src.portfolio_manager.release_entry",
                    return_value={"released": False}), \
         mock.patch("src.notifier.fire_broadcast") as broadcast:
        result = op.run_headless("NIFTY 50",
                                  state=state if state is not None else {})
        entries = journal.read_all()
    notes["broadcast"] = broadcast
    return result, entries, notes


def _clean_env():
    """Environment overlay guaranteeing the switch is ABSENT."""
    return {k: v for k, v in os.environ.items()
            if k != op.AUTO_APPROVE_ENV_KEY}


# ------------------------------------------------------------ flag parsing

def test_flag_parsing_default_off_and_explicit_values():
    for value, expected in (("1", True), ("true", True), ("YES", True),
                            (" TRUE ", True), ("", False), ("0", False),
                            ("false", False), ("off", False), ("no", False)):
        with mock.patch.dict(os.environ,
                             {op.AUTO_APPROVE_ENV_KEY: value}, clear=False):
            assert op.paper_auto_approve_enabled() is expected, value
    env = _clean_env()
    with mock.patch.dict(os.environ, env, clear=True):
        assert op.paper_auto_approve_enabled() is False


# ----------------------------------------------------- default (gate OFF)

def test_default_behavior_unchanged_entry_stays_pending():
    with mock.patch.dict(os.environ, _clean_env(), clear=True):
        result, entries, notes = _headless(env={})
    assert result["proposed"] is True
    assert result["auto_approved"] is False
    assert len(entries) == 1
    assert entries[0]["decision"] == "pending_approval"
    assert "PENDING_APPROVAL" in notes["action_notes"][0]
    assert not notes["broadcast"].called          # no "opened" card


# ---------------------------------------------------------- gate ON

def test_auto_approve_journals_approved_with_audit_trail():
    result, entries, notes = _headless(env={op.AUTO_APPROVE_ENV_KEY: "1"})
    assert result["proposed"] is True
    assert result["auto_approved"] is True
    assert result["reason"] == "ok (auto-approved)"
    assert len(entries) == 1
    assert entries[0]["decision"] == "approved"
    assert "auto-approved" in entries[0]["why"]           # the audit trail
    assert "PAPER_AUTO_APPROVE" in entries[0]["why"]
    # the alert told the truth about what was going to happen
    assert "APPROVED automatically" in notes["action_notes"][0]
    # the same "opened" broadcast a human approval fires
    assert notes["broadcast"].called
    assert notes["broadcast"].call_args[0][0]["event"] == "opened"
    # and the human-facing decision note still went out (audit visibility)
    assert any("APPROVED" in m for m in notes["discord"])


def test_margin_blocked_auto_approval_leaves_the_entry_pending():
    # First gate call (proposal time) allows; second (approval time) blocks.
    gate = mock.Mock(side_effect=[(True, "margin locked"),
                                  (False, "margin exhaustion: pool dry")])
    result, entries, notes = _headless(env={op.AUTO_APPROVE_ENV_KEY: "1"},
                                       gate=gate)
    assert result["proposed"] is True
    assert result["auto_approved"] is False
    assert "auto-approval declined" in result["reason"]
    assert "margin exhaustion" in result["reason"]
    assert entries[0]["decision"] == "pending_approval"   # human can decide
    assert not notes["broadcast"].called                  # nothing opened


def test_auto_approve_flows_through_decide_pending_not_a_shortcut():
    """The switch must reuse the human path (margin gate, rewrite,
    confirmations) — not write 'approved' directly into the entry."""
    with mock.patch.object(op, "decide_pending",
                           wraps=op.decide_pending) as dp:
        result, entries, _ = _headless(env={op.AUTO_APPROVE_ENV_KEY: "1"})
    assert dp.called
    assert dp.call_args[0][0] == entries[0]["short_id"]
    assert dp.call_args[1]["approve"] is True
    assert result["auto_approved"] is True


# ------------------------------------------- injected book (sandbox) guard

def test_injected_book_is_never_auto_approved():
    """A sandboxed (simulator / what-if) run is its own capital world:
    even with the switch ON, its proposals stay PENDING — decide_pending's
    margin gate only knows the REAL account and must never be reached
    from a run that was promised isolation."""
    gate = mock.Mock(return_value=(True, "margin locked"))
    result, entries, notes = _headless(env={op.AUTO_APPROVE_ENV_KEY: "1"},
                                       gate=gate,
                                       state={"book": {"sandbox": True}})
    assert result["proposed"] is True
    assert result["auto_approved"] is False
    assert entries[0].get("decision") != "approved"
    gate.assert_not_called()                 # the real pool stays untouched
    assert "PENDING_APPROVAL" in notes["action_notes"][0]


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

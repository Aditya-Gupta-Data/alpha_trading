"""Cross-process rate gate for dhan_client._throttle (DH-905 fix, 2026-07-22).

Offline, no network. A fake clock makes the 1.1s pacing instant and
deterministic: time.sleep records its delay and advances the clock instead of
blocking, so we assert the SPACING the gate would enforce without waiting for it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dhan_client as dc


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """Point the gate at a temp file with a controllable clock + recording sleep."""
    monkeypatch.setattr(dc, "_THROTTLE_FILE", tmp_path / ".dhan_throttle")
    monkeypatch.setattr(dc, "_RATE_PAUSE", 1.1)
    monkeypatch.setattr(dc, "_CHAIN_THROTTLE_FILE", tmp_path / ".dhan_chain_throttle")
    monkeypatch.setattr(dc, "_CHAIN_PAUSE", 3.5)
    clock = {"t": 1000.0}
    sleeps = []

    def _sleep(d):
        sleeps.append(d)
        clock["t"] += d           # slept time really elapses on the fake clock

    monkeypatch.setattr(dc.time, "time", lambda: clock["t"])
    monkeypatch.setattr(dc.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(dc.time, "sleep", _sleep)
    return clock, sleeps


def test_first_call_on_idle_gate_does_not_wait(gate):
    _, sleeps = gate
    dc._throttle()
    assert sleeps == []            # gate idle -> go immediately


def test_second_immediate_call_waits_one_pause(gate):
    _, sleeps = gate
    dc._throttle()                 # reserves the current slot
    dc._throttle()                 # same instant -> must wait ~_RATE_PAUSE
    assert sleeps and abs(sleeps[-1] - dc._RATE_PAUSE) < 1e-6


def test_calls_are_spaced_across_processes_sharing_the_file(gate):
    # Every _throttle() against the SAME file is a distinct "process"; each must
    # reserve a slot >= one pause after the last, no matter who called.
    clock, _ = gate
    for _ in range(4):
        dc._throttle()
    slot = float(dc._THROTTLE_FILE.read_text())
    assert slot >= 1000.0 + 3 * dc._RATE_PAUSE - 1e-6


def test_corrupt_file_self_heals_and_never_raises(gate):
    clock, sleeps = gate
    dc._THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    dc._THROTTLE_FILE.write_text("not-a-number")
    dc._throttle()                 # must not raise
    assert float(dc._THROTTLE_FILE.read_text()) >= clock["t"] - 1e-6


def test_absurd_future_slot_is_clamped_not_obeyed(gate):
    _, sleeps = gate
    dc._THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    dc._THROTTLE_FILE.write_text("999999999.0")   # a corrupt far-future slot
    dc._throttle()
    # self-heal: never sleep more than one pause, never honor the bogus slot
    assert not sleeps or sleeps[-1] <= dc._RATE_PAUSE + 1e-6


def test_fail_open_to_per_process_when_no_fcntl(gate, monkeypatch):
    # On a host without fcntl the gate degrades to the original per-process pace.
    _, sleeps = gate
    monkeypatch.setattr(dc, "fcntl", None)
    monkeypatch.setattr(dc, "_last_api_call", 0.0)
    dc._throttle()                 # first call: monotonic ~1000, no wait
    dc._throttle()                 # immediate second: waits one pause
    assert sleeps and abs(sleeps[-1] - dc._RATE_PAUSE) < 1e-6


# ------------------------------------------------- the chain lane (2026-10-01)

def test_two_chain_calls_are_spaced_one_chain_pause_apart(gate):
    # the 10-01 collision: the proposer's chain fetch, then the live arm's
    # re-quote of the same chain moments later — the second must wait the
    # chain endpoint's gap, not just the 1.1 s general gap.
    clock, sleeps = gate
    dc._throttle(chain=True)
    first = clock["t"]
    dc._throttle(chain=True)
    assert clock["t"] - first >= dc._CHAIN_PAUSE - 1e-6
    assert sleeps and abs(sleeps[-1] - dc._CHAIN_PAUSE) < 1e-6


def test_chain_lane_spacing_holds_across_processes_sharing_the_files(gate):
    clock, _ = gate
    sent = []
    for _ in range(3):
        dc._throttle(chain=True)          # each call = a different process
        sent.append(clock["t"])
    assert all(b - a >= dc._CHAIN_PAUSE - 1e-6 for a, b in zip(sent, sent[1:]))


def test_a_general_call_never_waits_for_the_chain_lane_but_keeps_its_own_gap(gate):
    clock, sleeps = gate
    dc._throttle(chain=True)
    dc._throttle()                        # a quote right after a chain call
    assert abs(sleeps[-1] - dc._RATE_PAUSE) < 1e-6
    t = clock["t"]
    dc._throttle(chain=True)              # the next chain call: chain gap from the LAST chain call
    assert clock["t"] - (t - dc._RATE_PAUSE) >= dc._CHAIN_PAUSE - 1e-6


def test_a_chain_call_keeps_the_general_gap_from_a_quote(gate):
    clock, sleeps = gate
    dc._throttle()                        # a quote reserved the current slot
    dc._throttle(chain=True)              # chain lane idle -> only the general gap
    assert abs(sleeps[-1] - dc._RATE_PAUSE) < 1e-6
    # and the general gate now holds the chain call's slot
    assert float(dc._THROTTLE_FILE.read_text()) == pytest.approx(clock["t"])


def test_corrupt_chain_lane_file_self_heals(gate):
    clock, sleeps = gate
    dc._CHAIN_THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    dc._CHAIN_THROTTLE_FILE.write_text("999999999.0")
    dc._throttle(chain=True)              # must not raise, must not obey the bogus slot
    assert not sleeps or sleeps[-1] <= dc._CHAIN_PAUSE + 1e-6


def test_chain_lane_fails_open_to_per_process_pacing(gate, monkeypatch):
    _, sleeps = gate
    monkeypatch.setattr(dc, "fcntl", None)
    monkeypatch.setattr(dc, "_last_api_call", 0.0)
    monkeypatch.setattr(dc, "_last_chain_call", 0.0)
    dc._throttle(chain=True)
    dc._throttle(chain=True)
    assert sleeps and abs(sleeps[-1] - dc._CHAIN_PAUSE) < 1e-6


def test_the_proposer_then_live_requote_sequence_goes_out_a_chain_gap_apart(gate, monkeypatch):
    # end to end through the one door: two get_option_chain calls on the
    # same instrument reach the SDK >= _CHAIN_PAUSE apart on the clock.
    clock, _ = gate
    sent = []

    class _Client:
        def option_chain(self, sid, seg, expiry):
            sent.append(clock["t"])
            return {"status": "success", "data": {"data": {"last_price": 1.0, "oc": {"100.000000": {}}}}}

    monkeypatch.setattr(dc, "_get_client", lambda: _Client())
    monkeypatch.setattr(dc, "_resolve", lambda t: {"id": "4963", "seg": "NSE_EQ"})
    assert dc.get_option_chain("ICICIBANK.NS", "2026-10-27")["oc"]
    assert dc.get_option_chain("ICICIBANK.NS", "2026-10-27")["oc"]
    assert len(sent) == 2 and sent[1] - sent[0] >= dc._CHAIN_PAUSE - 1e-6

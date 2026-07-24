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

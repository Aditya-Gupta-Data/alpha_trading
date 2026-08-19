"""
tests/conftest.py — the suite's hermeticity floor (RULE 6).

Created 2026-08-19 after `test_pm_run_headless_silently_rejects_when_the_
gate_says_no` failed exactly once on the VM mid-session and then passed
five times running. The cause was not the test: it was reading TWO pieces
of live production state that only exist on the trading box —

  1. `PAPER_AUTO_APPROVE=1` from the VM's real environment/.env, which
     flipped `run_headless` onto its auto-approve branch, and
  2. the real `data/human_pulse.json`, whose engagement tripwire was in a
     tripped-but-not-yet-alerted episode, so the branch fired an extra
     🛑 unsupervised card through the stubbed notifier and the "exactly
     one Discord message" assertion saw two.

Worse than the red: `should_alert_once()` stamps `alerted_at`, so the
failing run CONSUMED the owner's one-per-episode card and the next run
was green. A self-erasing failure that also suppressed a real alert.

These fixtures are autouse and deliberately blunt. A test that wants
either switch must set it itself (`mock.patch.dict(os.environ, ...)` or
an explicit `path=`), which is how the intent becomes visible in the test
instead of inherited from whatever box the suite happens to run on.
"""
import os

import pytest

# Ambient switches that change ENGINE BEHAVIOUR and exist on the VM.
# Cleared for every test; a test that needs one sets it explicitly.
_AMBIENT_ENGINE_SWITCHES = ("PAPER_AUTO_APPROVE",)


@pytest.fixture(autouse=True)
def _no_ambient_engine_switches(monkeypatch):
    """The suite must behave identically on the Mac and on the VM."""
    for key in _AMBIENT_ENGINE_SWITCHES:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolated_human_pulse(monkeypatch, tmp_path):
    """Point the engagement tripwire at a per-test temp file. `touch()`
    and `should_alert_once()` are muzzled under pytest on the default
    path, but `auto_approve_tripped()` READS it — and a read of live
    supervision state is what made the VM failure order-dependent."""
    from src import human_pulse
    monkeypatch.setattr(human_pulse, "PULSE_PATH",
                        tmp_path / "human_pulse.json")

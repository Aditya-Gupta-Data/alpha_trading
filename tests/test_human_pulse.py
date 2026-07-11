"""
Tests for the engagement tripwire (Phase 3, P3-3): full paper autonomy
pauses when the human goes absent, resumes on any human action. Offline.

Run either of these from the project folder:
    python tests/test_human_pulse.py
    python -m pytest tests/test_human_pulse.py
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import human_pulse as hp


def test_trading_days_skip_weekends():
    from datetime import date
    # Fri 2026-07-10 -> Mon 2026-07-13 is ONE trading day.
    assert hp.trading_days_between(date(2026, 7, 10), date(2026, 7, 13)) == 1
    assert hp.trading_days_between(date(2026, 7, 6), date(2026, 7, 10)) == 4
    assert hp.trading_days_between(date(2026, 7, 10), date(2026, 7, 10)) == 0


def test_first_run_seeds_and_does_not_trip():
    with tempfile.TemporaryDirectory() as tmp:
        pulse = Path(tmp) / "pulse.json"
        assert hp.auto_approve_tripped(path=pulse,
                                       now=datetime(2026, 7, 10, 10)) is False
        assert pulse.exists()                      # seeded "seen now"
        # Immediately after: still armed.
        assert hp.auto_approve_tripped(path=pulse,
                                       now=datetime(2026, 7, 10, 15)) is False


def test_trips_after_threshold_and_rearms_on_touch():
    with tempfile.TemporaryDirectory() as tmp:
        pulse = Path(tmp) / "pulse.json"
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"auto_approve_unsupervised_days": 3}))
        hp.touch("human", path=pulse, now=datetime(2026, 7, 6, 10))  # Monday
        # Thu = 3 trading days since Monday -> NOT tripped (needs > 3).
        assert hp.auto_approve_tripped(path=pulse, config_path=cfg,
                                       now=datetime(2026, 7, 9, 10)) is False
        # Friday = 4 trading days -> TRIPPED.
        assert hp.auto_approve_tripped(path=pulse, config_path=cfg,
                                       now=datetime(2026, 7, 10, 10)) is True
        # The weekend doesn't add trading days: Sunday still counts 4.
        assert hp.auto_approve_tripped(path=pulse, config_path=cfg,
                                       now=datetime(2026, 7, 12, 10)) is True
        # One human action re-arms instantly.
        hp.touch("decide_pending", path=pulse, now=datetime(2026, 7, 12, 11))
        assert hp.auto_approve_tripped(path=pulse, config_path=cfg,
                                       now=datetime(2026, 7, 13, 10)) is False


def test_alert_fires_exactly_once_per_pause_episode():
    with tempfile.TemporaryDirectory() as tmp:
        pulse = Path(tmp) / "pulse.json"
        hp.touch("human", path=pulse, now=datetime(2026, 7, 1, 10))
        assert hp.should_alert_once(path=pulse) is True
        assert hp.should_alert_once(path=pulse) is False    # same episode
        # A human action clears the episode -> a future pause alerts again.
        hp.touch("human", path=pulse, now=datetime(2026, 7, 20, 10))
        assert hp.should_alert_once(path=pulse) is True


def test_corrupt_state_reseeds_toward_autonomy_never_pauses():
    with tempfile.TemporaryDirectory() as tmp:
        pulse = Path(tmp) / "pulse.json"
        pulse.write_text("{broken")
        assert hp.auto_approve_tripped(path=pulse,
                                       now=datetime(2026, 7, 10, 10)) is False
        assert json.loads(pulse.read_text())["source"] == "seeded"


def test_card_carries_the_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"auto_approve_unsupervised_days": 5}))
        card = hp.unsupervised_card(config_path=cfg)
        assert "5 trading day(s)" in card and "PENDING_APPROVAL" in card


def test_touch_without_explicit_path_is_muzzled_under_pytest():
    import os
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return   # only meaningful under pytest; the direct runner WOULD write
    before = hp.PULSE_PATH.exists()
    hp.touch("human")                                # default path -> muzzled
    assert hp.PULSE_PATH.exists() == before          # nothing new appeared


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

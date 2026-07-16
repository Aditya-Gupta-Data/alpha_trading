"""
Tests for the market loop (src/market_loop.py) and the proposer's
headless mode: market-hours gating, the 2h per-index cool-down, the
fetch_market_state injection seam, and PENDING_APPROVAL journaling with
no terminal pause.

100% offline — time, market state, the proposer, Discord, and the
journal are all mocked/injected.

Run either of these from the project folder:
    python tests/test_market_loop.py      (simple, no extra installs)
    python -m pytest tests/                (if you have pytest)
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import market_loop as ml
from src import options_proposer as op


def ist(hour, minute, day=6):
    """An IST datetime on July `day` 2026 (July 6 2026 is a Monday)."""
    return datetime(2026, 7, day, hour, minute, tzinfo=ml.IST)


# --------------------------------------------------------- market hours

def test_market_hours_gating():
    assert ml.is_market_open(ist(9, 14)) is False    # pre-open
    assert ml.is_market_open(ist(9, 15)) is True     # opening bell
    assert ml.is_market_open(ist(12, 0)) is True
    assert ml.is_market_open(ist(15, 30)) is True    # closing minute
    assert ml.is_market_open(ist(15, 31)) is False   # post-close
    assert ml.is_market_open(ist(12, 0, day=4)) is False   # Sat 2026-07-04
    assert ml.is_market_open(ist(12, 0, day=5)) is False   # Sun 2026-07-05


# ------------------------------------------------------------- cooldown

def test_cooldown_registry_math():
    cd = ml.CooldownRegistry(seconds=7200)
    t0 = ist(10, 0)
    assert cd.ready("NIFTY 50", t0)                  # never armed -> ready
    cd.arm("NIFTY 50", t0)
    assert not cd.ready("NIFTY 50", t0 + timedelta(minutes=15))
    assert not cd.ready("NIFTY 50", t0 + timedelta(hours=1, minutes=59))
    assert cd.ready("NIFTY 50", t0 + timedelta(hours=2))   # exactly 2h -> ready
    # Keys are independent:
    assert cd.ready("NIFTY BANK", t0 + timedelta(minutes=1))


# ------------------------------------------------------------ loop runs

def run_cycles(now_values, fetch_fn, propose_fn, cooldown=None,
               underlyings=("NIFTY 50",)):
    """Drive run_market_loop through len(now_values) cycles: each sleep
    advances to the next now; the last sleep cancels the loop."""
    # Always inject a cool-down. Left as None, run_market_loop takes its
    # "seed the cooldown from the journal" branch, which reads the REAL
    # on-disk data/journal.jsonl — breaking this module's promise that the
    # journal is mocked/injected. A live proposal timestamped AFTER these
    # fake `now`s then makes the seed compute a NEGATIVE elapsed time, read
    # it as "still cooling down", and silence the proposal (real 2026-07-13
    # NIFTY 50 lines vs. this suite's July-6 clock did exactly that). The
    # seeding path itself is covered hermetically in test_cooldown_persistence.
    if cooldown is None:
        cooldown = ml.CooldownRegistry()
    times = list(now_values)
    idx = {"i": 0}

    def fake_now():
        return times[min(idx["i"], len(times) - 1)]

    async def fake_sleep(seconds):
        idx["i"] += 1
        if idx["i"] >= len(times):
            raise asyncio.CancelledError()

    with mock.patch("asyncio.sleep", side_effect=fake_sleep):
        try:
            asyncio.run(ml.run_market_loop(
                underlyings=underlyings, interval=900, cooldown=cooldown,
                fetch_fn=fetch_fn, propose_fn=propose_fn, now_fn=fake_now))
        except asyncio.CancelledError:
            pass


def test_favorable_state_triggers_headless_proposal_once():
    fetch = mock.Mock(return_value={"vix": 13.0})
    propose = mock.Mock(return_value={"proposed": True, "reason": "ok"})
    cd = ml.CooldownRegistry(seconds=7200)
    # Two in-hours cycles 15 minutes apart — the second must be silenced
    # by the cool-down armed by the first:
    run_cycles([ist(10, 0), ist(10, 15)], fetch, propose, cooldown=cd)
    assert propose.call_count == 1
    propose.assert_called_once_with("NIFTY 50", {"vix": 13.0})
    assert not cd.ready("NIFTY 50", ist(10, 15))


def test_cooldown_expires_and_the_loop_proposes_again():
    fetch = mock.Mock(return_value={"vix": 13.0})
    propose = mock.Mock(return_value={"proposed": True, "reason": "ok"})
    cd = ml.CooldownRegistry(seconds=7200)
    # 10:00 (fires), 10:15 (cooled), 12:01 (2h+ later -> fires again):
    run_cycles([ist(10, 0), ist(10, 15), ist(12, 1)], fetch, propose, cooldown=cd)
    assert propose.call_count == 2


def test_no_proposal_does_not_arm_the_cooldown():
    fetch = mock.Mock(return_value={"vix": 18.0})
    propose = mock.Mock(return_value={"proposed": False,
                                      "reason": "range-bound blocked"})
    cd = ml.CooldownRegistry(seconds=7200)
    run_cycles([ist(10, 0), ist(10, 15)], fetch, propose, cooldown=cd)
    # Blocked cycles keep trying every interval — no cool-down burned:
    assert propose.call_count == 2
    assert cd.ready("NIFTY 50", ist(10, 15))


def test_closed_market_never_fetches_or_proposes():
    fetch = mock.Mock()
    propose = mock.Mock()
    run_cycles([ist(8, 0), ist(16, 0), ist(12, 0, day=4)], fetch, propose)
    fetch.assert_not_called()
    propose.assert_not_called()


def test_missing_state_and_crashes_never_kill_the_loop():
    # Cycle 1: fetch returns None (no state). Cycle 2: fetch raises.
    # Cycle 3: healthy -> proposal fires. The loop survives all three.
    fetch = mock.Mock(side_effect=[None, RuntimeError("dhan down"),
                                   {"vix": 13.0}])
    propose = mock.Mock(return_value={"proposed": True, "reason": "ok"})
    run_cycles([ist(10, 0), ist(10, 15), ist(10, 30)], fetch, propose)
    assert propose.call_count == 1


def test_multiple_underlyings_have_independent_cooldowns():
    fetch = mock.Mock(return_value={"vix": 13.0})
    # NIFTY 50 proposes; NIFTY BANK is blocked this cycle:
    propose = mock.Mock(side_effect=[
        {"proposed": True, "reason": "ok"},        # NIFTY 50 @ 10:00
        {"proposed": False, "reason": "blocked"},  # NIFTY BANK @ 10:00
        {"proposed": True, "reason": "ok"},        # NIFTY BANK @ 10:15
    ])
    cd = ml.CooldownRegistry(seconds=7200)
    run_cycles([ist(10, 0), ist(10, 15)], fetch, propose, cooldown=cd,
               underlyings=("NIFTY 50", "NIFTY BANK"))
    # 10:15 cycle: NIFTY 50 cooled down (skipped), NIFTY BANK retried:
    assert propose.call_count == 3
    assert propose.call_args_list[2].args[0] == "NIFTY BANK"


# ------------------------------------------------------- headless mode

def make_analysis(rsi=55.0):
    return {"ticker": "NIFTY 50", "uptrend": True, "fresh_cross": False,
            "rsi": rsi, "price": 25000.0}


def make_chain(spot=25000.0, step=50.0, span=20, base_premium=200.0):
    oc = {}
    for i in range(-span, span + 1):
        strike = spot + i * step
        ce = max(5.0, base_premium - i * (base_premium / span) * 0.9)
        pe = max(5.0, base_premium + i * (base_premium / span) * 0.9)
        oc[f"{strike:.6f}"] = {"ce": {"last_price": round(ce, 2)},
                               "pe": {"last_price": round(pe, 2)}}
    return {"last_price": spot, "oc": oc}


HEADLESS_STATE = {
    "analysis": make_analysis(), "vix": 13.0, "expiry": "2026-07-30",
    "chain": make_chain(), "book": {"cash": 2_000_000.0, "holdings": {}},
    "prices": {},
}


def test_run_headless_journals_pending_and_never_prompts():
    with mock.patch.object(op.journal, "log") as mock_log, \
         mock.patch.object(op, "_notify_discord", return_value=True) as notif, \
         mock.patch("builtins.input",
                    side_effect=AssertionError("headless must never prompt")):
        result = op.run_headless("NIFTY 50", state=dict(HEADLESS_STATE))

    assert result["proposed"] is True
    entry = mock_log.call_args.args[0]
    assert entry["decision"] == "pending_approval"
    assert entry["action"] == "SPREAD" and entry["spread"]["lots"] >= 1
    assert "headless" in entry["why"]
    # The rich alert fired with the headless action note:
    alert = notif.call_args.args[0]
    assert "🚨 **PROPOSAL ALERT" in alert
    assert "PENDING_APPROVAL" in alert
    assert result["entry"]["short_id"]


def test_run_headless_returns_reason_without_journaling_when_blocked():
    blocked_state = dict(HEADLESS_STATE, vix=18.5)  # condor VIX-gated
    with mock.patch.object(op.journal, "log") as mock_log, \
         mock.patch.object(op, "_notify_discord") as notif:
        result = op.run_headless("NIFTY 50", state=blocked_state)
    assert result["proposed"] is False and "blocked" in result["reason"]
    mock_log.assert_not_called()
    notif.assert_not_called()  # nothing to alert — no Discord spam


def test_run_headless_survives_discord_down():
    with mock.patch.object(op.journal, "log") as mock_log, \
         mock.patch("src.notifier.send_discord_message",
                    side_effect=RuntimeError("webhook exploded")):
        result = op.run_headless("NIFTY 50", state=dict(HEADLESS_STATE))
    assert result["proposed"] is True     # journaled despite Discord failure
    mock_log.assert_called_once()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}  {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

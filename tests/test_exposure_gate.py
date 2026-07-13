"""
Tests for decision #68: the exposure gate (one open spread per
underlying+direction, fail-open, sandbox-exempt) and the trend-flip
exit advisory (advisory-only Discord card when the binary trend read
turns against open directional positions).

Offline — temp journals/ledgers, every network seam mocked, no Dhan,
no Discord (and webhooks are muzzled under pytest anyway).

Run:
    pytest tests/test_exposure_gate.py -v
"""

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import exposure_gate as eg
from src import journal
from src import options_proposer as op
from src import live_bridge as lb


# --------------------------------------------------------------- fixtures

def _open_entry(ticker="NIFTY 50", strategy="bear_put_spread",
                direction="bearish", decision="approved", outcome=None,
                short_id="ab12cd34", expiry="2026-07-21",
                opened="2026-07-09"):
    """A journal entry that positions.active_positions treats as OPEN
    (approved + unresolved + trackable spread), in the REAL journal
    shape — direction stamped inside the spread block."""
    return {
        "short_id": short_id, "date": opened, "action": "SPREAD",
        "ticker": ticker, "decision": decision, "outcome": outcome,
        "signal": "test", "why": "test",
        "spread": {"strategy": strategy, "direction": direction,
                   "legs": [{"side": "BUY", "option_type": "PE",
                             "strike": 24050.0, "premium": 207.8},
                            {"side": "SELL", "option_type": "PE",
                             "strike": 23850.0, "premium": 132.25}],
                   "lot_size": 75, "lots": 1, "expiry": expiry,
                   "net_debit": 75.55, "net_credit": None,
                   "max_loss": 5666.25, "max_profit": 9333.75,
                   "spread_width": 200.0, "entry_spot": 24045.0,
                   "margin": {"total_margin": 20666.25}},
    }


def _proposal(ticker="NIFTY 50", strategy="bear_put_spread",
              direction="bearish", view="bearish"):
    """REAL proposal shape: direction/strategy live inside `spread`,
    never at the top level (options_proposer.build_proposal)."""
    return {
        "action": "SPREAD", "ticker": ticker, "shares": 75, "price": 75.55,
        "signal": "test", "view": view, "vix": 14.0, "lots": 1,
        "spread": {"strategy": strategy, "direction": direction,
                   "legs": [{"side": "BUY", "option_type": "PE",
                             "strike": 24050.0, "premium": 207.8}],
                   "lot_size": 75, "lots": 1, "expiry": "2026-07-28",
                   "net_debit": 75.55, "max_loss": 5666.25,
                   "max_profit": 9333.75,
                   "margin": {"total_margin": 20666.25}},
    }


class _TempLedger:
    """Context manager pointing the gate's block ledger at a temp file."""

    def __enter__(self):
        self._dir = tempfile.mkdtemp()
        self._patch = mock.patch.object(
            eg, "LEDGER_PATH", Path(self._dir) / "exposure_blocks.jsonl")
        return self._patch.__enter__()

    def __exit__(self, *a):
        self._patch.__exit__(*a)


# ---------------------------------------------------------- gate: verdicts

def test_duplicate_same_underlying_same_direction_blocked():
    entries = [_open_entry()]
    with _TempLedger():
        allowed, reason = eg.gate_entry(_proposal(), entries=entries)
    assert allowed is False
    assert "ab12cd34" in reason
    assert "decision #68" in reason


def test_different_direction_same_underlying_allowed():
    entries = [_open_entry(direction="bearish")]
    with _TempLedger():
        allowed, _ = eg.gate_entry(
            _proposal(strategy="bull_call_spread", direction="bullish",
                      view="bullish"), entries=entries)
    assert allowed is True


def test_different_underlying_same_direction_allowed():
    """1 bearish NIFTY + 1 bearish BANKNIFTY may coexist — the cap is per
    underlying+direction, not per direction."""
    entries = [_open_entry(ticker="NIFTY 50")]
    with _TempLedger():
        allowed, _ = eg.gate_entry(
            _proposal(ticker="NIFTY BANK"), entries=entries)
    assert allowed is True


def test_neutral_is_its_own_slot():
    condor = _open_entry(strategy="iron_condor", direction="neutral")
    with _TempLedger():
        # an open condor doesn't block a directional spread...
        allowed, _ = eg.gate_entry(_proposal(), entries=[condor])
        assert allowed is True
        # ...but does block a second neutral structure on the same index
        allowed, _ = eg.gate_entry(
            _proposal(strategy="iron_butterfly", direction="neutral",
                      view="neutral"), entries=[condor])
        assert allowed is False


def test_pending_rejected_resolved_never_block():
    entries = [
        _open_entry(decision="pending_approval", short_id="p1"),
        _open_entry(decision="rejected", short_id="p2"),
        _open_entry(decision="approved", outcome={"resolution": "profit_take"},
                    short_id="p3"),
    ]
    with _TempLedger():
        allowed, _ = eg.gate_entry(_proposal(), entries=entries)
    assert allowed is True


def test_legacy_entry_without_direction_stamp_still_blocks():
    """Pre-stamp journal lines fall back to the strategy-name map."""
    e = _open_entry()
    del e["spread"]["direction"]
    with _TempLedger():
        allowed, _ = eg.gate_entry(_proposal(), entries=[e])
    assert allowed is False


def test_fail_open_on_positions_error():
    with mock.patch("src.positions.active_positions",
                    side_effect=RuntimeError("journal unreadable")):
        allowed, reason = eg.gate_entry(_proposal())
    assert allowed is True
    assert "exposure gate unavailable" in reason


def test_unclassifiable_proposal_never_blocked():
    p = _proposal()
    p["spread"] = {"strategy": "calendar_spread"}   # unknown structure
    p["view"] = "sideways-ish"                       # not a direction either
    with _TempLedger():
        allowed, reason = eg.gate_entry(p, entries=[_open_entry()])
    assert allowed is True
    assert "unclassifiable" in reason


# ----------------------------------------------------- gate: anti-drift map

def test_direction_map_agrees_with_every_constructor():
    """The fallback map must agree with strategy.py's stamped direction —
    built through the REAL constructors so the two can never drift."""
    from src.strategy import StrategyConstructor
    sc = StrategyConstructor(vix=14.0, lot_size=75)
    built = {
        "bull_call_spread": sc.construct_bull_call_spread(
            24000, 24200, 180.0, 90.0),
        "bear_put_spread": sc.construct_bear_put_spread(
            24200, 24000, 180.0, 90.0),
        "iron_condor": sc.construct_iron_condor(
            23800, 24400, 200, 90.0, 50.0, 95.0, 55.0),
        "iron_butterfly": sc.construct_iron_butterfly(
            24100, 300, 180.0, 175.0, 60.0, 65.0),
    }
    assert set(built) == set(eg.DIRECTION_BY_STRATEGY)
    for strategy, spread in built.items():
        assert spread["direction"] == eg.DIRECTION_BY_STRATEGY[strategy], \
            strategy


# ------------------------------------------------- gate: ledger + Discord

def test_block_writes_ledger_line():
    with _TempLedger():
        eg.gate_entry(_proposal(), entries=[_open_entry()])
        lines = [json.loads(l) for l in
                 eg.LEDGER_PATH.read_text().splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["ticker"] == "NIFTY 50"
    assert rec["direction"] == "bearish"
    assert rec["blocked_by"] == ["ab12cd34"]


def test_discord_note_once_per_day_then_silent_then_next_day():
    notes = []
    entries = [_open_entry()]
    with _TempLedger():
        day1, day2 = date(2026, 7, 14), date(2026, 7, 15)
        eg.gate_entry(_proposal(), entries=entries, today=day1,
                      notify_fn=notes.append)
        assert len(notes) == 1 and "Exposure gate" in notes[0]
        eg.gate_entry(_proposal(), entries=entries, today=day1,
                      notify_fn=notes.append)
        assert len(notes) == 1          # second block same day: silent
        eg.gate_entry(_proposal(), entries=entries, today=day2,
                      notify_fn=notes.append)
        assert len(notes) == 2          # new day: announces again


def test_ledger_write_failure_never_changes_verdict():
    with mock.patch.object(eg, "LEDGER_PATH",
                           Path("/nonexistent-root/x/y.jsonl")):
        allowed, reason = eg.gate_entry(_proposal(),
                                        entries=[_open_entry()])
    assert allowed is False             # still blocked, just unlogged
    assert "decision #68" in reason


# ------------------------------------------- run_headless integration

def _run_headless(state, entries=None, gate_spy=None):
    """options_proposer.run_headless against a temp journal with every
    seam mocked (mirrors tests/test_auto_approve.py's harness)."""
    tmp = Path(tempfile.mkdtemp())
    jpath = tmp / "journal.jsonl"
    if entries:
        jpath.write_text("".join(json.dumps(e) + "\n" for e in entries))
    discord = []
    margin_gate = mock.Mock(return_value=(True, "margin locked"))
    with mock.patch.object(journal, "DATA_DIR", tmp), \
         mock.patch.object(journal, "JOURNAL_PATH", jpath), \
         mock.patch.object(op, "build_proposal",
                           return_value={"proposal": _proposal(),
                                         "reason": "ok"}), \
         mock.patch.object(op, "_memory_context_for", return_value=""), \
         mock.patch.object(op, "_skeptic_note_for", return_value=""), \
         mock.patch.object(op, "_format_proposal_alert",
                           lambda p, action_note="": "alert"), \
         mock.patch.object(op, "_notify_discord", discord.append), \
         mock.patch("src.portfolio_manager.gate_headless_entry",
                    margin_gate), \
         mock.patch("src.notifier.fire_broadcast"), \
         _TempLedger():
        if gate_spy is not None:
            with mock.patch.object(eg, "gate_entry", gate_spy):
                result = op.run_headless("NIFTY 50", state=state)
        else:
            result = op.run_headless("NIFTY 50", state=state)
        remaining = journal.read_all()
    return result, remaining, discord, margin_gate


def test_headless_duplicate_blocked_nothing_downstream():
    """A blocked duplicate journals nothing, alerts nothing, and NEVER
    reaches the margin gate (so no margin can be locked)."""
    open_pos = _open_entry()
    result, remaining, discord, margin_gate = _run_headless(
        state={}, entries=[open_pos])
    assert result["proposed"] is False
    assert "exposure gate" in result["reason"]
    assert remaining == [open_pos]                # journal untouched
    assert not any("alert" == d for d in discord)  # no proposal card
    margin_gate.assert_not_called()


def test_headless_gate_applies_on_auto_approve_path():
    with mock.patch.dict(os.environ, {"PAPER_AUTO_APPROVE": "1"},
                         clear=False):
        result, remaining, _, margin_gate = _run_headless(
            state={}, entries=[_open_entry()])
    assert result["proposed"] is False
    assert remaining == [_open_entry()]
    margin_gate.assert_not_called()


def test_headless_no_conflict_proposes_as_before():
    result, remaining, _, _ = _run_headless(state={}, entries=None)
    assert result["proposed"] is True
    assert len(remaining) == 1


def test_headless_sandbox_book_exempt():
    """An injected `book` is its own capital world: the exposure gate is
    never even consulted (same exemption as the margin gate)."""
    spy = mock.Mock(return_value=(True, "allowed"))
    result, _, _, _ = _run_headless(
        state={"book": {"cash": 1e6, "holdings": {}}},
        entries=[_open_entry()], gate_spy=spy)
    spy.assert_not_called()
    assert result["proposed"] is True


# ------------------------------------------------ trend-flip advisory unit

def _flip(uptrend_value, entries, registry, ticker="NIFTY 50",
          today=date(2026, 7, 14)):
    return eg.trend_flip_advisory(
        ticker, 24200.0, registry=registry, entries=entries,
        closes_fn=lambda t: [100.0, 101.0],
        analysis_fn=lambda t, closes: {"uptrend": uptrend_value},
        today=today)


def test_flip_fires_once_then_silent_then_refires_on_flip_back():
    eg._CLOSES_CACHE.clear()
    reg = eg.TrendFlipRegistry()
    entries = [_open_entry()]                     # open bearish spread
    first = _flip(True, entries, reg)             # uptrend contradicts it
    assert first is not None
    assert "TREND FLIP" in first["card"]
    assert "ab12cd34" in first["card"]
    assert "Advisory only" in first["card"]
    assert _flip(True, entries, reg) is None      # same read: silent
    assert _flip(False, entries, reg) is None     # back to bearish: read
    # changed but now SUPPORTS the position — observe() consumed the flip
    assert _flip(True, entries, reg) is not None  # contradicts again


def test_flip_first_observation_into_contradicted_book_fires():
    """A daemon restarting into an already-contradicted book alerts once
    at session start instead of never."""
    eg._CLOSES_CACHE.clear()
    reg = eg.TrendFlipRegistry()
    assert _flip(True, [_open_entry()], reg) is not None


def test_flip_supporting_read_and_condors_never_fire():
    eg._CLOSES_CACHE.clear()
    reg = eg.TrendFlipRegistry()
    # bearish position + bearish read: supported, no card
    assert _flip(False, [_open_entry()], reg) is None
    # condor: trend-agnostic, never fires either way
    eg._CLOSES_CACHE.clear()
    reg2 = eg.TrendFlipRegistry()
    condor = [_open_entry(strategy="iron_condor", direction="neutral")]
    assert _flip(True, condor, reg2) is None
    assert _flip(False, condor, reg2) is None


def test_flip_no_positions_no_fetch_no_fire():
    eg._CLOSES_CACHE.clear()
    reg = eg.TrendFlipRegistry()
    fetched = []
    out = eg.trend_flip_advisory(
        "NIFTY 50", 24200.0, registry=reg, entries=[],
        closes_fn=lambda t: fetched.append(t) or [100.0],
        analysis_fn=lambda t, c: {"uptrend": True})
    assert out is None
    assert fetched == []                     # zero Dhan cost when idle


def test_flip_fail_open_on_closes_error():
    eg._CLOSES_CACHE.clear()
    reg = eg.TrendFlipRegistry()
    out = eg.trend_flip_advisory(
        "NIFTY 50", 24200.0, registry=reg, entries=[_open_entry()],
        closes_fn=mock.Mock(side_effect=RuntimeError("feed down")),
        today=date(2026, 7, 14))
    assert out is None


def test_flip_closes_cached_once_per_session():
    eg._CLOSES_CACHE.clear()
    reg = eg.TrendFlipRegistry()
    closes_fn = mock.Mock(return_value=[100.0, 101.0])
    for _ in range(3):
        eg.trend_flip_advisory(
            "NIFTY 50", 24200.0, registry=reg, entries=[_open_entry()],
            closes_fn=closes_fn,
            analysis_fn=lambda t, c: {"uptrend": True},
            today=date(2026, 7, 14))
    assert closes_fn.call_count == 1


# ------------------------------------------------ live_cycle integration

def _cycle(flip_registry=None, notify=None, entries=None):
    """One offline live_cycle pass inside market hours with a canned
    quote (mirrors tests/test_live_bridge.py's playback style)."""
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 7, 14, 10, 30, tzinfo=ist)
    return lb.live_cycle(
        ("NIFTY 50",),
        quote_fn=lambda u: {"current_price": 24200.0, "ts": now},
        entries=entries if entries is not None else [_open_entry()],
        aggregators={}, registry=lb.AlertRegistry(),
        notify_fn=notify, now_fn=lambda: now,
        flip_registry=flip_registry,
        closes_fn=lambda t: [100.0, 101.0])


def test_live_cycle_none_registry_is_a_noop():
    """flip_registry=None (every existing caller) must not consult the
    advisory at all — byte-identical legacy behavior."""
    with mock.patch.object(eg, "trend_flip_advisory") as spy:
        _cycle(flip_registry=None)
    spy.assert_not_called()


def test_live_cycle_fires_flip_card_once_across_cycles():
    notes = []
    reg = eg.TrendFlipRegistry()
    with mock.patch.object(eg, "trend_flip_advisory",
                           side_effect=lambda t, s, **kw:
                           ({"card": "FLIP CARD"}
                            if kw["registry"].observe(t, True) else None)):
        _cycle(flip_registry=reg, notify=notes.append)
        _cycle(flip_registry=reg, notify=notes.append)
    assert notes.count("FLIP CARD") == 1


def test_live_cycle_survives_advisory_explosion():
    """The belt-and-braces guard: an advisory crash never kills the
    cycle (exit alerts and the return value are unaffected)."""
    with mock.patch.object(eg, "trend_flip_advisory",
                           side_effect=RuntimeError("boom")):
        fired = _cycle(flip_registry=eg.TrendFlipRegistry())
    assert isinstance(fired, list)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

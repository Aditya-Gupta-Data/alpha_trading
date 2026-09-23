"""Decision #110 — the ASYMMETRIC PROFIT RATCHET for directional spreads:
arm at 40% of max profit (breakeven), lock 30 at 60, 50 at 80, 70 at 90;
exit `ratchet_hit` when capture falls below the lock; one-way; forward-
looking; neutral structures keep the static 65% take."""
import tempfile
from datetime import date

import pytest

from src import live_bridge as lb, plan_tracker as pt, profit_ratchet as pr


# ------------------------------------------------------------- the ladder
def test_ladder_arms_at_40_and_steps_the_lock_one_way():
    assert pr.locked_pct(None) is None and pr.locked_pct(39.9) is None
    assert pr.locked_pct(40.0) == 0.0 and pr.locked_pct(59.9) == 0.0
    assert pr.locked_pct(60.0) == 30.0 and pr.locked_pct(80.0) == 50.0 and pr.locked_pct(95.0) == 70.0
    assert pr.ratchet_hit(29.9, 30.0) and not pr.ratchet_hit(30.0, 30.0) and not pr.ratchet_hit(50.0, None)
    st = pr.state(45.0, persisted_lock=50.0)          # a persisted lock is a floor
    assert st["locked_pct"] == 50.0 and st["armed"]


def test_walk_is_one_way_and_fires_on_the_first_bar_below_the_lock():
    caps = [("2026-09-24", 10.0), ("2026-09-25", 45.0), ("2026-09-26", 85.0),
            ("2026-09-29", 62.0), ("2026-09-30", 49.0), ("2026-10-01", 20.0)]
    st = pr.walk(caps, effective_date="2026-09-24")
    assert st["peak_capture_pct"] == 85.0 and st["locked_pct"] == 50.0
    assert st["hit_day"] == "2026-09-30" and st["hit_capture_pct"] == 49.0
    # a lower peak later never lowers the lock
    st2 = pr.walk(caps[:4], effective_date="2026-09-24")
    assert st2["locked_pct"] == 50.0 and st2["hit_day"] is None


def test_walk_never_fires_on_a_bar_before_the_effective_date():
    caps = [("2026-09-10", 85.0), ("2026-09-11", 20.0), ("2026-09-24", 20.0)]
    st = pr.walk(caps, effective_date="2026-09-24")
    assert st["locked_pct"] == 50.0 and st["hit_day"] == "2026-09-24"   # the peak is a fact; the exit is forward
    assert pr.walk(caps[:2], effective_date="2026-09-24")["hit_day"] is None


# --------------------------------------------------------- the resolver
def _bear_put(entry_date="2026-09-01", expiry="2026-10-29"):
    # long 100 PE @ 10, short 90 PE @ 4 -> debit 6, max loss 6, max profit 4 per share
    return {"short_id": "rt000001", "ticker": "NIFTY 50", "date": entry_date,
            "decision": "approved", "outcome": None, "signal": "t",
            "spread": {"strategy": "bear_put_spread", "direction": "bearish",
                       "expiry": expiry, "lot_size": 10, "lots": 1,
                       "max_loss": 60.0, "max_profit": 40.0, "entry_spot": 100.0,
                       "legs": [{"side": "BUY", "option_type": "PE", "strike": 100.0, "premium": 10.0},
                                {"side": "SELL", "option_type": "PE", "strike": 90.0, "premium": 4.0}]}}


def _condor(entry_date="2026-09-01", expiry="2026-10-29"):
    return {"short_id": "rt000002", "ticker": "NIFTY 50", "date": entry_date,
            "decision": "approved", "outcome": None, "signal": "t",
            "spread": {"strategy": "iron_condor", "direction": "neutral",
                       "expiry": expiry, "lot_size": 10, "lots": 1,
                       "max_loss": 60.0, "max_profit": 40.0, "entry_spot": 100.0,
                       "legs": [{"side": "SELL", "option_type": "PE", "strike": 90.0, "premium": 3.0},
                                {"side": "BUY", "option_type": "PE", "strike": 80.0, "premium": 1.0},
                                {"side": "SELL", "option_type": "CE", "strike": 110.0, "premium": 3.0},
                                {"side": "BUY", "option_type": "CE", "strike": 120.0, "premium": 1.0}]}}


def _bars(days_closes):
    return [(d, c - 1, c + 1, c) for d, c in days_closes]


def _capture(entry, close, day):
    """The tracker's own capture arithmetic for one bar (test helper)."""
    s = entry["spread"]
    expiry = date.fromisoformat(s["expiry"]); e = date.fromisoformat(entry["date"])
    frac = max(0.0, (expiry - date.fromisoformat(day)).days / max(1, (expiry - e).days))
    m = pt._spread_mark(s, close, frac) - pt._spread_entry_mark(s)
    p = max(-6.0, min(m, 4.0))
    return p / 4.0 * 100


def test_directional_spread_rides_past_65_and_exits_on_the_ratchet(monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-09-02")
    e = _bear_put()
    # a strong fall lifts capture above 65% (the old take) — the trade HOLDS
    path = [("2026-09-02", 100.0), ("2026-09-03", 95.0), ("2026-09-04", 88.0), ("2026-09-05", 85.0)]
    bars = _bars(path)
    caps = [_capture(e, c, d) for d, c in path]
    assert max(caps) > 65.0
    assert pt._resolve_spread(e, bars) is None                       # no static take any more
    assert e["ratchet"]["armed"] and e["ratchet"]["locked_pct"] >= 30.0
    # then the give-back: capture falls under the lock -> ratchet_hit on THAT bar
    give_back = path + [("2026-09-08", 97.0), ("2026-09-09", 99.5)]
    hit = pt._resolve_spread(_bear_put(), _bars(give_back))
    assert hit is not None and hit[0] == "ratchet_hit"
    assert hit[3] in ("2026-09-08", "2026-09-09")


def test_ratchet_never_settles_retroactively_before_the_effective_date(monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-09-24")
    e = _bear_put()
    old = [("2026-09-02", 100.0), ("2026-09-03", 85.0), ("2026-09-04", 99.5), ("2026-09-05", 99.5)]
    assert pt._resolve_spread(e, _bars(old)) is None                  # Issue 31 rule
    assert e["ratchet"]["armed"]                                       # the peak was still recorded
    hit = pt._resolve_spread(_bear_put(), _bars(old + [("2026-09-24", 99.5)]))
    assert hit is not None and hit[0] == "ratchet_hit" and hit[3] == "2026-09-24"


def test_neutral_structures_keep_the_static_65_percent_take(monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-09-02")
    e = _condor()
    bars = _bars([("2026-09-02", 100.0), ("2026-09-03", 100.0), ("2026-09-04", 100.0)] +
                 [(f"2026-10-{d:02d}", 100.0) for d in range(1, 25)])
    hit = pt._resolve_spread(e, bars)
    assert hit is not None and hit[0] == "profit_take"
    assert "ratchet" not in e


def test_unarmed_directional_rides_to_the_pre_expiry_exit(monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_EFFECTIVE_DATE", "2026-09-02")
    e = _bear_put(expiry="2026-09-30")
    bars = _bars([(f"2026-09-{d:02d}", 100.0) for d in range(2, 30)])
    hit = pt._resolve_spread(e, bars)
    assert hit is not None and hit[0] == "pre_expiry_exit"


def test_ratchet_can_be_switched_off_back_to_the_static_take(monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    e = _bear_put()
    hit = pt._resolve_spread(e, _bars([("2026-09-02", 100.0), ("2026-09-03", 90.0), ("2026-09-04", 85.0)]))
    assert hit is not None and hit[0] == "profit_take"


# ------------------------------------------------------- the live bridge
def test_live_bridge_signals_ratchet_hit_from_the_persisted_state():
    e = _bear_put(entry_date="2026-09-24", expiry="2026-10-29")
    e["ratchet"] = {"peak_capture_pct": 85.0, "locked_pct": 50.0, "armed": True}
    sig = lb.evaluate_position(e, spot=98.0, today=date(2026, 9, 25))    # capture well under 50%
    assert sig["signal"] == "ratchet_hit" and sig["ratchet"]["locked_pct"] == 50.0
    strong = lb.evaluate_position(e, spot=85.0, today=date(2026, 9, 25))
    assert strong["signal"] == "hold" and strong["capture_pct"] > 50.0
    fresh = lb.evaluate_position(_bear_put(entry_date="2026-09-24", expiry="2026-10-29"),
                                 spot=85.0, today=date(2026, 9, 25))
    assert fresh["signal"] == "hold" and fresh["ratchet"]["new_rung"] and fresh["ratchet"]["armed"]
    assert lb.evaluate_position(_condor(entry_date="2026-09-24", expiry="2026-10-29"),
                                spot=100.0, today=date(2026, 10, 20))["ratchet"] is None


def test_intraday_square_off_verifies_the_lock_on_real_quotes(monkeypatch):
    from src import journal
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    monkeypatch.setattr(pt, "journal", journal)     # another test file leaves a FakeJournal on pt
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        monkeypatch.setattr(journal, "JOURNAL_PATH", Path(tmp) / "j.jsonl")
        e = _bear_put(entry_date="2026-09-24", expiry="2026-10-29")
        e["ratchet"] = {"peak_capture_pct": 85.0, "locked_pct": 50.0, "armed": True}
        journal.rewrite_all([e])
        monkeypatch.setattr(pt, "_settle_spread_cash", lambda pnl: True)
        monkeypatch.setattr("src.portfolio_manager.release_entry", lambda *a, **k: {"released": True})
        # real quotes say capture is 75% (above the lock): refuse, EOD owns it
        above = pt.resolve_intraday_profit_take("rt000001", {(100.0, "PE"): 10.0, (90.0, "PE"): 1.0},
                                                today=date(2026, 9, 25), resolution="ratchet_hit",
                                                locked_pct=50.0)
        assert above["status"] == "above_lock_on_real_quotes" and above["real_capture_pct"] == 75.0
        # real quotes say 25%: below the 50% lock -> squared off as ratchet_hit
        hit = pt.resolve_intraday_profit_take("rt000001", {(100.0, "PE"): 8.0, (90.0, "PE"): 1.0},
                                              today=date(2026, 9, 25), resolution="ratchet_hit",
                                              locked_pct=50.0)
        assert hit["status"] == "squared_off"
        row = journal.read_all()[0]
        assert row["outcome"]["resolution"] == "ratchet_hit" and "ratchet" in row["outcome"]["verdict"]


def test_note_ratchet_only_ever_raises_the_stored_state(monkeypatch):
    from src import journal
    monkeypatch.setattr(pt, "journal", journal)     # another test file leaves a FakeJournal on pt
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        monkeypatch.setattr(journal, "JOURNAL_PATH", Path(tmp) / "j.jsonl")
        journal.rewrite_all([_bear_put()])
        assert pt.note_ratchet("rt000001", 62.0, 30.0)
        assert not pt.note_ratchet("rt000001", 45.0, 0.0)              # lower: ignored
        assert pt.note_ratchet("rt000001", 91.0, 70.0)
        r = journal.read_all()[0]["ratchet"]
        assert r["peak_capture_pct"] == 91.0 and r["locked_pct"] == 70.0 and r["source"] == "live_bridge"

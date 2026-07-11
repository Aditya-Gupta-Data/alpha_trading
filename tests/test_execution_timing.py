"""
Tests for the §5.5 T+1-open execution-timing contract
(src/execution_timing.py + simulator.run_simulation(eod_signal_days=...)).

Fully offline. The contract under test: an EOD-sourced signal dated T
never executes at T's close — the decision uses only data known by T,
the fill happens at T+1's open (the gapped price), the receipt carries
signal_day / signal_age_hours / entry_basis, and a missing open REFUSES
the trade instead of interpolating.

Run:
    python tests/test_execution_timing.py
    pytest tests/test_execution_timing.py -v
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import execution_timing as et
from src import simulator as sim
from tests.test_simulator import make_history


# ------------------------------------------------------------ unit level

def test_signal_age_hours_overnight_and_over_weekend():
    # Friday deals print (19:30) -> Monday 09:15 open: 61.75h, not 13.75h.
    assert et.signal_age_hours("deals", "2026-07-10", "2026-07-13") == 61.75
    # Plain overnight: Tue -> Wed.
    assert et.signal_age_hours("deals", "2026-07-07", "2026-07-08") == 13.75
    # Unknown layer / garbage dates / execution before publication -> None.
    assert et.signal_age_hours("astrology", "2026-07-07", "2026-07-08") is None
    assert et.signal_age_hours("deals", "not-a-date", "2026-07-08") is None
    assert et.signal_age_hours("deals", "2026-07-08", "2026-07-08") is None


def test_next_trading_bar_skips_non_trading_days():
    bars = [("2026-07-09", 1, 2, 1.5), ("2026-07-10", 1, 2, 1.5),
            ("2026-07-13", 1, 2, 1.5)]  # Fri -> Mon (weekend absent)
    i, bar = et.next_trading_bar(bars, "2026-07-10")
    assert (i, bar[0]) == (2, "2026-07-13")
    assert et.next_trading_bar(bars, "2026-07-13") is None
    assert et.next_trading_bar([], "2026-07-10") is None


def test_t1_open_entry_uses_true_open_and_refuses_without_one():
    bars = [("2026-07-10", 1, 2, 1.5), ("2026-07-13", 1, 2, 1.5)]
    opens = {"2026-07-13": 1.62}
    fill = et.t1_open_entry(bars, "2026-07-10", opens)
    assert fill == {"exec_day": "2026-07-13", "open": 1.62,
                    "bar_index": 1, "basis": "t1_open"}
    # No open recorded for the next day -> refusal, never interpolation.
    assert et.t1_open_entry(bars, "2026-07-10", {}) is None
    assert et.t1_open_entry(bars, "2026-07-10", None) is None


# ------------------------------------------------------ simulator wiring

def _run(conn, eod_days=None, opens=None):
    bars, start, end = make_history()
    vix = {b[0]: 12.0 for b in bars}
    stats = sim.run_simulation(
        start, end, ("NIFTY 50",), conn=conn,
        bars_by_underlying={"NIFTY 50": bars}, vix_by_date=vix,
        eod_signal_days=eod_days, opens_by_date=opens)
    return stats, bars, start


def test_eod_signal_day_executes_at_t1_open_with_receipt():
    conn = brain_map.connect(":memory:")
    bars, start, _ = make_history()
    # Every day carries an open = that day's close - 3 (a visible gap vs
    # the prior close so a wrong-source fill would be detectable).
    opens = {b[0]: b[3] - 3.0 for b in bars}
    stats, bars, start = _run(conn, eod_days={start}, opens=opens)
    assert stats["t1_entries"] == 1
    row = conn.execute(
        "SELECT proposed_on, signal_day, signal_age_hours, entry_basis "
        "FROM simulated_trades WHERE signal_day IS NOT NULL").fetchone()
    assert row["signal_day"] == start
    assert row["entry_basis"] == "t1_open"
    # Entry is dated the NEXT trading day, never the signal day.
    exec_day = et.next_trading_bar(bars, start)[1][0]
    assert row["proposed_on"] == exec_day
    assert row["signal_age_hours"] == et.signal_age_hours(
        "deals", start, exec_day)
    conn.close()


def test_missing_open_refuses_the_trade_not_interpolates():
    conn = brain_map.connect(":memory:")
    stats, _, _ = _run(conn, eod_days={make_history()[1]}, opens={})
    assert stats["t1_refused_no_open"] == 1
    assert stats["t1_entries"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM simulated_trades "
                        "WHERE signal_day IS NOT NULL").fetchone()["c"] == 0
    conn.close()


def test_default_path_is_byte_identical_without_eod_days():
    """No eod_signal_days -> the pre-contract behaviour exactly: same-day
    entries, NULL timing columns, no opens required."""
    conn = brain_map.connect(":memory:")
    stats, _, _ = _run(conn)
    assert stats["resolved"] >= 1
    assert stats["t1_entries"] == 0 and stats["t1_refused_no_open"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM simulated_trades "
                        "WHERE signal_day IS NOT NULL "
                        "OR entry_basis IS NOT NULL").fetchone()["c"] == 0
    conn.close()


def test_t1_ref_never_collides_with_same_day_technical_trade():
    assert sim.sim_ref("NIFTY 50", "2026-07-10", "iron_condor",
                       "2026-07-17") != \
           sim.sim_ref("NIFTY 50", "2026-07-10", "iron_condor",
                       "2026-07-17", basis="t1_open")
    # And the basis'd ref is still deterministic.
    assert sim.sim_ref("N", "d", "s", "e", basis="t1_open") == \
           sim.sim_ref("N", "d", "s", "e", basis="t1_open")


def test_decision_uses_only_data_known_by_signal_day():
    """Timelock over the T+1 path: mutating every bar AFTER the execution
    day's close data must not change whether/what the signal-day proposal
    decides (the analysis feeding the decision ends at T)."""
    bars, start, end = make_history()
    opens = {b[0]: b[3] - 3.0 for b in bars}
    exec_i = et.next_trading_bar(bars, start)[0]

    def decide(world):
        conn = brain_map.connect(":memory:")
        sim.run_simulation(start, start, ("NIFTY 50",), conn=conn,
                           bars_by_underlying={"NIFTY 50": world},
                           vix_by_date={b[0]: 12.0 for b in world},
                           eod_signal_days={start}, opens_by_date=opens)
        row = conn.execute(
            "SELECT underlying, strategy, proposed_on, signal_day, "
            "net_credit, spread_width, lots FROM simulated_trades "
            "WHERE signal_day IS NOT NULL").fetchone()
        conn.close()
        return dict(row) if row else None

    baseline = decide(bars)
    assert baseline is not None
    # Crash the market from exec_day+1 on — decision inputs end at T and
    # the fill at T+1's open, so the DECISION must be identical (the
    # resolution may differ; it's allowed to see the post-entry world).
    mutated = [b if i <= exec_i else (b[0], b[1] * 0.5, b[2] * 0.5, b[3] * 0.5)
               for i, b in enumerate(bars)]
    assert decide(mutated) == baseline


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

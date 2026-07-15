"""
Tests for src/performance.py — risk-adjusted track-record metrics.

Offline: journal entries are injected, no Discord. Pure-math helpers
checked against hand-computed values.

    python -m pytest tests/test_performance.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import performance as perf


def entry(r, pnl, date, hypo=False, resolved=True):
    o = None
    if resolved:
        o = {"r_multiple": r, "pnl_rs": pnl, "exit_date": date,
             "resolution": "profit_take" if r > 0 else "stop_hit"}
        if hypo:
            o["hypothetical"] = True
    return {"short_id": f"t{date}", "outcome": o}


# ------------------------------------------------------------- helpers

def test_stdev_and_mean():
    assert perf._mean([1, 2, 3]) == 2
    assert round(perf._stdev([1, 2, 3]), 4) == 1.0  # sample sd of 1,2,3


def test_sharpe_none_when_no_dispersion():
    assert perf.sharpe([1.0, 1.0, 1.0]) is None       # zero stdev
    assert perf.sharpe([2.0, -1.0, 2.0, -1.0]) is not None


def test_sortino_none_when_no_losses():
    assert perf.sortino([1.0, 2.0, 0.5]) is None       # no downside
    s = perf.sortino([2.0, -1.0, -1.0, 2.0])
    assert s is not None


def test_max_drawdown_peak_to_trough():
    # cumulative: 1, 3, 1, 2 -> peak 3, trough 1 -> drawdown 2
    assert perf.max_drawdown([1, 2, -2, 1]) == 2.0
    assert perf.max_drawdown([1, 1, 1]) == 0.0          # monotone up
    assert perf.max_drawdown([]) == 0.0


# --------------------------------------------------------- resolved_returns

def test_resolved_returns_excludes_unresolved_and_hypothetical():
    entries = [
        entry(1.0, 500, "2026-07-01"),
        entry(-1.0, -300, "2026-07-02"),
        entry(2.0, 900, "2026-07-03", hypo=True),   # hypothetical -> out
        entry(None, 0, "2026-07-04", resolved=False),  # unresolved -> out
    ]
    rows = perf.resolved_returns(entries)
    assert [r["r"] for r in rows] == [1.0, -1.0]     # sorted, real only


def test_resolved_returns_sorted_by_exit_date():
    entries = [entry(1.0, 1, "2026-07-05"), entry(-1.0, -1, "2026-07-01")]
    assert [r["date"] for r in perf.resolved_returns(entries)] == \
        ["2026-07-01", "2026-07-05"]


# ------------------------------------------------------------- compute

def test_compute_abstains_below_floor():
    entries = [entry(1.0, 100, f"2026-07-{d:02d}") for d in range(1, 5)]
    m = perf.compute(entries, min_trades=20)
    assert m["verdict"] == "abstain" and m["n"] == 4


def test_compute_full_metrics_above_floor():
    # 10 trades: 6 wins at +1R, 4 losses at -1R
    entries = ([entry(1.0, 500, f"2026-07-{d:02d}") for d in range(1, 7)]
               + [entry(-1.0, -500, f"2026-07-{d:02d}") for d in range(7, 11)])
    m = perf.compute(entries, min_trades=10)
    assert m["verdict"] == "ok" and m["n"] == 10
    assert m["win_rate"] == 60.0
    assert m["avg_r"] == round((6 - 4) / 10, 3)          # +0.2R expectancy
    assert m["total_pnl"] == 6 * 500 - 4 * 500           # +1000
    assert m["sharpe"] is not None and m["sortino"] is not None
    assert m["max_drawdown_r"] >= 0


def test_compute_respects_config_floor():
    entries = [entry(1.0, 100, f"2026-07-{d:02d}") for d in range(1, 13)]
    assert perf.compute(entries, config={"performance_min_trades": 15})[
        "verdict"] == "abstain"
    assert perf.compute(entries, config={"performance_min_trades": 10})[
        "verdict"] == "ok"


# ----------------------------------------------------------- card + run

def test_card_abstain_and_ok():
    ab = perf.build_card({"verdict": "abstain", "reason": "only 3", "n": 3})
    assert "abstaining" in ab
    entries = ([entry(1.0, 500, f"2026-07-{d:02d}") for d in range(1, 7)]
               + [entry(-1.0, -500, f"2026-07-{d:02d}") for d in range(7, 11)])
    ok = perf.build_card(perf.compute(entries, min_trades=10))
    assert "Sharpe" in ok and "resolved paper trades" in ok


def test_run_posts_only_on_ok_verdict():
    sent = []
    # abstain -> no post
    perf.run([entry(1.0, 1, "2026-07-01")], notify_fn=sent.append,
             config={"performance_min_trades": 20})
    assert sent == []
    # ok -> posts
    entries = ([entry(1.0, 500, f"2026-07-{d:02d}") for d in range(1, 7)]
               + [entry(-1.0, -500, f"2026-07-{d:02d}") for d in range(7, 11)])
    perf.run(entries, notify_fn=sent.append,
             config={"performance_min_trades": 10})
    assert len(sent) == 1


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python", "-m", "pytest", __file__, "-q"]))

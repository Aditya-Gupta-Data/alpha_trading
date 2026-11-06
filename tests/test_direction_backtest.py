"""Tests for src/research/direction_backtest.py. Hermetic: synthetic close series, tmp_path CSV."""
from __future__ import annotations

from datetime import date, timedelta

from src.research import direction_backtest as db


def _series(n=400, start=date(2023, 1, 2), slope=1.0, wiggle=0.0):
    out, px = [], 1000.0
    for i in range(n):
        px += slope + (wiggle if i % 2 else -wiggle)
        out.append(((start + timedelta(days=i)).isoformat(), px))
    return out


def test_a_steady_uptrend_is_called_bullish_and_graded_without_overlap():
    s = _series()
    calls, neutral = db.calls_for("RELIANCE", s, "2023-09-01", "2023-12-31", 5)
    assert calls and all(c["view"] == "bullish" and c["signed_pct"] > 0 for c in calls)
    days = [c["day"] for c in calls]
    assert all((date.fromisoformat(b) - date.fromisoformat(a)).days >= 6 for a, b in zip(days, days[1:]))
    assert all(c["day"] <= "2023-12-31" for c in calls) and neutral == 0


def test_entry_is_the_next_close_not_the_signal_close():
    s = _series()
    c = db.calls_for("X", s, "2023-09-01", "2023-09-01", 5)[0][0]
    i = [d for d, _ in s].index(c["day"])
    assert abs(c["return_pct"] - (s[i + 6][1] - s[i + 1][1]) / s[i + 1][1] * 100) < 1e-9


def test_stats_and_placebo_and_run_groups():
    up, down = _series(slope=1.0), _series(slope=-1.0)
    rep = db.run("2023-09-01", "2023-12-31", horizons=(5,),
                 series_by_name={"NIFTY 50": up, "TCS": down})
    g = rep["horizons"][5]["groups"]
    assert g["ALL"]["n"] == g["INDEX"]["n"] + g["STOCKS"]["n"]
    assert g["BEARISH"]["n"] == g["STOCKS"]["n"] and g["BULLISH"]["n"] == g["INDEX"]["n"]
    assert g["ALL"]["hit_rate"] == 1.0 and g["ALL"]["placebo_p"] < 0.05
    assert "YEAR 2023" in g and "BACKTEST" in rep["label"]
    assert any("5-session" in l for l in db.summary_lines(rep))
    assert db.stats([]) == {"n": 0}


def test_index_csv_loader(tmp_path):
    (tmp_path / "NIFTY.csv").write_text("date,value\n2024-01-01,100.5\n2024-01-02,\n2024-01-03,101\n")
    assert db.load_index_closes("NIFTY 50", tmp_path) == [("2024-01-01", 100.5), ("2024-01-03", 101.0)]

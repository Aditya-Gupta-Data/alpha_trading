"""scripts/market_vs_skill_study.py — the read-only market-vs-skill study."""
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "market_vs_skill_study", Path(__file__).resolve().parent.parent / "scripts" / "market_vs_skill_study.py")
ms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ms)


def _falling(start="2026-07-01", days=120, first=50000.0, step=-50.0):
    d0 = date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), first + step * i) for i in range(days)]


def _e(sid, ticker, strategy, entry, exit_, pnl, spot=None, decision="approved", hyp=False):
    return {"short_id": sid, "ticker": ticker, "date": entry, "decision": decision,
            "spread": {"strategy": strategy, **({"entry_spot": spot} if spot else {})},
            "outcome": {"pnl_rs": pnl, "exit_date": exit_, "hypothetical": hyp}}


def test_loader_keeps_settled_verticals_only(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [
        _e("a", "NIFTY BANK", "bear_put_spread", "2026-08-01", "2026-08-10", 1000.0),
        _e("b", "NIFTY BANK", "iron_condor", "2026-08-01", "2026-08-10", 1000.0),
        _e("c", "NIFTY BANK", "bear_put_spread", "2026-08-01", "2026-08-10", 1000.0, decision="rejected"),
        _e("d", "NIFTY BANK", "bull_call_spread", "2026-08-01", "2026-08-10", -5.0, hyp=True),
        {"short_id": "e", "decision": "approved", "date": "2026-08-01",
         "spread": {"strategy": "bear_put_spread"}, "outcome": {"pnl_rs": 1.0}},
    ]) + "\nbad\n")
    trades, skipped = ms.load_trades(p)
    assert [t["ref"] for t in trades] == ["a"] and trades[0]["bet"] == "down"
    assert skipped == {"no pnl / exit date on the outcome": 1}


def test_series_keys_and_lake_readers(tmp_path):
    assert ms.series_key("NIFTY BANK") == ("macro", "NIFTY_BANK")
    assert ms.series_key("NIFTY MID SELECT") == ("macro", "NIFTY_MID_SELECT")
    assert ms.series_key("TCS.NS") == ("bhav", "TCS")
    macro = tmp_path / "macro"
    macro.mkdir()
    (macro / "NIFTY_BANK.csv").write_text("date,value\n2026-08-01,50000\n2026-08-04,49000\n2026-08-05,\n")
    bhav = tmp_path / "bhav"
    bhav.mkdir()
    for d, px in (("2026-08-01", "3000"), ("2026-08-04", "3100")):
        (bhav / f"{d}.csv").write_text("SYMBOL, SERIES, CLOSE_PRICE\nTCS, EQ, %s\nTCS, BE, 1\nINFY, EQ, 9\n" % px)
    trades = [{"ticker": "NIFTY BANK", "entry_date": "2026-08-01"}, {"ticker": "TCS.NS", "entry_date": "2026-08-01"},
              {"ticker": "NIFTY MID SELECT", "entry_date": "2026-08-01"}]
    s = ms.load_series(trades, macro, bhav)
    assert s["NIFTY BANK"] == [("2026-08-01", 50000.0), ("2026-08-04", 49000.0)]
    assert s["TCS.NS"] == [("2026-08-01", 3000.0), ("2026-08-04", 3100.0)]
    assert "NIFTY MID SELECT" not in s


def test_falling_market_bearish_bets_score_as_market_not_skill():
    series = {"NIFTY BANK": _falling()}
    trades = [{"ref": f"t{i}", "ticker": "NIFTY BANK", "bet": "down",
               "entry_date": (date(2026, 7, 5) + timedelta(days=3 * i)).isoformat(),
               "exit_date": (date(2026, 7, 15) + timedelta(days=3 * i)).isoformat(),
               "entry_spot": None, "pnl": 1000.0} for i in range(25)]
    trades.append({"ref": "x", "ticker": "NIFTY MID SELECT", "bet": "down", "entry_date": "2026-07-05",
                   "exit_date": "2026-07-15", "entry_spot": None, "pnl": 1.0})
    rows, skipped = ms.evaluate(trades, series)
    assert skipped == {"no lake series for NIFTY MID SELECT": 1}
    s = ms.summary(rows)
    assert s["hit_rate"] == 100.0 and s["base_rate"] == 100.0       # every down bet wins in a one-way fall
    assert "Mostly the market" in ms.verdict(s)
    assert s["market_our_way"] == {"n": 25, "wins": 25, "pnl": 25000.0}


def test_timing_skill_is_recognised_in_a_choppy_market():
    # alternate up/down weeks: a random down bet is right ~half the time
    d0, px, series = date(2026, 7, 1), 50000.0, []
    for i in range(140):
        px += 100.0 if (i // 7) % 2 == 0 else -100.0
        series.append(((d0 + timedelta(days=i)).isoformat(), px))
    # our bets start exactly at each down-leg's start and exit 6 days later
    trades = [{"ref": f"t{k}", "ticker": "NIFTY", "bet": "down",
               "entry_date": (d0 + timedelta(days=7 * k - 1)).isoformat(),
               "exit_date": (d0 + timedelta(days=7 * k + 5)).isoformat(),
               "entry_spot": None, "pnl": 500.0} for k in range(1, 19, 2)]
    trades += [dict(t, ref=t["ref"] + "b") for t in trades] + [dict(t, ref=t["ref"] + "c") for t in trades]
    rows, _ = ms.evaluate(trades, {"NIFTY": series})
    s = ms.summary(rows)
    assert s["hit_rate"] == 100.0 and s["base_rate"] < 70 and s["z"] >= 2
    assert ms.verdict(s).startswith("Timing skill")


def test_render_and_main_name_gaps(tmp_path):
    out = tmp_path / "r.md"
    assert ms.main(["--journal", str(tmp_path / "none.jsonl"), "--macro-dir", str(tmp_path),
                    "--bhav-dir", str(tmp_path), "--out", str(out)]) == 0
    assert "no settled directional spreads" in out.read_text()
    rows = [{"ref": "a", "ticker": "NIFTY", "bet": "up", "move_pct": -1.0, "right": False, "hold_days": 5,
             "base": 0.5, "pnl": -700.0}]
    text = ms.render(rows, {"no lake series for X": 2}, [])
    assert "## Bullish bets — 1 trades ⚠️ n<20" in text and "Too few trades" in text
    assert "| Market moved against us | 1 | 0 | -Rs.700 |" in text and "- no lake series for X: 2" in text

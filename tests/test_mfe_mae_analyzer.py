"""
Tests for src/calibration/mfe_mae_analyzer.py — the MFE/MAE expectancy
surface (§3.1). Entirely offline: synthetic OHLC bars, injected journal
entries, a seeded temp sqlite DB; SafeDhanClient is never constructed
(fetch_bars_fn is injected everywhere).

Run:
    python tests/test_mfe_mae_analyzer.py
    pytest tests/test_mfe_mae_analyzer.py -v
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.calibration import mfe_mae_analyzer as mm


def _bar(day, high, low, open_=None, close=None):
    return {"date": day, "open": open_ if open_ is not None else low,
            "high": high, "low": low,
            "close": close if close is not None else low, "volume": 0}


# A 4-day window around a ref price of 100: runs up to 108, down to 95.
WINDOW = [
    _bar("2026-07-01", 103.0, 99.0, open_=100.0),
    _bar("2026-07-02", 108.0, 101.0),
    _bar("2026-07-03", 106.0, 95.0),
    _bar("2026-07-04", 102.0, 98.0),
]


# ---------------------------------------------------------- excursion math

def test_long_excursions_use_highest_high_and_lowest_low():
    exc = mm.compute_excursions(WINDOW, ref_price=100.0, direction=mm.LONG)
    assert exc["mfe_abs"] == 8.0          # 108 high vs 100 entry
    assert exc["mae_abs"] == 5.0          # 95 low vs 100 entry
    assert exc["mfe_pct"] == 8.0
    assert exc["mae_pct"] == 5.0
    assert exc["bars"] == 4


def test_short_excursions_are_mirrored():
    exc = mm.compute_excursions(WINDOW, ref_price=100.0, direction=mm.SHORT)
    assert exc["mfe_abs"] == 5.0          # favorable = the drop to 95
    assert exc["mae_abs"] == 8.0          # adverse = the rally to 108
    assert exc["mfe_pct"] == 5.0 and exc["mae_pct"] == 8.0


def test_excursions_clamp_at_zero_never_negative():
    # price only ever rose: a long trade has zero ADVERSE excursion...
    rising = [_bar("2026-07-01", 105.0, 101.0), _bar("2026-07-02", 110.0, 104.0)]
    long_exc = mm.compute_excursions(rising, 100.0, mm.LONG)
    assert long_exc["mae_abs"] == 0.0 and long_exc["mae_pct"] == 0.0
    # ...and a short trade in the same window has zero FAVORABLE excursion.
    short_exc = mm.compute_excursions(rising, 100.0, mm.SHORT)
    assert short_exc["mfe_abs"] == 0.0


def test_excursions_undefined_cases_return_none():
    assert mm.compute_excursions([], 100.0, mm.LONG) is None
    assert mm.compute_excursions(WINDOW, 100.0, None) is None      # neutral
    assert mm.compute_excursions(WINDOW, 0, mm.LONG) is None       # no ref
    assert mm.compute_excursions(WINDOW, None, mm.LONG) is None


def test_trim_window_is_inclusive_on_both_ends():
    trimmed = mm.trim_window(WINDOW, "2026-07-02", "2026-07-03")
    assert [b["date"] for b in trimmed] == ["2026-07-02", "2026-07-03"]
    assert mm.trim_window(WINDOW, "2026-08-01", "2026-08-05") == []


# ------------------------------------------------------- direction mapping

def test_strategy_direction_mapping():
    assert mm.direction_for_strategy("bull_call_spread") == mm.LONG
    assert mm.direction_for_strategy("bear_put_spread") == mm.SHORT
    assert mm.direction_for_strategy("bear_call_spread") == mm.SHORT
    assert mm.direction_for_strategy("iron_condor") is None
    assert mm.direction_for_strategy(None) is None


# ---------------------------------------------------------------- loaders

def _closed_spread_entry(short_id="sprd0001", strategy="bear_put_spread",
                         hypothetical=False, ticker="NIFTY 50"):
    return {
        "short_id": short_id, "date": "2026-07-01", "action": "SPREAD",
        "ticker": ticker, "price": 72.3, "decision": "approved",
        "spread": {"strategy": strategy, "entry_spot": 24169.45,
                   "expiry": "2026-07-21", "lots": 1, "lot_size": 75,
                   "max_loss": 5422.5, "max_profit": 9577.5},
        "outcome": {"resolution": "profit_take", "exit_date": "2026-07-04",
                    "pnl_rs": 4500.0, "hypothetical": hypothetical},
    }


def _closed_equity_entry():
    return {
        "short_id": "eqty0001", "date": "2026-07-01", "action": "BUY",
        "ticker": "ONGC.NS", "price": 242.5, "decision": "approved",
        "plan": {"variant": "breakout", "stop_loss": 235.0, "target": 260.0},
        "outcome": {"resolution": "target_hit", "exit_date": "2026-07-03",
                    "pnl_rs": 1855.0},
    }


def test_journal_loader_normalizes_spreads_and_equity():
    open_entry = dict(_closed_spread_entry("open0001"), outcome=None)
    trades = mm.load_journal_trades(
        entries=[_closed_spread_entry(), _closed_equity_entry(), open_entry])
    assert [t["trade_id"] for t in trades] == ["sprd0001", "eqty0001"]
    spread, equity = trades
    assert spread["direction"] == mm.SHORT           # bear put = short
    assert spread["ref_price"] == 24169.45           # the underlying spot
    assert spread["exit_date"] == "2026-07-04"
    assert equity["direction"] == mm.LONG
    assert equity["ref_price"] == 242.5


def test_journal_loader_hypothetical_toggle():
    entries = [_closed_spread_entry(),
               _closed_spread_entry("hypo0001", hypothetical=True)]
    assert len(mm.load_journal_trades(entries=entries)) == 2
    approved_only = mm.load_journal_trades(entries=entries,
                                           include_hypothetical=False)
    assert [t["trade_id"] for t in approved_only] == ["sprd0001"]
    assert mm.load_journal_trades(entries=entries)[1]["hypothetical"] is True


def _seed_simulated_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE simulated_trades (
        journal_ref TEXT PRIMARY KEY, underlying TEXT, strategy TEXT,
        view TEXT, proposed_on TEXT, expiry TEXT, vix REAL, net_credit REAL,
        net_debit REAL, spread_width REAL, max_loss REAL, max_profit REAL,
        lots INTEGER, lot_size INTEGER, resolution TEXT, exit_date TEXT,
        pnl_net REAL, frictions_rs REAL, slippage_rs REAL, capture_pct REAL,
        r_multiple REAL, result TEXT, verdict TEXT)""")
    for ref, strat, opened, exited, pnl in rows:
        conn.execute(
            "INSERT INTO simulated_trades (journal_ref, underlying, strategy,"
            " proposed_on, exit_date, pnl_net, frictions_rs, slippage_rs,"
            " resolution, result) VALUES (?, 'NIFTY 50', ?, ?, ?, ?, 0, 0,"
            " 'expiry', ?)",
            (ref, strat, opened, exited, pnl, "win" if pnl > 0 else "loss"))
    conn.commit()
    conn.close()


def test_simulated_loader_reads_every_row_read_only():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "brain_map.db"
        _seed_simulated_db(db, [
            ("sim:1", "bull_call_spread", "2026-07-01", "2026-07-04", 900.0),
            ("sim:2", "iron_condor", "2026-07-01", "2026-07-04", -400.0)])
        trades = mm.load_simulated_trades(db_path=db)
        assert len(trades) == 2                     # no status filter exists
        assert trades[0]["direction"] == mm.LONG
        assert trades[1]["direction"] is None       # condor = neutral
        assert trades[0]["ref_price"] is None       # first-bar fallback later
        # read-only URI: a write through the same mechanism must fail
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            ro.execute("DELETE FROM simulated_trades")
            assert False, "read-only connection accepted a write"
        except sqlite3.OperationalError:
            pass
        finally:
            ro.close()


def test_simulated_loader_missing_db_or_table_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        assert mm.load_simulated_trades(Path(tmp) / "nope.db") == []
        empty = Path(tmp) / "empty.db"
        sqlite3.connect(empty).close()
        assert mm.load_simulated_trades(empty) == []


# ---------------------------------------------------------------- pipeline

def test_analyze_fetches_once_per_ticker_and_slices_per_trade():
    calls = []

    def fake_fetch(ticker, start):
        calls.append((ticker, start))
        return WINDOW

    trades = [
        mm._trade_record("t1", "journal", "NIFTY 50", "bull_call_spread",
                         mm.LONG, 100.0, "2026-07-01", "2026-07-02", 500.0),
        mm._trade_record("t2", "journal", "NIFTY 50", "bear_put_spread",
                         mm.SHORT, 100.0, "2026-07-02", "2026-07-04", -300.0),
        mm._trade_record("t3", "journal", "NIFTY 50", "iron_condor",
                         None, 100.0, "2026-07-01", "2026-07-04", 200.0),
    ]
    result = mm.analyze(trades, fetch_bars_fn=fake_fetch)
    assert calls == [("NIFTY 50", "2026-07-01")]    # ONE fetch, earliest start
    assert result["skipped"] == {"neutral_strategy": 1}
    t1, t2 = result["records"]
    # t1's window is 07-01..07-02 only: high 108, low 99 -> long MFE 8, MAE 1
    assert (t1["mfe_abs"], t1["mae_abs"]) == (8.0, 1.0)
    # t2's window is 07-02..07-04: high 108, low 95 -> short MFE 5, MAE 8
    assert (t2["mfe_abs"], t2["mae_abs"]) == (5.0, 8.0)


def test_analyze_counts_data_gaps_instead_of_hiding_them():
    trades = [
        mm._trade_record("gap1", "journal", "NIFTY 50", "bull_call_spread",
                         mm.LONG, 100.0, "2026-08-01", "2026-08-04", 1.0),
        mm._trade_record("bad1", "journal", "NIFTY 50", "bull_call_spread",
                         mm.LONG, 100.0, None, "2026-07-04", 1.0),
    ]
    result = mm.analyze(trades, fetch_bars_fn=lambda t, s: WINDOW)
    assert result["records"] == []
    assert result["skipped"]["no_bars_in_window"] == 1
    assert result["skipped"]["bad_dates"] == 1


def test_analyze_first_bar_fallback_for_missing_ref_price():
    trades = [mm._trade_record("sim:1", "simulated", "NIFTY 50",
                               "bull_call_spread", mm.LONG, None,
                               "2026-07-01", "2026-07-04", 900.0)]
    result = mm.analyze(trades, fetch_bars_fn=lambda t, s: WINDOW)
    rec = result["records"][0]
    assert rec["ref_price"] == 100.0                # WINDOW[0]'s open
    assert rec["mfe_abs"] == 8.0


# -------------------------------------------------------------- statistics

def test_percentile_interpolates():
    values = [1.0, 2.0, 3.0, 4.0]
    assert mm.percentile(values, 0) == 1.0
    assert mm.percentile(values, 100) == 4.0
    assert mm.percentile(values, 50) == 2.5
    assert mm.percentile(values, 75) == 3.25
    assert mm.percentile([7.0], 75) == 7.0
    assert mm.percentile([], 50) is None


def _record(mfe, mae, pnl, strategy="bull_call_spread"):
    return {"mfe_pct": mfe, "mae_pct": mae, "pnl": pnl, "strategy": strategy}


def test_summarize_computes_winner_based_apex():
    # 20 winners (MFE 4..8ish, MAE 1..2), 10 losers — a clean edge.
    records = ([_record(4.0 + i * 0.2, 1.0 + i * 0.05, 100.0) for i in range(20)]
               + [_record(1.0, 6.0, -100.0, strategy="bear_put_spread")
                  for _ in range(10)])
    summary = mm.summarize(records, min_samples=20)
    assert summary["all"]["count"] == 30
    assert summary["winners"]["count"] == 20
    assert summary["losers"]["count"] == 10
    apex = summary["apex"]
    assert apex["status"] == "ok"
    # winners' MFE median: values 4.0,4.2..7.8 -> median 5.9
    assert apex["suggested_take_profit_pct"] == 5.9
    # winners' MAE p75: 1.0..1.95 step .05 -> p75 = 1.7125 -> 1.71
    assert apex["suggested_stop_loss_pct"] == 1.71
    assert apex["reward_risk_ratio"] == round(5.9 / 1.7125, 2)
    assert "ADVISORY ONLY" in apex["note"]
    assert set(summary["by_strategy"]) == {"bull_call_spread",
                                           "bear_put_spread"}


def test_summarize_abstains_below_the_sample_floor():
    few = [_record(5.0, 1.0, 100.0) for _ in range(5)]
    apex = mm.summarize(few, min_samples=20)["apex"]
    assert apex["status"] == "insufficient_data"
    assert "refusing" in apex["reason"]
    assert "suggested_take_profit_pct" not in apex


def test_summarize_abstains_with_zero_winners():
    all_losers = [_record(1.0, 5.0, -50.0) for _ in range(25)]
    apex = mm.summarize(all_losers, min_samples=20)["apex"]
    assert apex["status"] == "insufficient_data"


# ------------------------------------------------------------ run / report

def test_run_end_to_end_writes_the_report(capsys=None):
    entries = [_closed_spread_entry(f"id{i:04d}") for i in range(25)]
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "calibration_report.json"
        nifty_window = [_bar("2026-07-01", 24500.0, 24000.0, open_=24169.0),
                        _bar("2026-07-04", 24300.0, 23800.0)]
        report = mm.run(source="journal", journal_entries=entries,
                        fetch_bars_fn=lambda t, s: nifty_window,
                        min_samples=20, report_path=report_path)
        assert report_path.exists()
        on_disk = json.loads(report_path.read_text())
    assert on_disk["source"] == "journal"
    assert on_disk["trades_loaded"] == 25
    assert on_disk["trades_analyzed"] == 25
    assert on_disk["summary"]["apex"]["status"] == "ok"
    assert on_disk["generated_at"] == report["generated_at"]


def test_run_never_touches_dhan_when_fetcher_is_injected():
    with mock.patch("src.dhan_guard.SafeDhanClient") as client_cls:
        mm.run(source="journal", journal_entries=[],
               fetch_bars_fn=lambda t, s: [])
    assert not client_cls.called


def test_cli_flags_wire_through():
    with mock.patch.object(mm, "run") as run_fn:
        mm.main(["--source", "simulated", "--approved-only",
                 "--min-samples", "40"])
    kwargs = run_fn.call_args[1]
    assert kwargs["source"] == "simulated"
    assert kwargs["include_hypothetical"] is False
    assert kwargs["min_samples"] == 40
    assert kwargs["report_path"] is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

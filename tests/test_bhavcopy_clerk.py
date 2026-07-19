"""
The bhavcopy clerk (the Dynamic Execution Layer's fuel line), fully
offline: NULL-honest CSV parsing (NSE's ' -' null, series filter),
capture/idempotency/holiday honesty, the never-crash backfill walk
(weekends skipped, nothing interpolated), and the chronological
bars_for() reader the pricer will consume.
"""
from datetime import date

from src.ingestion import bhavcopy_clerk as BC

CSV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
RELIANCE, EQ, 17-Jul-2026, 2900.00, 2910.00, 2955.00, 2895.00, 2940.00, 2942.50, 2930.10, 5000000, 146505.00, 250000, 2500000, 50.00
NOVOLTY, BE, 17-Jul-2026, 100.00, 101.00, 105.00, 99.00, 104.00, 104.50, 102.00, 20000, 20.90, 900,  -,  -
GHOSTOPT, F1, 17-Jul-2026, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1, 0.01, 1, 1, 100.00
"""


def test_parse_is_null_honest_and_filters_series():
    rows = BC.parse_bhavcopy(CSV)
    assert set(rows) == {"RELIANCE", "NOVOLTY"}      # F1 series dropped
    r = rows["RELIANCE"]
    assert r["close"] == 2942.50 and r["volume"] == 5000000
    assert r["deliv_pct"] == 50.0
    n = rows["NOVOLTY"]
    assert n["deliv_qty"] is None and n["deliv_pct"] is None  # ' -' -> None


def test_fetch_day_captures_and_is_idempotent(tmp_path):
    r = BC.fetch_day(date(2026, 7, 17), fetch_bytes_fn=lambda u: CSV.encode(),
                     out_dir=tmp_path, log_path=tmp_path / "o.jsonl")
    assert r["status"] == "captured" and r["symbols"] == 2
    assert (tmp_path / "2026-07-17.csv").exists()
    again = BC.fetch_day(date(2026, 7, 17), fetch_bytes_fn=lambda u: CSV.encode(),
                         out_dir=tmp_path, log_path=tmp_path / "o.jsonl")
    assert again["status"] == "already_have"


def test_fetch_day_treats_html_error_page_as_holiday(tmp_path):
    r = BC.fetch_day(date(2026, 7, 16),
                     fetch_bytes_fn=lambda u: b"<html>404 not found</html>",
                     out_dir=tmp_path, log_path=tmp_path / "o.jsonl")
    assert r["status"] == "no_file"
    assert not (tmp_path / "2026-07-16.csv").exists()
    assert "BC-404" in (tmp_path / "o.jsonl").read_text()


def test_backfill_skips_weekends_and_never_crashes(tmp_path):
    fetched = []

    def flaky(url):
        fetched.append(url)
        if len(fetched) == 2:
            raise ConnectionError("HTTP Error 403")
        return CSV.encode()

    # 2026-07-19 is a Sunday; walking back 7 calendar days = 5 weekdays
    out = BC.backfill(7, end=date(2026, 7, 19), fetch_bytes_fn=flaky,
                      out_dir=tmp_path, log_path=tmp_path / "o.jsonl",
                      sleep_fn=lambda s: None)
    assert out["attempted"] == 5
    assert out["summary"]["captured"] == 4
    assert out["summary"]["outage"] == 1
    assert out["any_new"] is True


def test_bars_for_reads_chronologically_and_honestly(tmp_path):
    day1 = CSV
    day2 = CSV.replace("17-Jul-2026", "18-Jul-2026").replace(
        "2942.50", "2960.00")
    (tmp_path / "2026-07-16.csv").write_text(day1)
    (tmp_path / "2026-07-17.csv").write_text(day2)
    bars = BC.bars_for("RELIANCE.NS", lake_dir=tmp_path)
    assert [b["session"] for b in bars] == ["2026-07-16", "2026-07-17"]
    assert bars[1]["close"] == 2960.00
    assert BC.bars_for("NOSUCHNAME", lake_dir=tmp_path) == []
    assert BC.bars_for("RELIANCE", lake_dir=tmp_path / "empty") == []

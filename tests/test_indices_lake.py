"""
The NSE indices leg of the macro lake, fully offline: header-by-name
parsing survives NSE column shuffles, unmapped indices are ignored, '-'
closes stay NULL-honest, ingest is append-only per series, holidays are
honest no_file days, the forward backfill skips weekends and survives a
dead day, and dry-run writes nothing.
"""
from datetime import date

from src.ingestion import indices_lake as IL


DAY_CSV = (
    "Index Name,Index Date,Open Index Value,High Index Value,"
    "Low Index Value,Closing Index Value,Points Change,Change(%),"
    "Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
    "Nifty 50,21-07-2026,24216.05,24262.2,24135.65,24187.7,-50.8,-.21,"
    "315560285,28361.57,20.65,3.04,1.19\n"
    "India VIX,21-07-2026,12.5,13.1,12.2,12.85,0.3,2.4,0,0,0,0,0\n"
    "Nifty Bank,21-07-2026,52100,52300,51900,52200,100,.19,"
    "12345,4567.8,15.2,2.6,1.0\n"
    "Nifty Weird New Index,21-07-2026,1,2,3,4,0,0,0,0,0,0,0\n"
    "Nifty IT,21-07-2026,31000,31200,30800,-,0,0,999,111.1,25.0,5.0,2.0\n"
)


def _canned(text=DAY_CSV):
    return lambda url: text.encode()


def test_parse_maps_by_name_ignores_unmapped_and_keeps_null_holes():
    out = IL.parse_day(DAY_CSV.encode())
    assert out["NIFTY"] == 24187.7
    assert out["INDIAVIX"] == 12.85
    assert out["NIFTY_BANK"] == 52200
    assert out["NIFTY_IT"] is None            # '-' close -> None, row kept
    assert "Nifty Weird New Index" not in str(out.keys())


def test_parse_survives_shuffled_columns_and_refuses_html():
    shuffled = ("Closing Index Value,Index Name\n"
                "101.5,India VIX\n")
    assert IL.parse_day(shuffled.encode()) == {"INDIAVIX": 101.5}
    assert IL.parse_day(b"<html>blocked</html>") == {}
    assert IL.parse_day(b"") == {}


def test_ingest_day_appends_per_series_and_is_append_only(tmp_path):
    d = date(2026, 7, 21)
    out = IL.ingest_day(d, fetch_bytes_fn=_canned(), lake_dir=tmp_path)
    assert out["no_file"] is False
    assert out["rows_added"]["NIFTY"] == 24187.7
    assert (tmp_path / "NIFTY.csv").read_text().endswith(
        "2026-07-21,24187.7\n")
    # NIFTY_IT's '-' close is stored as a visible hole
    assert (tmp_path / "NIFTY_IT.csv").read_text().endswith("2026-07-21,\n")
    # series absent from the file are NAMED, not silently skipped
    assert "NIFTY_PHARMA" in out["missing_from_file"]

    again = IL.ingest_day(d, fetch_bytes_fn=_canned(), lake_dir=tmp_path)
    assert again["rows_added"] == {}          # idempotent second run
    assert "NIFTY" in again["skipped_not_newer"]

    older = IL.ingest_day(date(2026, 7, 20), fetch_bytes_fn=_canned(),
                          lake_dir=tmp_path)
    assert older["rows_added"] == {}          # append-only refuses the past


def test_holiday_404_is_an_honest_no_file(tmp_path):
    def dead(url):
        raise ConnectionError("HTTP Error 404: Not Found")
    out = IL.ingest_day(date(2026, 7, 19), fetch_bytes_fn=dead,
                        lake_dir=tmp_path)
    assert out["no_file"] is True and out["rows_added"] == {}
    assert list(tmp_path.glob("*.csv")) == []


def test_backfill_walks_forward_skips_weekends_survives_a_dead_day(tmp_path):
    calls = []

    def flaky(url):
        calls.append(url)
        if "17072026" in url:                 # Friday: server hiccup
            raise ConnectionError("HTTP Error 503")
        return DAY_CSV.encode()

    out = IL.backfill(date(2026, 7, 16), end=date(2026, 7, 21),
                      fetch_bytes_fn=flaky, sleep_fn=lambda s: None,
                      lake_dir=tmp_path, log_path=tmp_path / "o.jsonl")
    s = out["summary"]
    # Thu 16 walked+captured; Fri 17 failed (logged, walk survived);
    # Sat/Sun never fetched; Mon 20 + Tue 21 walked — rows are stamped
    # with the WALK day, so each weekday appends its own newer date.
    assert s["failed"] == 1
    assert s["captured"] == 3                 # 16, 20, 21
    assert s["attempted"] == 4                # 16,17,20,21 — no weekend
    assert "18072026" not in "".join(calls)   # Saturday never fetched
    assert "503" in (tmp_path / "o.jsonl").read_text()


def test_backfill_dry_run_writes_nothing(tmp_path):
    out = IL.backfill(date(2026, 7, 20), end=date(2026, 7, 21),
                      fetch_bytes_fn=_canned(), sleep_fn=lambda s: None,
                      lake_dir=tmp_path, dry_run=True)
    assert out["dry_run"] is True
    assert list(tmp_path.glob("*.csv")) == []

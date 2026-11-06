"""Tests for scripts/backfill_fo_history.py (MANUAL OFFLINE TOOL). Hermetic: fake zip bytes, tmp_path."""
from __future__ import annotations

import csv
import gzip
import importlib.util
import io
import zipfile
from datetime import date
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "backfill_fo_history", Path(__file__).resolve().parents[1] / "scripts" / "backfill_fo_history.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

HEADER = ",".join(bf.COLUMNS + ("Rmks",))


def _zip(rows):
    text = HEADER + "\n" + "\n".join(rows) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy.csv", text)
    return buf.getvalue()


ROWS = [
    "2024-01-02,IDO,NIFTY,2024-01-11,21700.00,CE,10,12,9,11,11,21665.8,1000,500,50,x",
    "2024-01-02,STO,ZOMATO,2024-01-25,130.00,CE,1,1,1,1,1,129,10,5,4500,x",
    "2024-01-02,IDF,NIFTY,2024-01-25,,,21700,21800,21650,21750,21750,21665.8,99,10,50,x",
]


def test_filter_keeps_desk_symbols_and_columns():
    rows = bf.filter_rows(HEADER + "\n" + "\n".join(ROWS), ["NIFTY"])
    assert [r["FinInstrmTp"] for r in rows] == ["IDO", "IDF"]
    assert set(rows[0]) == set(bf.COLUMNS) and rows[0]["ClsPric"] == "11"


def test_backfill_writes_gz_skips_weekends_and_is_resumable(tmp_path):
    calls = []
    fetch = lambda url: calls.append(url) or _zip(ROWS)
    c = bf.backfill(date(2024, 1, 5), date(2024, 1, 8), ["NIFTY"], tmp_path, fetch, sleep=lambda s: None)
    assert c == {"ok": 2, "weekend": 2} and len(calls) == 2 and "20240105" in calls[0]
    with gzip.open(tmp_path / "2024-01-05.csv.gz", "rt") as fh:
        assert len(list(csv.DictReader(fh))) == 2
    again = bf.backfill(date(2024, 1, 5), date(2024, 1, 8), ["NIFTY"], tmp_path, fetch, sleep=lambda s: None)
    assert again == {"exists": 2, "weekend": 2} and len(calls) == 2


def test_a_holiday_or_html_page_is_a_named_miss(tmp_path):
    def boom(url):
        raise OSError("404")
    assert bf.fetch_day(date(2024, 1, 26), ["NIFTY"], tmp_path, boom) == "missing"
    assert bf.fetch_day(date(2024, 1, 26), ["NIFTY"], tmp_path, lambda u: b"<html>") == "missing"
    assert not list(tmp_path.iterdir())

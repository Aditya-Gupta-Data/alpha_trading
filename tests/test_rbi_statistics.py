"""Hermetic tests for src/ingestion/alt_data/rbi_statistics.py (RBI Payment System Indicators)."""
from __future__ import annotations

import csv
import io
import json

import pytest

from src.ingestion.alt_data.rbi_statistics import (
    RbiPaymentSystemsScraper,
    clean_metric,
    month_key,
    parse_part_one,
    parse_release_index,
)

INDEX_HTML = """
<table class="tablebg"><tr><td class="tableheader"><b>September - 2026</b></td></tr>
<tr><td><a class="link2" href=PSIUserView.aspx?Id=64>Payment System Indicators - September 2026</a></td>
<td><a href="https://rbidocs.rbi.org.in/rdocs/PSI/DOCs/PSI09.XLSX">xlsx</a></td></tr>
<tr><td><a class="link2" href=PSIUserView.aspx?Id=63>Payment System Indicators - August 2026</a></td></tr>
<tr><td><a class="link2" href=PSIUserView.aspx?Id=62>Payment System Indicators - July 2026</a></td></tr></table>
"""


def _release(prev_year, prev, cur, upi_prev, upi_cur):
    """Part I table as the live page lays it out (verified 2026-11-03)."""
    py, pm = prev.split()
    cy, cm = cur.split()
    return f"""
<table>
<tr><td>PART I - Payment System Indicators - Payment &amp; Settlement System Statistics</td></tr>
<tr><td></td><td>Volume (lakh)</td><td>Value (&#8377; crore)</td></tr>
<tr><td>FY 2025-26</td><td>{prev_year}</td><td>{py}</td><td>{cy}</td><td>FY 2025-26</td><td>{prev_year}</td><td>{py}</td><td>{cy}</td></tr>
<tr><td>{pm}</td><td>{cm}</td><td>{pm}</td><td>{cm}</td></tr>
<tr><td>1</td><td>2</td><td>3</td><td>4</td><td>1</td><td>2</td><td>3</td><td>4</td></tr>
<tr><td>A. Settlement Systems</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>2.3 IMPS</td><td>49431.09</td><td>3943.79</td><td>3599.42</td><td>3540.80</td><td>7669929</td><td>596847</td><td>703724</td><td>705912</td></tr>
<tr><td>2.6 UPI @</td><td>2416168.86</td><td>196334.33</td><td>{upi_prev}</td><td>{upi_cur}</td><td>31423250</td><td>2489737</td><td>2982356</td><td>2937397</td></tr>
<tr><td>Total Retail Payments (2+3+4+5)</td><td>2819536.40</td><td>230018.06</td><td>281472.45</td><td>275442.18</td><td>104878484</td><td>8612885</td><td>9470546</td><td>9691383</td></tr>
<tr><td>Note: @ New inclusion</td></tr>
</table>
<table><tr><td>PART III - Payment Infrastructures</td></tr></table>
"""


def test_helpers():
    assert month_key("2025 September") == "2025-09"
    assert month_key("September 2026") == "2026-09"
    assert month_key("FY 2025-26") is None
    assert clean_metric("2.6 UPI @") == "2.6 UPI"
    assert clean_metric("Total Retail Payments (2+3+4+5)") == "Total Retail Payments"
    assert parse_release_index(INDEX_HTML) == [
        {"id": 64, "release": "2026-09"}, {"id": 63, "release": "2026-08"}, {"id": 62, "release": "2026-07"}]


def test_part_one_gives_three_monthly_columns_and_skips_fy_totals():
    rows = parse_part_one(_release("2025 September", "2026 August", "2026 September", "245089.58", "240683.80"))
    upi = {r["month"]: r for r in rows if r["metric"] == "2.6 UPI"}
    assert sorted(upi) == ["2025-09", "2026-08", "2026-09"]
    assert upi["2026-09"]["volume_lakh"] == pytest.approx(240683.80)
    assert upi["2026-09"]["value_crore"] == pytest.approx(2937397)
    assert all(r["volume_lakh"] != pytest.approx(2416168.86) for r in rows)  # FY-to-date column dropped
    assert not any(r["metric"].startswith(("A.", "Note")) for r in rows)


def test_scraper_reads_newest_releases_and_newest_revision_wins(tmp_path):
    pages = {
        "https://www.rbi.org.in/Scripts/PSIUserView.aspx": INDEX_HTML,
        "https://www.rbi.org.in/Scripts/PSIUserView.aspx?Id=64":
            _release("2025 September", "2026 August", "2026 September", "245100.00", "240683.80"),
        "https://www.rbi.org.in/Scripts/PSIUserView.aspx?Id=63":
            _release("2025 August", "2026 July", "2026 August", "236583.53", "245089.58"),
    }
    fetched = []

    def fetch(url, **kw):
        fetched.append(url)
        return pages[url]

    res = RbiPaymentSystemsScraper(lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl",
                                   fetch_fn=fetch).run()
    assert res["ok"] is True
    assert not any("rbidocs" in u for u in fetched) and not any("Id=62" in u for u in fetched)
    rows = list(csv.DictReader(io.StringIO((tmp_path / "lake" / "rbi_payment_systems.csv").read_text())))
    by = {(r["month"], r["metric"]): r for r in rows}
    aug = by[("2026-08", "2.6 UPI")]
    assert float(aug["volume_lakh"]) == pytest.approx(245100.00) and aug["release_id"] == "64"
    sep = by[("2026-09", "2.6 UPI")]
    assert float(sep["avg_ticket_inr"]) == pytest.approx(2937397 * 1e7 / (240683.80 * 1e5), abs=0.01)
    assert ("2026-07", "2.6 UPI") in by and ("2025-08", "2.6 UPI") in by


def test_scraper_degrades_when_the_layout_changes(tmp_path):
    def fetch(url, **kw):
        return INDEX_HTML if url.endswith(".aspx") else "<table><tr><td>Something else</td></tr></table>"

    res = RbiPaymentSystemsScraper(lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl",
                                   fetch_fn=fetch).run()
    assert res["status"] == "degraded" and res["reason"] == "empty_parse"
    codes = [json.loads(ln)["code"] for ln in (tmp_path / "alt_data.jsonl").read_text().splitlines()]
    assert "PSI-LAYOUT" in codes

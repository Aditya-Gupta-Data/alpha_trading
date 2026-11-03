"""Hermetic unit tests for the AltData Fleet (`base_scraper`, `vahan_dashboard`, `gem_tenders`)."""

import csv
import io
import json
import urllib.error
from pathlib import Path

import pytest

from src.ingestion.alt_data import (
    BaseAltDataScraper,
    GemTendersScraper,
    VahanDashboardScraper,
    classify_fuel_type,
    normalize_maker,
    parse_bid_value_inr,
)
from src.ingestion.alt_data.gem_tenders import match_listed_ticker

VAHAN_LANDING_HTML = """
<html>
  <body>
    <form id="vahanForm">
      <input type="hidden" name="javax.faces.ViewState" value="-8472910384:192837465" />
    </form>
  </body>
</html>
"""

VAHAN_PRIMEFACES_XML = """<?xml version='1.0' encoding='UTF-8'?>
<partial-response>
  <changes>
    <update id="combTablePnl"><![CDATA[
      <table id="combTable">
        <thead>
          <tr>
            <th>S.No</th>
            <th>Maker</th>
            <th>ELECTRIC(BOV)</th>
            <th>PETROL</th>
            <th>DIESEL</th>
            <th>CNG ONLY</th>
            <th>PETROL/HYBRID</th>
            <th>TOTAL</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>1</td>
            <td>MARUTI SUZUKI INDIA LTD</td>
            <td>0</td>
            <td>3,200</td>
            <td>0</td>
            <td>1,150</td>
            <td>450</td>
            <td>4,800</td>
          </tr>
          <tr>
            <td>2</td>
            <td>TATA MOTORS PASSENGER VEHICLES LTD</td>
            <td>120</td>
            <td>980</td>
            <td>320</td>
            <td>400</td>
            <td>0</td>
            <td>1,820</td>
          </tr>
          <tr>
            <td>3</td>
            <td>TATA PASSENGER ELECTRIC MOBILITY LTD</td>
            <td>530</td>
            <td>0</td>
            <td>0</td>
            <td>0</td>
            <td>0</td>
            <td>530</td>
          </tr>
          <tr>
            <td>4</td>
            <td>MAHINDRA &amp; MAHINDRA LIMITED</td>
            <td>85</td>
            <td>410</td>
            <td>1,490</td>
            <td>0</td>
            <td>0</td>
            <td>1,985</td>
          </tr>
          <tr>
            <td>5</td>
            <td>MAHINDRA LAST MILE MOBILITY LTD</td>
            <td>215</td>
            <td>0</td>
            <td>40</td>
            <td>0</td>
            <td>0</td>
            <td>255</td>
          </tr>
        </tbody>
      </table>
    ]]></update>
  </changes>
</partial-response>
"""

def test_base_scraper_rotating_headers_and_exponential_backoff(tmp_path):
    seen_headers = []
    sleeps = []
    attempts = {"count": 0}

    def flaky_fetch(url, method="GET", data=None, headers=None):
        seen_headers.append(headers or {})
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("remote portal reset connection")
        if attempts["count"] == 2:
            raise urllib.error.HTTPError(
                url, 503, "Service Unavailable", hdrs=None, fp=None
            )
        return VAHAN_PRIMEFACES_XML

    scraper = VahanDashboardScraper(
        lake_dir=tmp_path / "lake",
        log_path=tmp_path / "alt_data.jsonl",
        max_retries=3,
        base_backoff=1.0,
        jitter=0.0,
        fetch_fn=flaky_fetch,
        sleep_fn=sleeps.append,
    )
    res = scraper.run(report_date="2026-11-02")
    assert res["ok"] is True
    assert res["status"] == "ok"
    assert attempts["count"] == 3
    assert sleeps == [1.0, 2.0]
    # User-Agent rotated across attempts
    uas = [h["User-Agent"] for h in seen_headers]
    assert len(set(uas)) == 3


def test_base_scraper_exits_cleanly_when_portal_is_down(tmp_path):
    log_file = tmp_path / "alt_data.jsonl"
    sleeps = []

    def dead_portal(url, **kwargs):
        raise TimeoutError("MoRTH Vahan gateway timed out")

    scraper = VahanDashboardScraper(
        lake_dir=tmp_path / "lake",
        log_path=log_file,
        max_retries=3,
        base_backoff=0.5,
        jitter=0.0,
        fetch_fn=dead_portal,
        sleep_fn=sleeps.append,
    )
    res = scraper.run(report_date="2026-11-02")
    assert res["ok"] is False
    assert res["status"] == "degraded"
    assert "TimeoutError" in res["reason"]
    assert res["rows_added"] == 0
    assert sleeps == [0.5, 1.0]
    assert log_file.is_file()
    logged = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert any(entry["code"] == "PORTAL-DOWN" for entry in logged)


def test_vahan_scraper_jsf_handshake_and_maker_fuel_grouping(tmp_path):
    calls = []

    def mock_vahan_portal(url, method="GET", data=None, headers=None):
        calls.append((method, data))
        if method == "GET":
            return VAHAN_LANDING_HTML
        assert data["javax.faces.ViewState"] == "-8472910384:192837465"
        assert data["xaxisVar"] == "Maker"
        assert data["yaxisVar"] == "Fuel"
        return VAHAN_PRIMEFACES_XML

    scraper = VahanDashboardScraper(
        lake_dir=tmp_path / "lake",
        log_path=tmp_path / "alt_data.jsonl",
        only_core_makers=True,
        fetch_fn=mock_vahan_portal,
    )
    res = scraper.run(report_date="2026-11-02")
    assert res["ok"] is True
    assert res["rows_added"] == 6  # 3 makers (M&M, Maruti, Tata) x 2 fuel types (EV, ICE)
    assert [c[0] for c in calls] == ["GET", "POST"]

    csv_file = tmp_path / "lake" / "vahan_registrations.csv"
    rows = list(csv.DictReader(io.StringIO(csv_file.read_text())))
    by_maker_fuel = {(r["maker"], r["fuel_type"]): int(r["registrations"]) for r in rows}

    # Maruti: EV = 0, ICE = 3200 (Petrol) + 1150 (CNG) + 450 (Hybrid) = 4800
    assert by_maker_fuel[("Maruti", "EV")] == 0
    assert by_maker_fuel[("Maruti", "ICE")] == 4800

    # Tata: EV = 120 + 530 = 650, ICE = 980 + 320 + 400 = 1700
    assert by_maker_fuel[("Tata", "EV")] == 650
    assert by_maker_fuel[("Tata", "ICE")] == 1700

    # M&M: EV = 85 + 215 = 300, ICE = 410 + 1490 + 40 = 1940
    assert by_maker_fuel[("M&M", "EV")] == 300
    assert by_maker_fuel[("M&M", "ICE")] == 1940

    # Re-running the same day is 100% idempotent (0 rows added, no duplicates)
    second = scraper.run(report_date="2026-11-02")
    assert second["ok"] is True
    assert second["rows_added"] == 0
    assert second["rows_total"] == 6


def test_vahan_json_payload_and_helpers():
    assert normalize_maker("TATA MOTORS LTD") == ("Tata", "TATAMOTORS.NS")
    assert normalize_maker("MAHINDRA & MAHINDRA") == ("M&M", "M&M.NS")
    assert normalize_maker("MARUTI SUZUKI INDIA") == ("Maruti", "MARUTI.NS")
    assert classify_fuel_type("ELECTRIC(BOV)") == "EV"
    assert classify_fuel_type("PETROL/CNG") == "ICE"

    payload = {
        "date": "2026-11-02",
        "records": [
            {"maker": "Tata Motors", "fuel": "PURE EV", "registrations": 610},
            {"maker": "Tata Motors", "fuel": "DIESEL", "registrations": 1400},
        ],
    }
    scraper = VahanDashboardScraper()
    parsed = scraper.parse(payload)
    assert parsed == [
        {"date": "2026-11-02", "maker": "Tata", "ticker": "TATAMOTORS.NS", "fuel_type": "EV", "registrations": 610},
        {"date": "2026-11-02", "maker": "Tata", "ticker": "TATAMOTORS.NS", "fuel_type": "ICE", "registrations": 1400},
    ]


GEM_LISTING_PAGE = "<script>var d = {'csrf_bd_gem_nk': 'abc123'};</script>"

GEM_RESULT_HTML = """
<h4>3. FINANCIAL EVALUATION</h4><p>List of Sellers Qualified Financially</p>
<table><tr><th>S.No.</th><th>Seller Name</th><th>Offered Item</th><th>Total Price</th><th>Rank</th></tr>
<tr><td>1</td><td>LARSEN &amp; TOUBRO LIMITED (MII) Under PMA</td><td>Item Categories : Radar</td><td>` 152000000.00</td><td>L1</td></tr>
<tr><td>2</td><td>SHREE BEL TRADERS (MSE,MII) Under PMA</td><td>Item Categories : Radar</td><td>` 158000000.00 (Bid Price)</td><td>L2</td></tr>
</table>
"""


def _gem_doc(bid_no, b_id, end="2026-10-30T17:00:00Z"):
    return {"b_bid_number": [bid_no], "b_id": [b_id], "final_end_date_sort": [end],
            "b_category_name": ["Coastal Surveillance Radar"], "ba_official_details_minName": ["Ministry of Defence"],
            "ba_official_details_deptName": ["Department of Defence Production"]}


def test_gem_scraper_reads_awarded_high_value_bids_and_their_l1(tmp_path):
    assert match_listed_ticker("SHREE BEL TRADERS") == ""
    assert match_listed_ticker("BEL") == "BEL.NS"
    assert match_listed_ticker("Bharat Electronics Limited") == "BEL.NS"
    assert parse_bid_value_inr("` 152000000.00") == pytest.approx(152_000_000.0)
    calls, sleeps = [], []
    listing = {1: [_gem_doc("GEM/2026/B/1", 11), _gem_doc("GEM/2026/R/2", 22)], 2: []}

    def fetch(url, method="GET", data=None, headers=None):
        calls.append((method, url, data))
        if url.endswith("/all-bids"):
            return GEM_LISTING_PAGE
        if url.endswith("/all-bids-data"):
            payload = json.loads(data["payload"])
            assert data["csrf_bd_gem_nk"] == "abc123"
            assert payload["filter"]["byStatus"] == "bid_awarded" and payload["filter"]["highBidValue"] == "highValue"
            return json.dumps({"code": 200, "response": {"response": {"docs": listing[payload["page"]]}}})
        if url.endswith("/22"):
            return "<html>no ranking yet</html>"
        return GEM_RESULT_HTML

    scraper = GemTendersScraper(lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl",
                                fetch_fn=fetch, sleep_fn=sleeps.append)
    res = scraper.run()
    assert res["ok"] is True and res["rows_added"] == 1
    assert sleeps == [1.0]
    (row,) = list(csv.DictReader(io.StringIO((tmp_path / "lake" / "gem_tenders.csv").read_text())))
    assert row["bid_number"] == "GEM/2026/B/1" and row["date"] == "2026-10-30"
    assert row["winning_bidder"] == "LARSEN & TOUBRO LIMITED" and row["ticker"] == "LT.NS"
    assert float(row["bid_value"]) == pytest.approx(152_000_000.0)
    assert row["ministry"] == "Ministry of Defence" and row["sellers"] == "2"
    codes = [json.loads(ln)["code"] for ln in (tmp_path / "alt_data.jsonl").read_text().splitlines()]
    assert codes == ["NO-L1"]

    calls.clear()
    scraper.run()
    assert not any("/getBidResultView/11" in c[1] for c in calls)  # known bid is not re-fetched


def test_gem_scraper_degrades_without_a_csrf_token(tmp_path):
    scraper = GemTendersScraper(lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl",
                                fetch_fn=lambda url, **kw: "<html></html>")
    res = scraper.run()
    assert res["status"] == "degraded" and "csrf" in res["reason"]

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

GEM_HTML_TABLE = """
<html>
  <body>
    <table class="table table-bordered">
      <thead>
        <tr>
          <th>Bid Number</th>
          <th>Award Date</th>
          <th>Item Category</th>
          <th>Winning Bidder</th>
          <th>Bid Value</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>GEM/2026/B/9001122</td>
          <td>2026-11-02</td>
          <td>Electric Buses - 12 Meter AC</td>
          <td>TATA MOTORS LIMITED</td>
          <td>₹ 42.50 Crore</td>
        </tr>
        <tr>
          <td>GEM/2026/B/9001188</td>
          <td>2026-11-02</td>
          <td>Radar Signal Processing Units</td>
          <td>BHARAT ELECTRONICS LIMITED</td>
          <td>₹ 1,85,00,000</td>
        </tr>
        <tr>
          <td>GEM/2026/B/9001245</td>
          <td>2026-11-02</td>
          <td>Light Utility Vehicles 4x4</td>
          <td>MAHINDRA &amp; MAHINDRA LTD</td>
          <td>Rs. 95.4 Lakh</td>
        </tr>
      </tbody>
    </table>
  </body>
</html>
"""

GEM_HTML_CARDS = """
<div class="bid-card">
  <span>Bid No: GEM/2026/B/7712300</span> |
  <span>Item Category: Solar Power Conditioning Unit</span> |
  <span>Winning Bidder: BHARAT HEAVY ELECTRICALS LIMITED</span> |
  <span>Bid Value: ₹ 12.25 Crore</span>
</div>
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


def test_gem_tenders_scraper_parses_html_tables_cards_and_json_idempotently(tmp_path):
    assert parse_bid_value_inr("₹ 42.50 Crore") == pytest.approx(425_000_000.0)
    assert parse_bid_value_inr("₹ 1,85,00,000") == pytest.approx(18_500_000.0)
    assert parse_bid_value_inr("Rs. 95.4 Lakh") == pytest.approx(9_540_000.0)

    scraper = GemTendersScraper(
        lake_dir=tmp_path / "lake",
        log_path=tmp_path / "alt_data.jsonl",
        fetch_fn=lambda url, **kw: GEM_HTML_TABLE,
    )
    res = scraper.run(award_date="2026-11-02")
    assert res["ok"] is True
    assert res["rows_added"] == 3

    csv_file = tmp_path / "lake" / "gem_tenders.csv"
    rows = list(csv.DictReader(io.StringIO(csv_file.read_text())))
    assert len(rows) == 3
    by_bid = {r["bid_number"]: r for r in rows}

    assert by_bid["GEM/2026/B/9001122"]["winning_bidder"] == "TATA MOTORS LIMITED"
    assert by_bid["GEM/2026/B/9001122"]["ticker"] == "TATAMOTORS.NS"
    assert float(by_bid["GEM/2026/B/9001122"]["bid_value"]) == pytest.approx(425_000_000.0)
    assert by_bid["GEM/2026/B/9001122"]["item_category"] == "Electric Buses - 12 Meter AC"

    assert by_bid["GEM/2026/B/9001188"]["ticker"] == "BEL.NS"
    assert float(by_bid["GEM/2026/B/9001188"]["bid_value"]) == pytest.approx(18_500_000.0)

    # Re-run is idempotent
    repeat = scraper.run(award_date="2026-11-02")
    assert repeat["rows_added"] == 0
    assert repeat["rows_total"] == 3

    # Card-layout HTML and JSON also parse cleanly
    card_rows = scraper.parse(GEM_HTML_CARDS, award_date="2026-11-02")
    assert len(card_rows) == 1
    assert card_rows[0]["winning_bidder"] == "BHARAT HEAVY ELECTRICALS LIMITED"
    assert card_rows[0]["ticker"] == "BHEL.NS"
    assert float(card_rows[0]["bid_value"]) == pytest.approx(122_500_000.0)
    assert card_rows[0]["item_category"] == "Solar Power Conditioning Unit"

    json_rows = scraper.parse(
        {
            "bids": [
                {
                    "bid_number": "GEM/2026/B/9990001",
                    "award_date": "2026-11-02",
                    "winning_bidder": "LARSEN & TOUBRO LIMITED",
                    "bid_value": "15.2 Crore",
                    "item_category": "GIS Substation Package",
                }
            ]
        }
    )
    assert len(json_rows) == 1
    assert json_rows[0]["ticker"] == "LT.NS"
    assert float(json_rows[0]["bid_value"]) == pytest.approx(152_000_000.0)

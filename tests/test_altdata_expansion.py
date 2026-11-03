"""Hermetic unit tests for the AltData Expansion Fleet (`quick_commerce`, `agmarknet_mandi`, `grid_india`, `npci_upi`, and `scripts/run_altdata_fleet.py`)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

import scripts.run_altdata_fleet as fleet_runner
from src.ingestion.alt_data import RbiPaymentSystemsScraper
from src.ingestion.alt_data import (
    AgmarknetMandiScraper,
    GemTendersScraper,
    GridIndiaScraper,
    NpciUpiScraper,
    QuickCommerceScraper,
    VahanDashboardScraper,
    classify_staple_category,
    compute_avg_ticket_size_inr,
    normalize_agri_commodity,
    normalize_grid_region,
    parse_power_gw,
)

BLINKIT_MOCK_PAYLOADS = {
    "wheat": {
        "products": [
            {"name": "Aashirvaad Shudh Chakki Atta 5kg", "selling_price": 252.0},
            {"name": "Pillsbury Chakki Fresh Wheat Atta 5kg", "selling_price": 264.0},
        ]
    },
    "cooking": {
        "products": [
            {"name": "Fortune Sunlite Refined Sunflower Oil 1L", "selling_price": 159.5},
        ]
    },
    "milk": {
        "products": [
            {"name": "Amul Taaza Toned Milk 1L", "selling_price": 69.3},
        ]
    },
    "detergent": {
        "products": [
            {"name": "Surf Excel Easy Wash Detergent 1kg", "selling_price": 132.0},
        ]
    },
}

ZEPTO_MOCK_PAYLOADS = {
    "wheat": {
        "storeProducts": [
            {
                "product": {"name": "Aashirvaad Select Sharbati Atta 5kg"},
                "discountedSellingPrice": 26400,
                "currency_unit": "paise",
            }
        ]
    },
    "cooking": {
        "storeProducts": [
            {
                "product": {"name": "Saffola Gold Cooking Oil 1L"},
                "discountedSellingPrice": 15225,
                "currency_unit": "paise",
            }
        ]
    },
    "milk": {
        "storeProducts": [
            {
                "product": {"name": "Mother Dairy Toned Milk 1L"},
                "sellingPrice": 6600,
                "currency_unit": "paise",
            }
        ]
    },
    "detergent": {
        "storeProducts": [
            {
                "product": {"name": "Tata Tea Gold & Parle Biscuit Combo"},
                "sellingPrice": 12600,
                "currency_unit": "paise",
            }
        ]
    },
}

NPCI_UPI_ECOSYSTEM_HTML = """
<html>
  <body>
    <table class="upi-stats-table">
      <thead>
        <tr>
          <th>Month</th>
          <th>Total Volume (in Mn)</th>
          <th>Total Value (in Cr.)</th>
          <th>P2P Volume (in Mn)</th>
          <th>P2P Value (in Cr.)</th>
          <th>P2M Volume (in Mn)</th>
          <th>P2M Value (in Cr.)</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Oct-2026</td>
          <td>16,580.00</td>
          <td>23,490,000.00</td>
          <td>6,280.00</td>
          <td>17,310,000.00</td>
          <td>10,300.00</td>
          <td>6,180,000.00</td>
        </tr>
        <tr>
          <td>2026-11-02</td>
          <td>560.00</td>
          <td>78,500.00</td>
          <td>210.00</td>
          <td>56,800.00</td>
          <td>350.00</td>
          <td>21,700.00</td>
        </tr>
      </tbody>
    </table>
  </body>
</html>
"""


def test_quick_commerce_scraper_computes_basket_indices_and_custom_cpi(tmp_path):
    assert classify_staple_category("Aashirvaad Atta 5kg") == "Wheat"
    assert classify_staple_category("Fortune Sunflower Oil") == "Cooking Oil"
    assert classify_staple_category("Amul Taaza Toned Milk") == "Milk"
    assert classify_staple_category("Surf Excel Detergent") == "FMCG"

    def mock_qc_fetch(url, method="GET", data=None, headers=None):
        u = url.lower()
        source = BLINKIT_MOCK_PAYLOADS if "blinkit.com" in u else ZEPTO_MOCK_PAYLOADS
        for key, payload in source.items():
            if key in u:
                return json.dumps(payload)
        return json.dumps({"products": []})

    scraper = QuickCommerceScraper(
        lake_dir=tmp_path / "lake",
        log_path=tmp_path / "alt_data.jsonl",
        fetch_fn=mock_qc_fetch,
    )
    res = scraper.run(report_date="2026-11-02")
    assert res["ok"] is True
    assert res["rows_added"] == 8  # 2 platforms (blinkit, zepto) x 4 staple categories

    csv_path = tmp_path / "lake" / "quick_commerce_cpi.csv"
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    by_plat_cat = {(r["platform"], r["category"]): r for r in rows}

    # Blinkit checks:
    # Wheat avg = (252 + 264)/2 = 258.0 vs baseline 240.0 -> index 107.50 (weight 0.30)
    # Cooking Oil = 159.5 vs baseline 145.0 -> index 110.00 (weight 0.25)
    # Milk = 69.3 vs baseline 66.0 -> index 105.00 (weight 0.25)
    # FMCG = 132.0 vs baseline 120.0 -> index 110.00 (weight 0.20)
    # Weighted Custom CPI = 0.30*107.5 + 0.25*110.0 + 0.25*105.0 + 0.20*110.0 = 108.00
    b_wheat = by_plat_cat[("blinkit", "Wheat")]
    assert float(b_wheat["avg_price_inr"]) == pytest.approx(258.0)
    assert float(b_wheat["category_index"]) == pytest.approx(107.5)
    assert float(b_wheat["custom_cpi"]) == pytest.approx(108.0)
    assert float(b_wheat["inflation_pct"]) == pytest.approx(8.0)
    assert int(b_wheat["sample_size"]) == 2

    # Zepto checks (paise converted to INR):
    # Wheat = 264.0 vs 240.0 -> index 110.00 (weight 0.30)
    # Cooking Oil = 152.25 vs 145.0 -> index 105.00 (weight 0.25)
    # Milk = 66.0 vs 66.0 -> index 100.00 (weight 0.25)
    # FMCG = 126.0 vs 120.0 -> index 105.00 (weight 0.20)
    # Weighted Custom CPI = 0.30*110.0 + 0.25*105.0 + 0.25*100.0 + 0.20*105.0 = 105.25
    z_oil = by_plat_cat[("zepto", "Cooking Oil")]
    assert float(z_oil["avg_price_inr"]) == pytest.approx(152.25)
    assert float(z_oil["category_index"]) == pytest.approx(105.0)
    assert float(z_oil["custom_cpi"]) == pytest.approx(105.25)
    assert float(z_oil["inflation_pct"]) == pytest.approx(5.25)


def _agm_page(records, next_page=None):
    """One `dashboard-data/` page as the live API returns it (shape verified 2026-11-03)."""
    return json.dumps({"status": "success", "message": "Data fetched successfully.",
                       "pagination": {"next_page": next_page}, "data": {"columns": [], "records": records}})


def _agm_rec(cmdt, state, as_on, one_yr=None):
    return {"cmdt_name": cmdt, "state_name": state, "as_on": as_on, "one_yr_ago": one_yr,
            "unit_name_price": "Rs./Quintal"}


def test_agmarknet_scraper_reads_the_statewise_dashboard_one_day_per_call(tmp_path):
    assert normalize_agri_commodity("Onion") == "Onions"
    assert normalize_agri_commodity("Onion Green") is None
    assert normalize_agri_commodity("Wheat Atta") is None
    bodies = []

    def fetch(url, method="GET", data=None, headers=None):
        body = json.loads(data)
        bodies.append(body)
        if body["from_date"] == "2026-11-02" and body["page"] == 1:
            return _agm_page([_agm_rec("Onion", "Maharashtra", "2150.00", "3100.00"),
                              _agm_rec("Onion Green", "Maharashtra", "900.00"),
                              _agm_rec("Tomato", "Karnataka", None)], next_page="p2")
        if body["from_date"] == "2026-11-02":
            return _agm_page([_agm_rec("Wheat", "Gujarat", "3,110.04")])
        return json.dumps({"status": False, "message": "No data found", "data": []})

    scraper = AgmarknetMandiScraper(
        lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl", fetch_fn=fetch,
        days_per_run=2, today_fn=lambda: __import__("datetime").date(2026, 11, 3),
    )
    res = scraper.run()
    assert res["ok"] is True and res["rows_added"] == 2
    first = bodies[0]
    assert first["dashboard"] == "statewise_price_arrival_sp" and "captcha" not in first
    assert sorted(first["commodity"]) == [1, 23, 44, 65]
    assert first["from_date"] == first["to_date"] == "2026-11-03"

    rows = list(csv.DictReader(io.StringIO((tmp_path / "lake" / "agmarknet_mandi.csv").read_text())))
    by = {(r["commodity"], r["state"]): r for r in rows}
    onion = by[("Onions", "Maharashtra")]
    assert onion["date"] == "2026-11-02" and onion["mandi"] == "STATE_WEIGHTED_AVG"
    assert float(onion["modal_price_quintal"]) == pytest.approx(2150.0)
    assert float(onion["modal_price_kg"]) == pytest.approx(21.5)
    assert float(onion["price_1y_ago_quintal"]) == pytest.approx(3100.0)
    assert float(by[("Wheat", "Gujarat")]["modal_price_quintal"]) == pytest.approx(3110.04)
    assert ("Tomatoes", "Karnataka") not in by  # no price that day: skipped, not filled


def test_agmarknet_scraper_degrades_when_the_api_refuses(tmp_path):
    scraper = AgmarknetMandiScraper(
        lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl", days_per_run=1,
        fetch_fn=lambda url, **kw: json.dumps({"detail": "x", "status": False, "message": "Invalid dashboard type"}),
    )
    res = scraper.run()
    assert res["status"] == "degraded" and "Invalid dashboard type" in res["reason"]


def _psp_rows(nr_energy, nr_met=61715.0):
    """Section A of an NLDC daily PSP sheet, as xlrd returns it (layout verified 2026-11-03)."""
    return [
        ["", "", "Report for previous day", "", "Date of Reporting:", "03-Nov-2026"],
        ["A. Power Supply Position at All India and Regional level"],
        ["", "NR", "WR", "SR", "ER", "NER", "TOTAL"],
        ["Demand Met (MW) during Evening Peak hr (19:00hrs)\u00b9", nr_met, 69808.0, 51258.0, 26568.0, 3459.0, 212808.0],
        ["Shoratge (MW) during Evening Peak hr (19:00hrs)\u00b9", 0.0, 0.0, 0.0, 0.0, 36.0, 36.0],
        ["Energy Met (MU)", nr_energy, 1645.2, 1292.04, 558.11, 63.96, 4775.31],
        ["Hydro Gen (MU)", 135.85, 49.23, 62.03, 47.41, 27.79, 322.31],
        ["Wind Gen (MU)", 4.72, 77.39, 44.75, "-", "-", 126.86],
        ["Solar Gen (MU)*", 243.47, 234.72, 144.72, 3.46, 1.46, 627.83],
        ["Energy Shortage (MU)", 0.62, 0.0, 0.0, 0.1, 0.05, 0.77],
        ["Maximum Demand Met During the Day (MW)\n(From NLDC SCADA)", 62942.0, 82933.0, 63571.0, 27356.0, 3579.0, 226478.0],
        ["B. Frequency Profile (%)"],
        ["Region", "FVI", "< 49.7"],
    ]


def test_grid_india_scraper_reads_psp_files_from_the_file_api(tmp_path):
    assert normalize_grid_region("Northern Region") == "NR"
    assert normalize_grid_region("All India") == "National"
    assert parse_power_gw("239,300 MW") == pytest.approx(239.3)
    listing = {"retData": [
        {"Title_": "02.11.26_NLDC_PSP", "FilePath": "files/grdw/2026/11/02.11.26_NLDC_PSP_993.xls"},
        {"Title_": "01.11.26_NLDC_PSP", "FilePath": "files/grdw/2026/11/01.11.26_NLDC_PSP_491.xls"},
        {"Title_": "not a psp", "FilePath": "files/x.pdf"},
    ]}
    calls = []

    def fetch(url, method="GET", data=None, headers=None):
        calls.append((method, url, data))
        if url.endswith("/api/v1/file"):
            return json.dumps(listing)
        return url.encode()

    sheets = {
        "https://webcdn.grid-india.in/files/grdw/2026/11/02.11.26_NLDC_PSP_993.xls": _psp_rows(1216.0),
        "https://webcdn.grid-india.in/files/grdw/2026/11/01.11.26_NLDC_PSP_491.xls": _psp_rows(1280.0),
    }
    scraper = GridIndiaScraper(
        lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl",
        fetch_fn=fetch, xls_reader=lambda data: sheets[data.decode()],
    )
    res = scraper.run()
    assert res["ok"] is True and res["rows_added"] == 12
    assert calls[0][0] == "POST" and json.loads(calls[0][2])["_type"] == "DAILY_PSP_REPORT"

    rows = list(csv.DictReader(io.StringIO((tmp_path / "lake" / "grid_india_power.csv").read_text())))
    by = {(r["date"], r["region"]): r for r in rows}
    nat = by[("2026-11-02", "National")]
    assert float(nat["demand_met_gw"]) == pytest.approx(212.808)
    assert float(nat["peak_demand_gw"]) == pytest.approx(212.844)
    assert float(nat["max_demand_met_gw"]) == pytest.approx(226.478)
    assert float(nat["solar_mu"]) == pytest.approx(627.83)
    assert by[("2026-11-02", "ER")]["wind_mu"] == ""
    nr = by[("2026-11-02", "NR")]
    assert float(nr["load_drop_pct"]) == pytest.approx((1216.0 - 1280.0) / 1280.0 * 100, abs=0.01)
    assert nr["load_drop_flag"] == "true"
    assert by[("2026-11-01", "NR")]["load_drop_pct"] == ""  # no prior day on file: abstain
    assert by[("2026-11-01", "NR")]["source_file"] == "01.11.26_NLDC_PSP_491.xls"


def test_grid_india_scraper_degrades_when_the_sheet_layout_changes(tmp_path):
    listing = {"retData": [{"Title_": "02.11.26_NLDC_PSP", "FilePath": "files/a.xls"}]}
    scraper = GridIndiaScraper(
        lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl",
        fetch_fn=lambda url, **kw: json.dumps(listing) if "api" in url else b"x",
        xls_reader=lambda data: [["Some other report"], ["Region", "Value"]],
    )
    res = scraper.run()
    assert res["status"] == "degraded" and res["reason"] == "empty_parse"
    codes = [json.loads(ln)["code"] for ln in (tmp_path / "alt_data.jsonl").read_text().splitlines()]
    assert "PSP-LAYOUT" in codes


def test_npci_upi_scraper_extracts_p2m_volumes_and_avg_ticket_sizes(tmp_path):
    # ATS = (6,180,000 Cr * 10) / 10,300 Mn = 6000.0 INR (or for 21,700 Cr / 350 Mn = 620.0 INR)
    assert compute_avg_ticket_size_inr(350.0, 21700.0) == pytest.approx(620.0)

    scraper = NpciUpiScraper(
        lake_dir=tmp_path / "lake",
        log_path=tmp_path / "alt_data.jsonl",
        fetch_fn=lambda url, **kw: NPCI_UPI_ECOSYSTEM_HTML,
    )
    res = scraper.run(report_date="2026-11-02")
    assert res["ok"] is True
    assert res["rows_added"] == 2

    csv_path = tmp_path / "lake" / "npci_upi.csv"
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    by_period = {(r["period"], r["granularity"]): r for r in rows}

    daily = by_period[("2026-11-02", "daily")]
    assert float(daily["p2m_volume_mn"]) == pytest.approx(350.0)
    assert float(daily["p2m_value_cr"]) == pytest.approx(21700.0)
    assert float(daily["avg_ticket_size_inr"]) == pytest.approx(620.0)
    assert float(daily["p2m_share_pct"]) == pytest.approx(62.5)

    monthly = by_period[("Oct-2026", "monthly")]
    assert float(monthly["p2m_volume_mn"]) == pytest.approx(10300.0)
    assert float(monthly["p2m_value_cr"]) == pytest.approx(6180000.0)
    assert float(monthly["avg_ticket_size_inr"]) == pytest.approx(6000.0)

    # Verify JSON payload parsing
    json_rows = scraper.parse(
        {
            "statistics": [
                {
                    "period": "2026-11-03",
                    "granularity": "daily",
                    "p2m_volume_mn": 400.0,
                    "p2m_value_cr": 24800.0,
                    "total_volume_mn": 640.0,
                    "total_value_cr": 82000.0,
                }
            ]
        }
    )
    assert len(json_rows) == 1
    assert float(json_rows[0]["avg_ticket_size_inr"]) == pytest.approx(620.0)
    assert float(json_rows[0]["p2m_share_pct"]) == pytest.approx(62.5)


def test_run_altdata_fleet_runs_five_scrapers_without_blocked_sources():
    assert fleet_runner.FLEET_SCRAPERS == (
        VahanDashboardScraper,
        GemTendersScraper,
        AgmarknetMandiScraper,
        GridIndiaScraper,
        RbiPaymentSystemsScraper,
    )
    assert QuickCommerceScraper not in fleet_runner.FLEET_SCRAPERS
    assert NpciUpiScraper not in fleet_runner.FLEET_SCRAPERS


def test_setup_cron_does_not_schedule_the_altdata_fleet():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "setup_cron.sh").read_text()
    cron_lines = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert not any("run_altdata_fleet" in ln for ln in cron_lines)

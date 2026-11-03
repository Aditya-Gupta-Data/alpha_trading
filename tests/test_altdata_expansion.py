"""Hermetic unit tests for the AltData Expansion Fleet (`quick_commerce`, `agmarknet_mandi`, `grid_india`, `npci_upi`, and `scripts/run_altdata_fleet.py`)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

import scripts.run_altdata_fleet as fleet_runner
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

AGMARKNET_GRIDVIEW_HTML = """
<html>
  <body>
    <table id="cphBody_GridPriceData">
      <tr>
        <th>Sl no.</th>
        <th>District Name</th>
        <th>Market Name</th>
        <th>Commodity</th>
        <th>Variety</th>
        <th>Grade</th>
        <th>Min Price (Rs./Quintal)</th>
        <th>Max Price (Rs./Quintal)</th>
        <th>Modal Price (Rs./Quintal)</th>
        <th>Price Date</th>
      </tr>
      <tr>
        <td>1</td>
        <td>Indore</td>
        <td>Indore Mandi</td>
        <td>Wheat</td>
        <td>Lokwan</td>
        <td>FAQ</td>
        <td>2,420</td>
        <td>2,680</td>
        <td>2,550</td>
        <td>02-Nov-2026</td>
      </tr>
      <tr>
        <td>2</td>
        <td>Kolhapur</td>
        <td>Kolhapur APMC</td>
        <td>Sugar</td>
        <td>Medium</td>
        <td>FAQ</td>
        <td>3,780</td>
        <td>3,920</td>
        <td>3,850</td>
        <td>02-Nov-2026</td>
      </tr>
      <tr>
        <td>3</td>
        <td>Kolar</td>
        <td>Kolar Market</td>
        <td>Tomato</td>
        <td>Hybrid</td>
        <td>FAQ</td>
        <td>1,800</td>
        <td>2,400</td>
        <td>2,100</td>
        <td>02-Nov-2026</td>
      </tr>
      <tr>
        <td>4</td>
        <td>Nashik</td>
        <td>Lasalgaon</td>
        <td>Onion</td>
        <td>Red</td>
        <td>FAQ</td>
        <td>2,900</td>
        <td>3,500</td>
        <td>3,200</td>
        <td>02-Nov-2026</td>
      </tr>
    </table>
  </body>
</html>
"""

GRID_INDIA_POSOCO_HTML = """
<html>
  <body>
    <table class="psp-table">
      <thead>
        <tr>
          <th>Region</th>
          <th>Peak Demand (MW)</th>
          <th>Demand Met (MW)</th>
          <th>Shortage (MW)</th>
          <th>Previous Day Met (MW)</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Northern Region</td>
          <td>74,500</td>
          <td>72,000</td>
          <td>2,500</td>
          <td>75,000</td>
        </tr>
        <tr>
          <td>Western Region</td>
          <td>71,000</td>
          <td>70,800</td>
          <td>200</td>
          <td>71,000</td>
        </tr>
        <tr>
          <td>Southern Region</td>
          <td>62,000</td>
          <td>61,900</td>
          <td>100</td>
          <td>62,000</td>
        </tr>
        <tr>
          <td>Eastern Region</td>
          <td>28,000</td>
          <td>27,200</td>
          <td>800</td>
          <td>28,000</td>
        </tr>
        <tr>
          <td>North-Eastern Region</td>
          <td>3,800</td>
          <td>3,750</td>
          <td>50</td>
          <td>3,800</td>
        </tr>
      </tbody>
    </table>
  </body>
</html>
"""

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


def test_agmarknet_mandi_scraper_parses_html_and_json_for_four_staples(tmp_path):
    assert normalize_agri_commodity("Wheat") == "Wheat"
    assert normalize_agri_commodity("Sugar") == "Sugar"
    assert normalize_agri_commodity("Tomato") == "Tomatoes"
    assert normalize_agri_commodity("Onion") == "Onions"

    scraper = AgmarknetMandiScraper(
        lake_dir=tmp_path / "lake",
        log_path=tmp_path / "alt_data.jsonl",
        fetch_fn=lambda url, **kw: AGMARKNET_GRIDVIEW_HTML,
    )
    res = scraper.run(report_date="2026-11-02")
    assert res["ok"] is True
    assert res["rows_added"] == 4

    csv_path = tmp_path / "lake" / "agmarknet_mandi.csv"
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    by_comm = {r["commodity"]: r for r in rows}

    assert set(by_comm.keys()) == {"Wheat", "Sugar", "Tomatoes", "Onions"}
    assert float(by_comm["Wheat"]["modal_price_quintal"]) == pytest.approx(2550.0)
    assert float(by_comm["Wheat"]["modal_price_kg"]) == pytest.approx(25.50)
    assert float(by_comm["Sugar"]["modal_price_quintal"]) == pytest.approx(3850.0)
    assert float(by_comm["Tomatoes"]["modal_price_kg"]) == pytest.approx(21.00)
    assert float(by_comm["Onions"]["modal_price_quintal"]) == pytest.approx(3200.0)

    # Verify JSON payload parsing as well
    json_rows = scraper.parse(
        {
            "date": "2026-11-02",
            "records": [
                {
                    "commodity": "Onion",
                    "state": "Maharashtra",
                    "mandi": "Pimpalgaon",
                    "min_price": 3000,
                    "max_price": 3400,
                    "modal_price": 3250,
                }
            ],
        }
    )
    assert len(json_rows) == 1
    assert json_rows[0]["commodity"] == "Onions"
    assert float(json_rows[0]["modal_price_kg"]) == pytest.approx(32.50)


def test_grid_india_scraper_extracts_national_gw_and_regional_load_drops(tmp_path):
    assert normalize_grid_region("Northern Region") == "NR"
    assert normalize_grid_region("All India") == "National"
    assert parse_power_gw("239,300 MW") == pytest.approx(239.3)
    assert parse_power_gw("241.5 GW") == pytest.approx(241.5)

    scraper = GridIndiaScraper(
        lake_dir=tmp_path / "lake",
        log_path=tmp_path / "alt_data.jsonl",
        fetch_fn=lambda url, **kw: GRID_INDIA_POSOCO_HTML,
    )
    res = scraper.run(report_date="2026-11-02")
    assert res["ok"] is True
    assert res["rows_added"] == 6  # Synthesized National + 5 regional grids (NR, WR, SR, ER, NER)

    csv_path = tmp_path / "lake" / "grid_india_power.csv"
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    by_region = {r["region"]: r for r in rows}

    # National Peak Power Demand = (74.5 + 71.0 + 62.0 + 28.0 + 3.8) = 239.300 GW
    assert float(by_region["National"]["peak_demand_gw"]) == pytest.approx(239.3)
    assert float(by_region["National"]["demand_met_gw"]) == pytest.approx(235.65)
    assert float(by_region["National"]["shortage_gw"]) == pytest.approx(3.65)

    # Northern Region dropped from 75.0 GW to 72.0 GW (-4.00%) -> load_drop_flag = true
    assert float(by_region["NR"]["peak_demand_gw"]) == pytest.approx(74.5)
    assert float(by_region["NR"]["load_drop_pct"]) == pytest.approx(-4.0)
    assert by_region["NR"]["load_drop_flag"] == "true"

    # Eastern Region dropped from 28.0 GW to 27.2 GW (-2.86%) -> load_drop_flag = true
    assert float(by_region["ER"]["load_drop_pct"]) == pytest.approx(-2.86)
    assert by_region["ER"]["load_drop_flag"] == "true"

    # Western Region (-0.28%) -> load_drop_flag = false
    assert by_region["WR"]["load_drop_flag"] == "false"

    # Verify JSON payload parsing
    json_rows = scraper.parse(
        {
            "date": "2026-11-02",
            "national_peak_gw": 242.5,
            "national_demand_met_gw": 241.0,
            "regions": [
                {"region": "WR", "peak_demand_gw": 72.0, "demand_met_gw": 69.0, "baseline_gw": 72.0}
            ],
        }
    )
    by_reg_json = {r["region"]: r for r in json_rows}
    assert float(by_reg_json["National"]["peak_demand_gw"]) == pytest.approx(242.5)
    assert by_reg_json["WR"]["load_drop_flag"] == "true"


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


def test_run_altdata_fleet_runs_five_scrapers_without_quick_commerce():
    assert fleet_runner.FLEET_SCRAPERS == (
        VahanDashboardScraper,
        GemTendersScraper,
        AgmarknetMandiScraper,
        GridIndiaScraper,
        NpciUpiScraper,
    )
    assert QuickCommerceScraper not in fleet_runner.FLEET_SCRAPERS


def test_setup_cron_does_not_schedule_the_altdata_fleet():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "setup_cron.sh").read_text()
    cron_lines = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert not any("run_altdata_fleet" in ln for ln in cron_lines)

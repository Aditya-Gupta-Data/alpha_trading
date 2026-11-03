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

class _EchoScraper(BaseAltDataScraper):
    dataset_name = "echo"
    default_columns = ("date", "value")
    primary_key_columns = ("date",)

    def fetch_raw(self, **kwargs):
        return self.request("https://example.invalid/data").decode()

    def parse(self, raw, **kwargs):
        return [{"date": "2026-11-02", "value": raw}]


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
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", hdrs=None, fp=None)
        return "42"

    scraper = _EchoScraper(
        lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl",
        max_retries=3, base_backoff=1.0, jitter=0.0, fetch_fn=flaky_fetch, sleep_fn=sleeps.append,
    )
    res = scraper.run()
    assert res["ok"] is True and res["status"] == "ok" and res["rows_added"] == 1
    assert attempts["count"] == 3
    assert sleeps == [1.0, 2.0]
    assert len({h["User-Agent"] for h in seen_headers}) == 3


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


def _xlsx(rows):
    """Minimal .xlsx (inline strings) shaped like Vahan's Maker x Fuel export."""
    import zipfile

    def col(i):
        name = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            name = chr(65 + r) + name
        return name

    xml_rows = []
    for r, row in enumerate(rows, 1):
        cells = "".join(f'<c r="{col(j)}{r}" t="inlineStr"><is><t>{v}</t></is></c>' for j, v in enumerate(row))
        xml_rows.append(f'<row r="{r}">{cells}</row>')
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + "".join(xml_rows) + "</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


VAHAN_FUELS = ["CNG ONLY", "DIESEL", "ELECTRIC(BOV)", "NOT APPLICABLE", "PETROL", "PURE EV", "STRONG HYBRID EV"]


def _vahan_sheet(tata_ev, maruti_petrol):
    return _xlsx([
        ["Maker Wise Fuel Data For All State (2026)"],
        ["S No", "\xa0 Maker \xa0", "Fuel ", "", "", "", "", "", "", "\xa0TOTAL\xa0"],
        ["", "", *VAHAN_FUELS, ""],
        ["1", "TATA MOTORS PASSENGER VEHICLES LTD", "100", "200", "0", "0", "900", "0", "0", "1,200"],
        ["2", "TATA PASSENGER ELECTRIC MOBILITY LTD", "0", "0", str(tata_ev), "0", "0", "50", "0", "x"],
        ["3", "TATA HITACHI CONSTRUCTION MACHINERY", "0", "70", "0", "0", "0", "0", "0", "70"],
        ["4", "MARUTI SUZUKI INDIA LTD", "400", "0", "0", "0", f"{maruti_petrol:,}", "0", "30", "x"],
        ["5", "SOME TRAILER WORKS", "0", "0", "0", "9", "0", "0", "0", "9"],
    ])


def _vahan_fetch(sheet, calls):
    def fetch(url, method="GET", data=None, headers=None):
        calls.append((method, data))
        if method == "GET":
            return '<input type="hidden" name="javax.faces.ViewState" value="vs-1" />'
        if data.get("javax.faces.partial.ajax") == "true":
            assert data["yaxisVar_input"] == "Maker" and data["xaxisVar_input"] == "Fuel"
            assert data["selectedYearType_input"] == "C" and data["selectedYear_input"] == "2026"
            return ('<partial-response><changes><update id="combTablePnl"><![CDATA[<div id="groupingTable"></div>]]>'
                    '</update><update id="j_id1:javax.faces.ViewState:0"><![CDATA[vs-2]]></update></changes></partial-response>')
        assert data["groupingTable:xls"] == "groupingTable:xls" and data["javax.faces.ViewState"] == "vs-2"
        return sheet
    return fetch


def test_vahan_scraper_snapshots_ytd_then_writes_daily_deltas(tmp_path):
    import datetime as dt

    lake = tmp_path / "lake"
    day = {"d": dt.date(2026, 11, 2)}

    def make(sheet, calls):
        return VahanDashboardScraper(lake_dir=lake, log_path=tmp_path / "alt_data.jsonl",
                                     fetch_fn=_vahan_fetch(sheet, calls), today_fn=lambda: day["d"])

    calls = []
    first = make(_vahan_sheet(tata_ev=500, maruti_petrol=3000), calls).run()
    assert first["reason"] == "empty_parse"  # first night: snapshot only, no zero rows
    assert [c[0] for c in calls] == ["GET", "POST", "POST"]
    snap = {(r["maker"], r["fuel_type"]): int(r["registrations_ytd"])
            for r in csv.DictReader(io.StringIO((lake / "vahan_registrations_ytd.csv").read_text()))}
    assert snap[("Tata", "EV")] == 550 and snap[("Tata", "ICE")] == 1200  # Hitachi excluded
    assert snap[("Maruti", "ICE")] == 3430  # CNG + petrol + strong hybrid
    assert snap[("ALL", "ICE")] == 100 + 200 + 70 + 900 + 400 + 3000 + 30
    assert not (lake / "vahan_registrations.csv").exists()

    day["d"] = dt.date(2026, 11, 3)
    res = make(_vahan_sheet(tata_ev=530, maruti_petrol=3100), []).run()
    assert res["ok"] is True
    daily = {(r["maker"], r["fuel_type"]): r
             for r in csv.DictReader(io.StringIO((lake / "vahan_registrations.csv").read_text()))}
    assert int(daily[("Tata", "EV")]["registrations"]) == 30
    assert daily[("Tata", "EV")]["ticker"] == "TATAMOTORS.NS"
    assert int(daily[("Maruti", "ICE")]["registrations"]) == 100
    assert int(daily[("Tata", "ICE")]["registrations"]) == 0
    assert daily[("Maruti", "ICE")]["prior_date"] == "2026-11-02" and daily[("Maruti", "ICE")]["days_covered"] == "1"


def test_vahan_scraper_skips_a_ytd_decrease_and_dry_run_writes_nothing(tmp_path):
    import datetime as dt

    lake = tmp_path / "lake"
    for d, ev in ((dt.date(2026, 11, 2), 500), (dt.date(2026, 11, 3), 480)):
        VahanDashboardScraper(lake_dir=lake, log_path=tmp_path / "alt_data.jsonl",
                              fetch_fn=_vahan_fetch(_vahan_sheet(ev, 3000), []), today_fn=lambda d=d: d).run()
    daily = {(r["maker"], r["fuel_type"]) for r in csv.DictReader(io.StringIO((lake / "vahan_registrations.csv").read_text()))}
    assert ("Tata", "EV") not in daily and ("Maruti", "ICE") in daily
    assert "VAHAN-YTD-DOWN" in (tmp_path / "alt_data.jsonl").read_text()

    dry_lake = tmp_path / "dry"
    VahanDashboardScraper(lake_dir=dry_lake, log_path=tmp_path / "dry.jsonl",
                          fetch_fn=_vahan_fetch(_vahan_sheet(500, 3000), []),
                          today_fn=lambda: dt.date(2026, 11, 2)).run(dry_run=True)
    assert not dry_lake.exists()


def test_vahan_helpers():
    assert classify_fuel_type("ELECTRIC(BOV)") == "EV"
    assert classify_fuel_type("SOLAR") == "EV"
    assert classify_fuel_type("PETROL/CNG") == "ICE"
    assert classify_fuel_type("NOT APPLICABLE") is None
    assert normalize_maker("TATA HITACHI CONSTRUCTION MACHINERY") is None
    assert normalize_maker("HERO MOTOCORP LTD") == ("Hero", "HEROMOTOCO.NS")


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

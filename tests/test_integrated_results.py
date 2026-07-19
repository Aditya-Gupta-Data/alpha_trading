"""
The Issue-21 fix clerk, fully offline: XBRL parsing (context-anchored,
lakhs conversion, EPS fallback tag), quarter selection (financials only,
Consolidated preferred, revisions win), fresh-capture assembly in the
financial_results schema, and honest no_data/outage handling.
"""
import json

from src.ingestion import integrated_results as IR

XML = """<xbrl>
<in-capmkt:RevenueFromOperations contextRef="OneD" unitRef="INR">722750000000</in-capmkt:RevenueFromOperations>
<in-capmkt:RevenueFromOperations contextRef="YTD" unitRef="INR">999990000000</in-capmkt:RevenueFromOperations>
<in-capmkt:ProfitLossForPeriod contextRef="OneD">134200000000</in-capmkt:ProfitLossForPeriod>
<in-capmkt:BasicEarningsLossPerShareFromContinuingOperations contextRef="OneD">36.90</in-capmkt:BasicEarningsLossPerShareFromContinuingOperations>
<in-capmkt:FaceValueOfEquityShareCapital contextRef="OneD">1</in-capmkt:FaceValueOfEquityShareCapital>
<in-capmkt:PaidUpValueOfEquityShareCapital contextRef="OneD">3620000000</in-capmkt:PaidUpValueOfEquityShareCapital>
</xbrl>"""


def test_parse_xbrl_anchors_context_and_converts_lakhs():
    f = IR.parse_xbrl(XML)
    assert f["net_sale"] == 7227500.0          # rupees -> lakhs, OneD not YTD
    assert f["net_profit"] == 1342000.0
    assert f["eps_basic"] == 36.9              # per-share, no conversion
    assert f["face_value"] == 1.0
    assert f["paidup_capital"] == 36200.0


def test_parse_xbrl_eps_fallback_tag():
    xml = XML.replace("BasicEarningsLossPerShareFromContinuingOperations",
                      "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
    assert IR.parse_xbrl(xml)["eps_basic"] == 36.9


def _row(qe, consolidated="Consolidated", created="01-Jul-2026 10:00",
         type_="Integrated Filing- Financials"):
    return {"qe_Date": qe, "consolidated": consolidated,
            "creation_Date": created, "type": type_,
            "xbrl": f"https://x/{qe}-{consolidated}.xml"}


def test_select_rows_prefers_consolidated_and_newest_revision():
    listing = {"data": [
        _row("30-JUN-2026", "Standalone"),
        _row("30-JUN-2026", "Consolidated"),
        _row("31-MAR-2026", "Consolidated", created="01-May-2026 09:00"),
        _row("31-MAR-2026", "Consolidated", created="15-May-2026 09:00"),
        _row("30-JUN-2026", type_="Integrated Filing- Governance"),
    ]}
    rows = IR.select_rows(listing)
    assert [r["qe_Date"] for r in rows] == ["30-JUN-2026", "31-MAR-2026"]
    assert rows[0]["consolidated"] == "Consolidated"
    assert rows[1]["creation_Date"] == "15-May-2026 09:00"


def test_fetch_one_assembles_fresh_capture(tmp_path):
    listing = {"data": [_row(q) for q in
                        ("30-JUN-2026", "31-MAR-2026", "31-DEC-2025",
                         "30-SEP-2025", "30-JUN-2025")]}
    r = IR.fetch_one("TCS.NS", fetch_json_fn=lambda u: listing,
                     fetch_bytes_fn=lambda u: XML.encode(),
                     out_dir=tmp_path, log_path=tmp_path / "o.jsonl",
                     sleep_fn=lambda s: None)
    assert r["status"] == "captured" and r["quarters"] == 5
    saved = json.loads((tmp_path / "TCS.json").read_text())
    assert "integrated-filing" in saved["source"]
    p0 = saved["periods"][0]
    assert p0["to"] == "30-JUN-2026"
    assert p0["net_profit_consolidated"] == 1342000.0
    assert p0["net_profit"] is None            # consolidated row
    assert p0["paidup_capital"] == 36200.0


def test_fetch_one_honest_no_data_and_outage(tmp_path):
    r = IR.fetch_one("GHOST", fetch_json_fn=lambda u: {"data": []},
                     fetch_bytes_fn=lambda u: b"", out_dir=tmp_path,
                     log_path=tmp_path / "o.jsonl", sleep_fn=lambda s: None)
    assert r["status"] == "no_data"
    assert "IR-404" in (tmp_path / "o.jsonl").read_text()

    def dead(u):
        raise ConnectionError("HTTP Error 403")
    r2 = IR.fetch_one("X", fetch_json_fn=dead, fetch_bytes_fn=lambda u: b"",
                      out_dir=tmp_path, log_path=tmp_path / "o.jsonl",
                      sleep_fn=lambda s: None)
    assert r2["status"] == "outage" and r2["code"] == "IR-401"

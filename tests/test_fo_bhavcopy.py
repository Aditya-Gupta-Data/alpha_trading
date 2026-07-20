"""
The F&O intake clerk offline: bundle ingest (nested zip, per-day
routing), bhavcopy aggregation (OPTSTK/FUTSTK per symbol, padded
numbers), ban-list parse, tiering (banned excluded from tier1),
newest-day-WITH-bhavcopy selection, and the automated fetch_day
(idempotent, html-error honest miss, zip unwrap).
"""
import io
import json
import zipfile
from datetime import date

from src.ingestion import fo_bhavcopy as FO

FO_CSV = """INSTRUMENT,SYMBOL    ,EXP_DATE  ,OPEN_PRICE ,HI_PRICE   ,LO_PRICE   ,CLOSE_PRICE,OPEN_INT*      ,TRD_VAL           ,TRD_QTY          ,NO_OF_CONT       ,NO_OF_TRADE
OPTSTK    ,BIGLIQ    ,28/07/2026,00000010.00,00000012.00,00000009.00,00000011.00,000000002000000,     5000000000.00,           100000,             5000,             4000
OPTSTK    ,BIGLIQ    ,25/08/2026,00000010.00,00000012.00,00000009.00,00000011.00,000000001000000,     3000000000.00,            50000,             2500,             2000
FUTSTK    ,BIGLIQ    ,28/07/2026,00000500.00,00000510.00,00000490.00,00000505.00,000000000700000,     9000000000.00,            30000,             1500,             1200
OPTSTK    ,THINQ     ,28/07/2026,00000005.00,00000006.00,00000004.00,00000005.50,000000000010000,       10000000.00,             1000,               50,               40
OPTSTK    ,CROWDED   ,28/07/2026,00000008.00,00000009.00,00000007.00,00000008.50,000000003000000,     4000000000.00,            80000,             4000,             3500
OPTIDX    ,NIFTY     ,24/07/2026,00000100.00,00000120.00,00000090.00,00000110.00,000000009000000,    99000000000.00,          9000000,           300000,           250000
"""
SECBAN = "Securities in Ban For Trade Date 20-JUL-2026\n1,CROWDED\n"


def test_parse_fo_aggregates_stock_fo_only():
    agg = FO.parse_fo_csv(FO_CSV)
    assert set(agg) == {"BIGLIQ", "THINQ", "CROWDED"}   # NIFTY (index) out
    assert agg["BIGLIQ"]["opt_oi"] == 3_000_000         # both expiries
    assert agg["BIGLIQ"]["opt_val"] == 8_000_000_000.0
    assert agg["BIGLIQ"]["fut_oi"] == 700_000


def test_secban_parse():
    assert FO.parse_secban(SECBAN) == ["CROWDED"]


def _seed_lake(tmp_path):
    d = tmp_path / "2026-07-17"
    d.mkdir(parents=True)
    (d / "fo.csv").write_text(FO_CSV)
    ban_day = tmp_path / "2026-07-20"       # pre-open day: ban list only
    ban_day.mkdir()
    (ban_day / "secban.csv").write_text(SECBAN)


def test_snapshot_tiers_ban_and_newest_bhavcopy_day(tmp_path):
    _seed_lake(tmp_path)
    s = FO.liquidity_snapshot(lake_dir=tmp_path, write=False)
    assert s["as_of"] == "2026-07-17"        # newest day WITH fo.csv
    assert s["banned"] == ["CROWDED"]        # newest ban list (20th) used
    assert s["symbols"]["BIGLIQ"]["tier"] == "tier1"
    assert s["symbols"]["CROWDED"]["tier"] == "banned"
    assert s["symbols"]["BIGLIQ"]["rank"] == 1


def test_ingest_bundle_routes_nested_zip(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("fo17072026.csv", FO_CSV)
    outer_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(outer_path, "w") as z:
        z.writestr("fo17072026.zip", inner.getvalue())
        z.writestr("fo_secban_20072026.csv", SECBAN)
        z.writestr("FOVOLT_17072026.csv", "Date, Symbol\n")
        z.writestr("junk.bin", b"\x00")
    out = FO.ingest_bundle(outer_path, lake_dir=tmp_path / "lake")
    assert out["status"] == "ok"
    assert sorted(out["landed"]["2026-07-17"]) == ["fo", "fovolt"]
    assert out["landed"]["2026-07-20"] == ["secban"]


def test_fetch_day_unwraps_idempotent_and_honest_miss(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("fo16072026.csv", FO_CSV)
    blobs = {"fo": inner.getvalue(), "secban": SECBAN.encode(),
             "fovolt": b"Date, Symbol\n"}
    calls = []

    def fake(url):
        for k in blobs:
            if k in url.lower() or ("mkt" in url and k == "fo"):
                calls.append(k)
                return blobs[k]
        raise ConnectionError("404")

    r = FO.fetch_day(date(2026, 7, 16), fetch_bytes_fn=fake,
                     lake_dir=tmp_path, sleep_fn=lambda s: None)
    assert sorted(r["got"]) == ["fo", "fovolt", "secban"] or \
        set(r["got"]) == {"fo", "secban", "fovolt"}
    assert (tmp_path / "2026-07-16" / "fo.csv").read_text() == FO_CSV
    again = FO.fetch_day(date(2026, 7, 16), fetch_bytes_fn=fake,
                         lake_dir=tmp_path, sleep_fn=lambda s: None)
    assert set(again["got"]) == {"fo", "secban", "fovolt"}   # idempotent

    def html(url):
        return b"<html>error page</html>"
    miss = FO.fetch_day(date(2026, 7, 15), fetch_bytes_fn=html,
                        lake_dir=tmp_path, sleep_fn=lambda s: None)
    assert set(miss["missed"]) >= {"secban", "fovolt"}

"""
NSE historical-index drop-folder clerk, fully offline: the export format
parses (DD-MON-YYYY, close col, '-' -> None), the index is read from the
filename, the merge extends BACKWARD without rewriting stored dates, and
a folder ingests fail-open with named skips.
"""
from src.ingestion import index_history as IH


EXPORT = (
    "Date ,Open ,High ,Low ,Close ,Shares Traded ,Turnover (₹ Cr)\n"
    "31-DEC-2018,10913,10923.55,10853.2,10862.55,199057082,9457.61\n"
    "28-DEC-2018,10820.95,10893.6,10817.15,10859.9,253086507,12615.01\n"
    "27-DEC-2018,10817.9,10834.2,10764.45,-,470160392,19119.88\n"   # hole
)


def test_parse_export_salvages_date_and_close_drops_closeless():
    rows = IH.parse_export(EXPORT.encode())
    # the '-' close row is DROPPED (no price to salvage), the rest kept
    assert rows == [("2018-12-31", 10862.55), ("2018-12-28", 10859.9)]


def test_parse_export_survives_bom_empty_ohlc_and_reduced_schemas():
    """The owner's dirty-data cases, all salvaged, none crashing:
    a UTF-8 BOM header, empty Open/High/Low/Volume (Close still present
    positionally), and a reduced Date,Close-only schema."""
    # BOM + empty OHLC/volume — Close found by NAME at its real index
    bom_empty = (
        "﻿Date ,Open ,High ,Low ,Close ,Shares Traded ,Turnover\n"
        "31-DEC-2001,,,,868.61,,\n"
        "28-DEC-2001,,,,870.0,,\n")
    assert IH.parse_export(bom_empty.encode()) == [
        ("2001-12-31", 868.61), ("2001-12-28", 870.0)]

    # reduced schema: Close is column 1, not 4 — header-by-name finds it
    reduced = "Date ,Close\n02-JAN-2001,860.5\n03-JAN-2001,865.25\n"
    assert IH.parse_export(reduced.encode()) == [
        ("2001-01-02", 860.5), ("2001-01-03", 865.25)]

    # headerless Date,Close dump — heuristic salvages it
    headerless = "02-JAN-2001,860.5\n03-JAN-2001,865.25\n"
    assert IH.parse_export(headerless.encode()) == [
        ("2001-01-02", 860.5), ("2001-01-03", 865.25)]

    # garbage never raises
    assert IH.parse_export(b"total nonsense\nno,dates,here\n") == []


def test_index_key_from_filename():
    assert IH.index_key_from_filename(
        "NIFTY 50-01-01-2018-to-30-12-2018.csv") == "NIFTY"
    assert IH.index_key_from_filename(
        "India VIX-01-01-2018-to-30-12-2018.csv") == "INDIAVIX"
    assert IH.index_key_from_filename(
        "NIFTY BANK-01-01-2018-to-30-12-2018.csv") == "NIFTY_BANK"
    assert IH.index_key_from_filename(              # browser dup suffix
        "NIFTY 50-01-01-2018-to-30-12-2018 (1).csv") == "NIFTY"
    assert IH.index_key_from_filename("random.csv") is None
    assert IH.index_key_from_filename(
        "NIFTY WEIRD-01-01-2018-to-30-12-2018.csv") is None


def test_merge_extends_backward_without_rewriting(tmp_path):
    lake = tmp_path
    # the lake already holds 2019 forward data (indices_lake's floor)
    (lake / "NIFTY.csv").write_text(
        "date,value\n2019-10-01,11359.9\n2019-10-03,11314.0\n")
    older = [("2018-12-31", 10862.55), ("2018-12-28", 10859.9),
             ("2019-10-01", 99999.0)]          # overlaps a stored date
    out = IH.merge_into_lake("NIFTY", older, lake_dir=lake)
    assert out["added"] == 2                    # only the two 2018 dates
    assert out["overlap_kept"] == 1             # 2019-10-01 NOT rewritten
    assert out["floor"] == "2018-12-28"         # earliest after backfill
    assert out["ceiling"] == "2019-10-03"
    body = (lake / "NIFTY.csv").read_text().splitlines()
    assert body[0] == "date,value"
    assert body[1] == "2018-12-28,10859.9"      # sorted ascending
    assert "2019-10-01,11359.9" in body         # stored value survived
    assert "99999.0" not in (lake / "NIFTY.csv").read_text()


def test_merge_is_idempotent(tmp_path):
    rows = IH.parse_export(EXPORT.encode())      # 2 salvageable rows
    first = IH.merge_into_lake("NIFTY", rows, lake_dir=tmp_path)
    assert first["added"] == 2
    again = IH.merge_into_lake("NIFTY", rows, lake_dir=tmp_path)
    assert again["added"] == 0 and again["overlap_kept"] == 2


def test_ingest_folder_routes_by_name_and_skips_unmapped(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "NIFTY 50-01-01-2018-to-30-12-2018.csv").write_text(EXPORT)
    (drop / "India VIX-01-01-2018-to-30-12-2018.csv").write_text(
        "Date ,Open ,High ,Low ,Close ,Shares Traded ,Turnover (₹ Cr)\n"
        "31-DEC-2018,16.5,16.8,16.1,16.35,0,0\n")
    (drop / "not-an-export.csv").write_text("garbage\n")
    lake = tmp_path / "macro"
    out = IH.ingest_folder(drop, lake_dir=lake, log_path=tmp_path / "l.jsonl")
    assert set(out["by_key"]) == {"NIFTY", "INDIAVIX"}
    assert out["by_key"]["NIFTY"]["added"] == 2
    assert out["by_key"]["INDIAVIX"]["added"] == 1
    assert [s["file"] for s in out["skipped"]] == ["not-an-export.csv"]
    assert (lake / "NIFTY.csv").exists() and (lake / "INDIAVIX.csv").exists()


def test_dry_run_writes_nothing(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "NIFTY 50-01-01-2018-to-30-12-2018.csv").write_text(EXPORT)
    lake = tmp_path / "macro"
    out = IH.ingest_folder(drop, lake_dir=lake, dry_run=True)
    assert out["by_key"]["NIFTY"]["added"] == 2
    assert not lake.exists()

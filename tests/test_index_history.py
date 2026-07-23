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


def test_parse_export_dates_close_and_null_honesty():
    rows = IH.parse_export(EXPORT.encode())
    assert rows == [("2018-12-31", 10862.55), ("2018-12-28", 10859.9),
                    ("2018-12-27", None)]                 # '-' -> None kept


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
    rows = IH.parse_export(EXPORT.encode())
    first = IH.merge_into_lake("NIFTY", rows, lake_dir=tmp_path)
    assert first["added"] == 3
    again = IH.merge_into_lake("NIFTY", rows, lake_dir=tmp_path)
    assert again["added"] == 0 and again["overlap_kept"] == 3


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
    assert out["by_key"]["NIFTY"]["added"] == 3
    assert out["by_key"]["INDIAVIX"]["added"] == 1
    assert [s["file"] for s in out["skipped"]] == ["not-an-export.csv"]
    assert (lake / "NIFTY.csv").exists() and (lake / "INDIAVIX.csv").exists()


def test_dry_run_writes_nothing(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "NIFTY 50-01-01-2018-to-30-12-2018.csv").write_text(EXPORT)
    lake = tmp_path / "macro"
    out = IH.ingest_folder(drop, lake_dir=lake, dry_run=True)
    assert out["by_key"]["NIFTY"]["added"] == 3
    assert not lake.exists()

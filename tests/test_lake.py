"""
Tests for src/lake.py — the date-partitioned gz-JSONL cold store.
Fully offline; every test runs against a temp root.

Run either of these from the project folder:
    python tests/test_lake.py
    python -m pytest tests/test_lake.py
"""

import gzip
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import lake


def test_write_partition_roundtrip_and_zcat_compatible():
    with tempfile.TemporaryDirectory() as tmp:
        rows = [{"a": 1}, {"a": 2, "b": "x"}]
        path = lake.write_partition("chains/nifty", "2026-07-10", rows, root=tmp)
        assert path is not None and path.name == "part.jsonl.gz"
        assert lake.read_day("chains/nifty", "2026-07-10", root=tmp) == rows
        # plain gzip-readable (zcat compatibility)
        with gzip.open(path, "rt") as fh:
            assert json.loads(fh.readline())["a"] == 1


def test_write_partition_replaces_whole_day():
    with tempfile.TemporaryDirectory() as tmp:
        lake.write_partition("ds", "2026-07-10", [{"v": 1}], root=tmp)
        lake.write_partition("ds", "2026-07-10", [{"v": 2}], root=tmp)
        assert lake.read_day("ds", "2026-07-10", root=tmp) == [{"v": 2}]


def test_append_rows_concatenated_members_read_as_one_stream():
    with tempfile.TemporaryDirectory() as tmp:
        assert lake.append_rows("candles/nifty", "2026-07-10",
                                [{"c": 1}], root=tmp) == 1
        assert lake.append_rows("candles/nifty", "2026-07-10",
                                [{"c": 2}, {"c": 3}], root=tmp) == 2
        rows = lake.read_day("candles/nifty", "2026-07-10", root=tmp)
        assert [r["c"] for r in rows] == [1, 2, 3]


def test_invalid_names_and_bad_rows_fail_open():
    with tempfile.TemporaryDirectory() as tmp:
        assert lake.write_partition("Bad Name!", "2026-07-10", [{}], root=tmp) is None
        assert lake.write_partition("ds", "not-a-date", [{}], root=tmp) is None
        assert lake.append_rows("ds", "nope", [{}], root=tmp) == 0
        # Unserializable row -> logged failure, no partial file left behind
        assert lake.write_partition("ds", "2026-07-10", [object()], root=tmp) is None
        assert lake.read_day("ds", "2026-07-10", root=tmp) == []
        # Scan over a missing dataset yields nothing
        assert list(lake.scan("ghost", root=tmp)) == []


def test_archive_blob_hashes_dedups_and_flags_revisions():
    with tempfile.TemporaryDirectory() as tmp:
        first = lake.archive_blob("deals_raw", "2026-07-10", "nse", b"payload-1",
                                  ext="json", root=tmp)
        assert first is not None and len(first["sha256"]) == 64
        # Same content again -> same path, no duplicate file
        again = lake.archive_blob("deals_raw", "2026-07-10", "nse", b"payload-1",
                                  ext="json", root=tmp)
        assert again["path"] == first["path"]
        # CHANGED content for the same day/name -> loud rev- file, original kept
        rev = lake.archive_blob("deals_raw", "2026-07-10", "nse", b"payload-2",
                                ext="json", root=tmp)
        assert "rev-" in rev["path"].name and first["path"].exists()
        with gzip.open(first["path"], "rb") as fh:
            assert fh.read() == b"payload-1"


def test_list_days_sorted_and_scan_bounds():
    with tempfile.TemporaryDirectory() as tmp:
        for day, v in (("2026-07-12", 3), ("2026-07-10", 1), ("2026-07-11", 2)):
            lake.write_partition("ds", day, [{"v": v}], root=tmp)
        assert lake.list_days("ds", root=tmp) == ["2026-07-10", "2026-07-11", "2026-07-12"]
        got = [(d, r["v"]) for d, r in lake.scan("ds", start="2026-07-11", root=tmp)]
        assert got == [("2026-07-11", 2), ("2026-07-12", 3)]
        got = [r["v"] for _, r in lake.scan("ds", end="2026-07-10", root=tmp)]
        assert got == [1]


def test_scan_skips_malformed_lines():
    with tempfile.TemporaryDirectory() as tmp:
        path = lake.write_partition("ds", "2026-07-10", [{"ok": 1}], root=tmp)
        with open(path, "ab") as fh:           # corrupt member appended raw
            fh.write(gzip.compress(b"{not json}\n"))
        rows = lake.read_day("ds", "2026-07-10", root=tmp)
        assert rows == [{"ok": 1}]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")

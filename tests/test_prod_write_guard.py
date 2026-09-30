"""
The production write guard in tests/conftest.py (ledger Issue 37) must keep
working: a write aimed at the real data/ or logs/ is refused before it
reaches the disk, reads and tmp_path writes are untouched, and a refusal
that the code under test swallows still fails the test.
"""
import os
import sqlite3
import sys

import pytest

guard = sys.modules["conftest"]          # the rootdir tests/conftest.py
PROBE = "_write_guard_probe.tmp"


def _refused(fn):
    """Run fn, expect ProdWriteBlocked, then forget the refusal so the
    autouse check does not fail THIS test for exercising the guard."""
    with pytest.raises(guard.ProdWriteBlocked):
        fn()
    assert guard._blocked, "the refusal was not recorded"
    guard._blocked.clear()


@pytest.mark.parametrize("sub", ["data", "logs"])
def test_writes_into_real_data_and_logs_are_refused(sub):
    target = guard._REPO / sub / PROBE
    _refused(lambda: open(target, "w"))
    _refused(lambda: open(target, "a"))
    _refused(lambda: target.write_text("x"))
    _refused(lambda: os.open(target, os.O_WRONLY | os.O_CREAT))
    _refused(lambda: sqlite3.connect(str(guard._REPO / sub / "probe.db")))
    assert not target.exists()                   # nothing reached the disk
    assert not (guard._REPO / sub / "probe.db").exists()


def test_deletes_and_renames_into_real_data_are_refused(tmp_path):
    src = tmp_path / "x.txt"
    src.write_text("x")
    _refused(lambda: os.replace(src, guard._REPO / "data" / PROBE))
    _refused(lambda: os.remove(guard._REPO / "data" / PROBE))
    assert src.exists()


def test_reads_read_only_connects_and_tmp_writes_are_allowed(tmp_path):
    (tmp_path / "ok.txt").write_text("fine")      # tmp_path is not the repo
    open(guard._REPO / "tests" / "conftest.py").close()   # a read
    ro = guard._REPO / "data" / "absent.db"
    with pytest.raises(sqlite3.OperationalError):  # allowed through; the
        sqlite3.connect(f"file:{ro}?mode=ro", uri=True).execute("select 1")
    assert not guard._blocked                      # file just does not exist


def test_a_swallowed_refusal_still_counts():
    """Fail-open code under test may catch the exception; the write is
    still refused and still recorded for the autouse check to fail on."""
    try:
        (guard._REPO / "logs" / PROBE).write_text("x")
    except OSError:
        pass                                      # what fail-open code does
    assert guard._blocked and "logs" in guard._blocked[0][1]
    guard._blocked.clear()

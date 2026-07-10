"""
Tests for the EOD option-chain archiver (Phase 0). Fully offline —
every Dhan fetcher injected; no network, no token.

Run either of these from the project folder:
    python tests/test_chain_archiver.py
    python -m pytest tests/test_chain_archiver.py
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import lake
from src.ingestion import chain_archiver as ca


def _fetchers(expiries=("2026-07-16", "2026-07-23", "2026-07-30",
                        "2026-08-27", "2026-09-24"),
              chain=None, spot=25000.0, vix=13.5, fail_expiry=None):
    calls = {"chain": [], "sleeps": []}

    def expiry_fn(u):
        return list(expiries)

    def chain_fn(u, e):
        calls["chain"].append((u, e))
        if e == fail_expiry:
            raise RuntimeError("simulated DH-905")
        return chain if chain is not None else {
            "last_price": spot, "oc": {"25000": {"ce": {"ltp": 120}, "pe": {"ltp": 95}}}}

    return {
        "expiry_fn": expiry_fn, "chain_fn": chain_fn,
        "spot_fn": lambda u: spot, "vix_fn": lambda: vix,
        "sleep_fn": lambda s: calls["sleeps"].append(s),
    }, calls


def test_captures_nearest_expiries_only_and_throttles():
    f, calls = _fetchers()
    rows = ca.capture_underlying("NIFTY 50", "nifty", date(2026, 7, 10), **f)
    assert len(rows) == ca.MAX_EXPIRIES                 # capped at nearest 4
    assert [r["expiry"] for r in rows] == ["2026-07-16", "2026-07-23",
                                           "2026-07-30", "2026-08-27"]
    assert len(calls["sleeps"]) == ca.MAX_EXPIRIES - 1  # throttle between calls
    assert rows[0]["spot"] == 25000.0 and rows[0]["vix"] == 13.5
    assert rows[0]["oc"]                                 # chain payload kept


def test_past_expiries_dropped_and_one_failure_never_blocks_the_rest():
    f, _ = _fetchers(expiries=("2026-07-01", "2026-07-16", "2026-07-23"),
                     fail_expiry="2026-07-16")
    rows = ca.capture_underlying("NIFTY 50", "nifty", date(2026, 7, 10), **f)
    # 07-01 already expired -> dropped; 07-16 raised -> skipped; 07-23 kept.
    assert [r["expiry"] for r in rows] == ["2026-07-23"]


def test_empty_chain_and_dead_expiry_list_fail_open():
    f, _ = _fetchers(chain={"last_price": 1, "oc": {}})
    assert ca.capture_underlying("NIFTY 50", "n", date(2026, 7, 10), **f) == []

    def dead_expiry(u):
        raise RuntimeError("no token")
    f, _ = _fetchers()
    f["expiry_fn"] = dead_expiry
    assert ca.capture_underlying("NIFTY 50", "n", date(2026, 7, 10), **f) == []


def test_run_writes_lake_partitions_per_underlying():
    with tempfile.TemporaryDirectory() as tmp:
        f, _ = _fetchers()
        summary = ca.run(today=date(2026, 7, 10), lake_root=tmp, **f)
        assert summary["captured"]["NIFTY 50"] == ca.MAX_EXPIRIES
        assert summary["captured"]["NIFTY BANK"] == ca.MAX_EXPIRIES
        rows = lake.read_day("chains/nifty", "2026-07-10", root=tmp)
        assert len(rows) == ca.MAX_EXPIRIES
        assert rows[0]["underlying"] == "NIFTY 50"
        assert lake.read_day("chains/banknifty", "2026-07-10", root=tmp)


def test_weekend_skips_unless_forced():
    with tempfile.TemporaryDirectory() as tmp:
        f, calls = _fetchers()
        summary = ca.run(today=date(2026, 7, 11), lake_root=tmp, **f)  # Saturday
        assert summary["skipped"] == "weekend" and not calls["chain"]
        summary = ca.run(today=date(2026, 7, 11), lake_root=tmp, force=True, **f)
        assert summary["captured"]["NIFTY 50"] == ca.MAX_EXPIRIES


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

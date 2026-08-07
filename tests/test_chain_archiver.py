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
    assert len(rows) == 4                               # NIFTY keeps 4
    assert [r["expiry"] for r in rows] == ["2026-07-16", "2026-07-23",
                                           "2026-07-30", "2026-08-27"]
    assert len(calls["sleeps"]) == 3                    # throttle between calls
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
        assert summary["captured"]["NIFTY 50"] == 4
        assert summary["captured"]["NIFTY BANK"] == ca.MAX_EXPIRIES
        rows = lake.read_day("chains/nifty", "2026-07-10", root=tmp)
        assert len(rows) == 4
        assert rows[0]["underlying"] == "NIFTY 50"
        assert lake.read_day("chains/banknifty", "2026-07-10", root=tmp)


def test_weekend_skips_unless_forced():
    with tempfile.TemporaryDirectory() as tmp:
        f, calls = _fetchers()
        summary = ca.run(today=date(2026, 7, 11), lake_root=tmp, **f)  # Saturday
        assert summary["skipped"] == "weekend" and not calls["chain"]
        summary = ca.run(today=date(2026, 7, 11), lake_root=tmp, force=True, **f)
        assert summary["captured"]["NIFTY 50"] == 4


# ------------------------------------------- the 2026-08-07 expansion
# The desk went 2 -> 9 underlyings on 08-05 but only two chains were being
# captured, so a refused FINNIFTY or equity-option trade could not be
# priced by `ghost_tracker` at all — and decision #36's clock applies to
# every underlying equally: a chain not captured today is gone tomorrow.


def test_the_whole_live_universe_is_archived():
    """Drift guard: the archiver's universe must not fall behind the
    market loop's. Anything the desk can trade, it must capture."""
    from src import market_loop as ml
    assert set(ca.UNDERLYINGS) == set(ml.UNDERLYINGS)
    assert len(ca.UNDERLYINGS) == 9


def test_the_existing_slugs_are_never_renamed():
    """`chains/nifty` and `chains/banknifty` already hold history; a slug
    rename would orphan every partition written before today."""
    assert ca.UNDERLYINGS["NIFTY 50"] == "nifty"
    assert ca.UNDERLYINGS["NIFTY BANK"] == "banknifty"


def test_slugs_are_unique_so_no_two_underlyings_share_a_partition():
    assert len(set(ca.UNDERLYINGS.values())) == len(ca.UNDERLYINGS)


def test_only_the_weekly_carrying_index_takes_four_expiries():
    """FINNIFTY/MIDCPNIFTY and the five stocks are MONTHLY-ONLY, so a
    4-deep sweep reaches contracts nobody trades — 4x the calls and 4x
    the storage for the same live month."""
    assert ca.expiries_wanted("NIFTY 50") == 4
    for u in ("NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY MID SELECT",
              "TCS.NS", "RELIANCE.NS"):
        assert ca.expiries_wanted(u) == 2


def test_underlyings_are_paced_apart():
    """Nine underlyings back-to-back is a burst on an account with ONE
    rate budget. The pause makes the sweep a drip."""
    with tempfile.TemporaryDirectory() as tmp:
        f, calls = _fetchers()
        ca.run(today=date(2026, 7, 10), lake_root=tmp, **f)
        pauses = [s for s in calls["sleeps"] if s == ca.UNDERLYING_PAUSE_SECONDS]
        assert len(pauses) == len(ca.UNDERLYINGS) - 1


def test_one_dead_underlying_never_costs_the_other_eight():
    """A stock chain that answers nothing must not abort the sweep."""
    with tempfile.TemporaryDirectory() as tmp:
        f, _ = _fetchers()
        good = f["chain_fn"]

        def chain_fn(u, e):
            if u == "TCS.NS":
                raise RuntimeError("simulated DH-905")
            return good(u, e)

        f["chain_fn"] = chain_fn
        summary = ca.run(today=date(2026, 7, 10), lake_root=tmp, **f)
        assert summary["captured"]["TCS.NS"] == 0
        assert summary["captured"]["NIFTY 50"] == 4
        assert summary["captured"]["RELIANCE.NS"] == 2


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

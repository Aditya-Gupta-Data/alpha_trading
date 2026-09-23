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
        assert summary["captured"]["NIFTY BANK"] == ca.MAX_EXPIRIES  # 3
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
    """NIFTY carries weeklies — the near-dated surface — so it takes 4.
    The monthly-only names take 3: raised from 2 on 2026-08-11 because
    the 08-10 ghosts wanted the THIRD monthly (2026-10-27) and the
    archive stopped at the second, so seven refused trades could not be
    priced at all. The archive has to reach as far as the proposer does."""
    assert ca.expiries_wanted("NIFTY 50") == 4
    for u in ("NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY MID SELECT",
              "TCS.NS", "RELIANCE.NS"):
        assert ca.expiries_wanted(u) == 3


def test_underlyings_are_paced_apart():
    """Nine underlyings back-to-back is a burst on an account with ONE
    rate budget. The pause makes the sweep a drip."""
    with tempfile.TemporaryDirectory() as tmp:
        f, calls = _fetchers()
        # the tier-1 extension (#100) adds one pause per extension name;
        # this test measures the CORE drip, so point the extension at nothing
        f["fo_path"] = Path(tmp) / "no_fo.json"
        f["ids_path"] = Path(tmp) / "no_ids.json"      # and the MCX extension (#106)
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
        assert summary["captured"]["RELIANCE.NS"] == 3


# ---------------------------------------------- no silent zeros (2026-08-13)
# The audit case, verbatim: 2026-08-05 logged
#   {"date":"2026-08-05","captured":{"NIFTY 50":0,"NIFTY BANK":0},"skipped":null}
# on a Wednesday. Green heartbeat, zero data, no alert, and option chains are
# the one dataset decision #36 says can never be re-bought.

def _problem_lines(capsys):
    from src.ops_monitor import is_problem_line
    return [ln for ln in capsys.readouterr().out.splitlines()
            if is_problem_line(ln)]


def test_an_empty_underlying_is_a_named_skip_that_reaches_the_ops_card(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        f, _ = _fetchers()
        good = f["chain_fn"]
        f["chain_fn"] = lambda u, e: None if u == "TCS.NS" else good(u, e)
        summary = ca.run(today=date(2026, 7, 10), lake_root=tmp, **f)

    assert summary["captured"]["TCS.NS"] == 0
    assert summary["empty"] == ["TCS.NS"]
    assert summary["skipped"] == "CA-EMPTY"          # was None — the bug
    fired = _problem_lines(capsys)
    assert any("CA-EMPTY" in ln and "TCS.NS" in ln for ln in fired)


def test_every_underlying_empty_is_CA_BLACKOUT_not_nine_coincidences(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        f, _ = _fetchers()
        f["chain_fn"] = lambda u, e: None
        summary = ca.run(today=date(2026, 7, 10), lake_root=tmp, **f)

    assert summary["skipped"] == "CA-BLACKOUT"
    assert len(summary["empty"]) == len(ca.UNDERLYINGS)
    assert all(v == 0 for v in summary["captured"].values())
    assert any("CA-BLACKOUT" in ln for ln in _problem_lines(capsys))


def test_a_clean_day_stays_silent_on_the_ops_card(capsys):
    """The other half of the contract: a good night must not start
    crying wolf, or the card becomes noise and gets ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        summary = ca.run(today=date(2026, 7, 10), lake_root=tmp,
                         **_fetchers()[0])
    assert summary["skipped"] is None and summary["empty"] == []
    assert _problem_lines(capsys) == []


def test_a_weekend_is_still_silent(capsys):
    """A closed market owes us nothing — it must never look like loss."""
    with tempfile.TemporaryDirectory() as tmp:
        summary = ca.run(today=date(2026, 7, 11), lake_root=tmp,
                         **_fetchers()[0])
    assert summary["skipped"] == "weekend" and summary["empty"] == []
    assert _problem_lines(capsys) == []


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


# ------------------------------------------ tier-1 extension (2026-09-19, #100)

import json as _json


def _ext_files(tmp, tier1=("ABB", "BAJAJ-AUTO", "TCS", "AMBER", "BANNEDCO"),
               banned=("BANNEDCO",), ids=None):
    fo = tmp / "fo.json"
    fo.write_text(_json.dumps({"as_of": "2026-09-19", "banned": list(banned),
                               "symbols": {s: {"tier": "tier1"} for s in tier1}
                               | {"TIER2CO": {"tier": "tier2"}}}))
    idp = tmp / "ids.json"
    idp.write_text(_json.dumps({"ids": ids if ids is not None else
                                {"ABB": {"id": "13"}, "BAJAJ-AUTO": {"id": "16669"}}}))
    return fo, idp


def test_tier1_extension_resolves_by_id_skips_core_banned_and_unresolved(tmp_path):
    fo, idp = _ext_files(tmp_path)
    ext = ca.tier1_extension(fo, idp)
    assert set(ext["names"]) == {"ABB.NS", "BAJAJ-AUTO.NS"}      # TCS is core, TIER2CO not tier1
    assert ext["names"]["BAJAJ-AUTO.NS"] == {"slug": "bajaj_auto", "security_id": "16669",
                                             "segment": "NSE_EQ", "symbol": "BAJAJ-AUTO"}
    assert ("BANNEDCO", "fo_banned") in ext["skipped"]
    assert ("AMBER", "no_scrip_master_id") in ext["skipped"]
    assert ca.extension_slug("GVT&D") == "gvt_d"
    assert not set(v["slug"] for v in ext["names"].values()) & set(ca.UNDERLYINGS.values())


def test_tier1_extension_fails_open_by_name_when_a_file_is_missing(tmp_path):
    ext = ca.tier1_extension(tmp_path / "nope.json", tmp_path / "nope2.json")
    assert ext["names"] == {} and ext["skipped"][0][1].startswith("fo_liquidity unavailable")
    fo, _ = _ext_files(tmp_path)
    ext = ca.tier1_extension(fo, tmp_path / "nope2.json")
    assert ext["names"] == {}
    assert any(r.startswith("darling_ids unavailable") for _, r in ext["skipped"])
    assert ("ABB", "no_scrip_master_id") in ext["skipped"]


def test_run_captures_the_extension_by_id_after_the_core_nine(tmp_path):
    fo, idp = _ext_files(tmp_path)
    f, calls = _fetchers()
    seen = {"expiry": [], "chain": [], "spot": []}
    f["fo_path"], f["ids_path"] = fo, idp
    f["expiry_by_id_fn"] = lambda sid, seg: seen["expiry"].append((sid, seg)) or ["2026-09-30", "2026-10-28", "2026-11-25"]
    f["chain_by_id_fn"] = lambda sid, e, seg: seen["chain"].append((sid, e)) or {
        "last_price": 1000.0, "oc": {"1000.000000": {"ce": {"last_price": 20, "lotSize": 250}}}}
    f["spot_by_id_fn"] = lambda sid, seg: seen["spot"].append(sid) or 1000.0
    summary = ca.run(today=date(2026, 7, 10), lake_root=tmp_path, **f)
    assert summary["captured"]["NIFTY 50"] == 4                    # core untouched
    ext = summary["extension"]
    assert ext["captured"] == {"ABB.NS": 2, "BAJAJ-AUTO.NS": 2}    # EXTENSION_MAX_EXPIRIES
    assert ext["empty"] == [] and ("AMBER", "no_scrip_master_id") in ext["skipped"]
    assert {s for s, _ in seen["expiry"]} == {"13", "16669"}
    assert all(seg == "NSE_EQ" for _, seg in seen["expiry"])
    assert len(seen["chain"]) == 4
    assert lake.read_day("chains/bajaj_auto", "2026-07-10", root=tmp_path)[0]["underlying"] == "BAJAJ-AUTO.NS"
    assert summary["skipped"] is None                              # extension holes never taint the core verdict


def test_an_empty_extension_name_is_noted_not_a_core_blackout(tmp_path):
    fo, idp = _ext_files(tmp_path)
    f, _ = _fetchers()
    f["fo_path"], f["ids_path"] = fo, idp
    f["expiry_by_id_fn"] = lambda sid, seg: []
    f["chain_by_id_fn"] = lambda sid, e, seg: None
    f["spot_by_id_fn"] = lambda sid, seg: None
    summary = ca.run(today=date(2026, 7, 10), lake_root=tmp_path, **f)
    assert summary["extension"]["captured"] == {"ABB.NS": 0, "BAJAJ-AUTO.NS": 0}
    assert set(summary["extension"]["empty"]) == {"ABB.NS", "BAJAJ-AUTO.NS"}
    assert summary["skipped"] is None and summary["empty"] == []


def test_the_court_reads_extension_slugs_and_lot_size_from_the_chain(tmp_path, monkeypatch):
    from src.validation import run_proving_court as court
    from src import lake as _lake
    oc = {f"{k:.6f}": {"ce": {"last_price": p, "top_ask_price": p + 0.5, "top_bid_price": p - 0.5,
                              "lotSize": 250}, "pe": {"last_price": 1.0, "lotSize": 250}}
          for k, p in ((980.0, 40.0), (1000.0, 25.0), (1020.0, 15.0), (1040.0, 9.0),
                       (1060.0, 5.0), (1080.0, 3.0), (1100.0, 1.5))}
    rows = [{"underlying": "BAJAJ-AUTO.NS", "slug": "bajaj_auto", "expiry": "2026-09-30",
             "spot": 1005.0, "oc": oc}]
    monkeypatch.setattr(_lake, "read_day", lambda ds, day, name=None, root=None:
                        rows if ds == "chains/bajaj_auto" else [])
    ch = court.chain_from_lake("BAJAJ-AUTO.NS", "2026-09-19", today=date(2026, 9, 19))
    assert ch is not None and ch["lot_size"] == 250 and ch["buy_strike"] == 1000.0
    assert court.lot_size_from_chain({}) is None

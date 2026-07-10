"""
Tests for the deals-tape census + raw-payload archiving (Phase 0.4 of
docs/HOLY_GRAIL_PLAN.md): per-day data-quality telemetry and immutable raw
snapshots protecting the affinity substrate. Fully offline.

Run either of these from the project folder:
    python tests/test_deals_census.py
    python -m pytest tests/test_deals_census.py
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import lake
from src.ingestion import deals_tracker as dt


def _deal(ticker, client, side="buy", qty=1000, price=100.0, deal_type="bulk"):
    return {"ticker": ticker, "client": client, "side": side, "qty": qty,
            "price": price, "value_rs": qty * price, "deal_type": deal_type}


def test_census_counts_and_group_coverage():
    deals = [
        _deal("ADANIENT.NS", "MISTY SEAS FUND"),               # grouped (ADANI)
        _deal("WIPRO.NS", "SOME PROP DESK", side="sell"),      # ungrouped
        _deal("TCS.NS", "OTHER FUND", deal_type="block"),      # grouped (TATA)
    ]
    raw_rows = deals + [{"symbol": "", "junk": True}]          # one dropped row
    c = dt.build_census(deals, raw_rows, "nse", "2026-07-10")
    assert c["raw_rows"] == 4 and c["normalized"] == 3 and c["dropped"] == 1
    assert c["distinct_clients"] == 3 and c["distinct_tickers"] == 3
    assert c["block_legs"] == 1 and c["buy_legs"] == 2 and c["sell_legs"] == 1
    assert c["ungrouped_deals"] == 1                            # WIPRO only
    assert c["source"] == "nse"


def test_alias_candidates_flag_near_duplicates_but_never_merge():
    deals = [
        _deal("X.NS", "SBI MUTUAL FUND A/C BLUECHIP"),
        _deal("Y.NS", "SBI MUTUAL FUNDS LIMITED"),
        _deal("Z.NS", "GRAVITON RESEARCH CAPITAL"),   # unique — not flagged
    ]
    cands = dt._alias_candidates(deals)
    assert len(cands) == 1
    assert cands[0]["prefix"] == "SBI MUTUAL"
    assert len(cands[0]["names"]) == 2
    # The deals themselves are untouched — flagging is advisory only.
    assert deals[0]["client"] == "SBI MUTUAL FUND A/C BLUECHIP"


def test_run_archives_raw_payload_and_census_to_lake():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        raw_rows = [{"symbol": "ITC", "clientName": "BIG FII",
                     "buySell": "BUY", "qty": "5,000", "watp": "400",
                     "deal_type": "bulk"}]
        payload = json.dumps({"BULK_DEALS_DATA": raw_rows}).encode()
        original = dt._fetch_nse_largedeals
        dt._fetch_nse_largedeals = lambda *a, **k: (raw_rows, payload)
        try:
            m = dt.run(output_path=tmp / "bulk.json",
                       snapshot_path=tmp / "no-snap.json",
                       watchlist_path=tmp / "no-wl.json",
                       history_path=tmp / "hist.jsonl",
                       lake_root=tmp / "lake",
                       today=date(2026, 7, 10), use_live=True)
        finally:
            dt._fetch_nse_largedeals = original
        assert m["source"] == "nse"
        # Raw payload archived with hash; census row written and readable.
        blobs = list((tmp / "lake" / "deals_raw" / "date=2026-07-10").iterdir())
        assert any(b.name.startswith("largedeal") for b in blobs)
        census = lake.read_day("deals_census", "2026-07-10", root=tmp / "lake")
        assert len(census) == 1
        assert census[0]["normalized"] == 1 and census[0]["source"] == "nse"


def test_snapshot_source_still_gets_census_but_no_raw_blob():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        snap = tmp / "snap.json"
        snap.write_text(json.dumps(
            [{"symbol": "LT", "clientName": "F", "buySell": "SELL",
              "qty": 100, "watp": 3500}]))
        m = dt.run(output_path=tmp / "bulk.json", snapshot_path=snap,
                   watchlist_path=tmp / "no-wl.json",
                   history_path=tmp / "hist.jsonl",
                   lake_root=tmp / "lake",
                   today=date(2026, 7, 10), use_live=False)
        assert m["source"] == "snapshot"
        census = lake.read_day("deals_census", "2026-07-10", root=tmp / "lake")
        assert census and census[0]["source"] == "snapshot"
        assert not (tmp / "lake" / "deals_raw").exists()   # nothing to archive


def test_legacy_fake_fetcher_returning_bare_list_still_works():
    # Back-compat: older tests monkeypatch the fetcher with a bare list.
    deals, wl, source, raw_rows, raw_payload = None, None, None, None, None
    original = dt._fetch_nse_largedeals
    dt._fetch_nse_largedeals = lambda *a, **k: [
        {"symbol": "INFY", "clientName": "C", "buySell": "BUY",
         "qty": 10, "watp": 1500}]
    try:
        deals, wl, source, raw_rows, raw_payload = dt._collect_deals(
            snapshot_path="/nonexistent", watchlist_path="/nonexistent",
            use_live=True)
    finally:
        dt._fetch_nse_largedeals = original
    assert source == "nse" and len(deals) == 1 and raw_payload is None


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

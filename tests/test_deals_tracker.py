"""
Tests for the Phase-8 bulk/block deals tracker (src/ingestion/deals_tracker).
Fully offline — the live NSE fetch is never touched (use_live=False or a
patched fetcher). Covers the coercion helpers, the net-direction /
marquee-tagging aggregation, the Option-B snapshot fallback, and the
fail-open discipline (no network, no snapshot, broken config all degrade
to an empty "none" snapshot rather than raising).

Run either of these from the project folder:
    python tests/test_deals_tracker.py     (simple, no extra installs)
    python -m pytest tests/                 (if you have pytest)
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingestion import deals_tracker as dt


# ---- raw rows shaped like NSE's report (BUY/SELL legs of the same day) ----

def _bulk_row(symbol, client, side, qty, price, deal_type="bulk"):
    return {"symbol": symbol, "clientName": client, "buySell": side,
            "qty": qty, "watp": price, "deal_type": deal_type}


# ------------------------------------------------------------- coercion

def test_coerce_side_maps_every_spelling():
    for word in ("BUY", "buy", "B", "Bought", "PURCHASE"):
        assert dt.coerce_side(word) == "buy"
    for word in ("SELL", "s", "Sold", "SALE"):
        assert dt.coerce_side(word) == "sell"
    for junk in (None, "", "hold", "xyz", 7):
        assert dt.coerce_side(junk) == "unknown"


def test_to_number_handles_indian_comma_grouping():
    assert dt._to_number("1,50,000") == 150000.0
    assert dt._to_number("2,345.67") == 2345.67
    assert dt._to_number(500) == 500.0
    for junk in (None, "", "n/a", "--"):
        assert dt._to_number(junk) is None


def test_normalize_symbol_suffixes_and_aliases():
    assert dt.normalize_symbol("RELIANCE") == "RELIANCE.NS"
    assert dt.normalize_symbol("infy") == "INFY.NS"
    assert dt.normalize_symbol("TCS.NS") == "TCS.NS"          # already tickered
    assert dt.normalize_symbol("500325.BO") == "500325.BO"    # BSE passthrough
    assert dt.normalize_symbol("ABC", {"ABC": "ABCAP.NS"}) == "ABCAP.NS"
    for junk in (None, "", "   "):
        assert dt.normalize_symbol(junk) is None


# ------------------------------------------------------------- normalize

def test_normalize_deal_drops_untrustworthy_rows():
    good = dt.normalize_deal(_bulk_row("WIPRO", "Some FII", "BUY", "1,000", "250"))
    assert good["ticker"] == "WIPRO.NS"
    assert good["side"] == "buy" and good["qty"] == 1000
    assert good["value_rs"] == 250000.0 and good["deal_type"] == "bulk"
    # Missing symbol / unknown side / non-positive qty -> dropped (None).
    assert dt.normalize_deal(_bulk_row("", "x", "BUY", 10, 5)) is None
    assert dt.normalize_deal(_bulk_row("WIPRO", "x", "HODL", 10, 5)) is None
    assert dt.normalize_deal(_bulk_row("WIPRO", "x", "BUY", 0, 5)) is None
    assert dt.normalize_deal("not a dict") is None


def test_normalize_deal_survives_missing_price():
    d = dt.normalize_deal({"symbol": "SBIN", "buySell": "SELL", "qty": 500})
    assert d["ticker"] == "SBIN.NS" and d["value_rs"] is None
    assert d["price"] is None and d["side"] == "sell"


# ------------------------------------------------------------- aggregate

def test_aggregate_nets_buys_against_sells():
    deals = [
        dt.normalize_deal(_bulk_row("TATAMOTORS", "FII A", "BUY", 10000, 900)),
        dt.normalize_deal(_bulk_row("TATAMOTORS", "Prop B", "SELL", 4000, 900)),
    ]
    entries = dt.aggregate_deals(deals)
    e = entries["TATAMOTORS.NS"]
    assert e["net_qty"] == 6000               # 10000 buy - 4000 sell
    assert e["net_value_rs"] == 5400000.0     # (10000-4000) * 900
    assert e["buy_deals"] == 1 and e["sell_deals"] == 1
    assert e["block_deal"] is False
    assert e["marquee_names"] == [] and e["marquee_net"] == "none"


def test_aggregate_flags_block_deal_and_marquee_direction():
    deals = [
        dt.normalize_deal(_bulk_row("INFY", "SBI Mutual Fund A/C X", "BUY",
                                    20000, 1500, deal_type="block")),
        dt.normalize_deal(_bulk_row("INFY", "Retail HNI", "SELL", 5000, 1500)),
    ]
    e = dt.aggregate_deals(deals, marquee=["sbi mutual fund"])["INFY.NS"]
    assert e["block_deal"] is True
    assert e["marquee_names"] == ["SBI Mutual Fund A/C X"]
    assert e["marquee_net"] == "accumulating"    # marquee only bought
    assert e["net_qty"] == 15000


def test_marquee_net_mixed_when_marquee_on_both_sides():
    deals = [
        dt.normalize_deal(_bulk_row("HDFCBANK", "LIC of India", "BUY", 8000, 1600)),
        dt.normalize_deal(_bulk_row("HDFCBANK", "Nippon India MF", "SELL", 8000, 1600)),
    ]
    e = dt.aggregate_deals(deals, marquee=["lic of india", "nippon"])["HDFCBANK.NS"]
    assert e["marquee_net"] == "mixed"           # +8000 - 8000 = 0, both marquee
    assert set(e["marquee_names"]) == {"LIC of India", "Nippon India MF"}


# ------------------------------------------------------------- file loads

def test_load_watchlist_missing_and_broken_degrade_to_empty():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        assert dt.load_watchlist(missing) == {"marquee": [], "aliases": {}}
        broken = Path(tmp) / "broken.json"
        broken.write_text("{not json")
        assert dt.load_watchlist(broken) == {"marquee": [], "aliases": {}}


def test_load_watchlist_normalizes_case():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "wl.json"
        p.write_text(json.dumps({"marquee_names": ["SBI Mutual Fund", " "],
                                 "symbol_aliases": {"abc": "ABCAP.NS"}}))
        wl = dt.load_watchlist(p)
        assert wl["marquee"] == ["sbi mutual fund"]     # lowercased, blank dropped
        assert wl["aliases"] == {"ABC": "ABCAP.NS"}     # key uppercased


def test_snapshot_accepts_bare_list_and_wrapped():
    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "bare.json"
        bare.write_text(json.dumps([_bulk_row("X", "c", "BUY", 1, 1)]))
        assert len(dt._load_snapshot(bare)) == 1
        wrapped = Path(tmp) / "wrapped.json"
        wrapped.write_text(json.dumps({"deals": [_bulk_row("X", "c", "BUY", 1, 1)]}))
        assert len(dt._load_snapshot(wrapped)) == 1
        assert dt._load_snapshot(Path(tmp) / "missing.json") == []


# ------------------------------------------------------------- build/run

def test_build_from_snapshot_labels_source_and_aggregates():
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "snap.json"
        snap.write_text(json.dumps([
            _bulk_row("RELIANCE", "SBI Mutual Fund", "BUY", 12000, 2900,
                      deal_type="block"),
            _bulk_row("RELIANCE", "Some Prop Desk", "SELL", 2000, 2900),
        ]))
        wl = Path(tmp) / "wl.json"
        wl.write_text(json.dumps({"marquee_names": ["sbi mutual fund"]}))
        m = dt.build_deals_matrix(snapshot_path=snap, watchlist_path=wl,
                                  today=date(2026, 7, 10), use_live=False)
        assert m["as_of"] == "2026-07-10" and m["source"] == "snapshot"
        e = m["entries"]["RELIANCE.NS"]
        assert e["net_qty"] == 10000 and e["block_deal"] is True
        assert e["marquee_net"] == "accumulating"


def test_build_fails_open_to_none_with_no_data():
    with tempfile.TemporaryDirectory() as tmp:
        m = dt.build_deals_matrix(snapshot_path=Path(tmp) / "absent.json",
                                  watchlist_path=Path(tmp) / "absent2.json",
                                  today=date(2026, 7, 10), use_live=False)
        assert m["source"] == "none" and m["entries"] == {}


def test_live_fetch_failure_falls_open_to_snapshot(monkeypatch=None):
    # Simulate NSE being unreachable: the live fetcher returns None, so the
    # build must fall through to the snapshot rather than error.
    original = dt._fetch_nse_largedeals
    dt._fetch_nse_largedeals = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.json"
            snap.write_text(json.dumps([_bulk_row("ITC", "c", "BUY", 500, 400)]))
            m = dt.build_deals_matrix(snapshot_path=snap,
                                      watchlist_path=Path(tmp) / "no.json",
                                      today=date(2026, 7, 10), use_live=True)
            assert m["source"] == "snapshot"
            assert m["entries"]["ITC.NS"]["net_qty"] == 500
    finally:
        dt._fetch_nse_largedeals = original


def test_run_writes_snapshot_that_load_deals_reads_back():
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "snap.json"
        snap.write_text(json.dumps([_bulk_row("LT", "FII", "BUY", 3000, 3500)]))
        out = Path(tmp) / "bulk_deals.json"
        m = dt.run(output_path=out, snapshot_path=snap,
                   watchlist_path=Path(tmp) / "no.json", use_live=False)
        assert out.exists()
        on_disk = json.loads(out.read_text())
        assert on_disk == m
        entries = dt.load_deals(out)
        assert entries["LT.NS"]["net_qty"] == 3000
        # load_deals on a missing/broken file degrades to {}.
        assert dt.load_deals(Path(tmp) / "gone.json") == {}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")

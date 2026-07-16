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
        hist = Path(tmp) / "deals_history.jsonl"
        m = dt.run(output_path=out, snapshot_path=snap,
                   watchlist_path=Path(tmp) / "no.json",
                   history_path=hist, use_live=False)
        assert out.exists() and hist.exists()   # both artifacts, temp paths only
        on_disk = json.loads(out.read_text())
        assert on_disk == m
        entries = dt.load_deals(out)
        assert entries["LT.NS"]["net_qty"] == 3000
        # load_deals on a missing/broken file degrades to {}.
        assert dt.load_deals(Path(tmp) / "gone.json") == {}


def test_live_fetch_survives_homepage_warmup_block():
    """NSE's homepage started 403-ing bots (2026-07-11) while the JSON API
    kept answering — a failed cookie warm-up must not abort the pull."""
    import urllib.error

    payload = {"BULK_DEALS_DATA": [
        {"symbol": "LT", "clientName": "F", "buySell": "BUY",
         "qty": 100, "watp": 10}], "BLOCK_DEALS_DATA": []}
    raw = json.dumps(payload).encode()

    class _Resp:
        def read(self):
            return raw
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            url = req.get_full_url()
            if url == dt._NSE_HOME:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return _Resp()

    saved = dt._nse_opener
    dt._nse_opener = lambda: _Opener()
    try:
        fetched = dt._fetch_nse_largedeals()
    finally:
        dt._nse_opener = saved
    assert fetched is not None
    rows, got_raw = fetched
    assert got_raw == raw
    assert [r["symbol"] for r in rows] == ["LT"]


def test_historical_url_joins_onto_optiontype_query():
    """The historicalOR endpoint already carries ?optionType=... — the
    csv/from/to params must append with '&', each deal type must select
    its own optionType, and csv=true must be requested (the JSON variant
    silently truncates to ~70 rows)."""
    captured = []

    class _Opener:
        def open(self, req, timeout=None):
            captured.append(req.get_full_url())
            raise OSError("stop here — URL is what's under test")

    for deal_type in ("bulk", "block"):
        assert dt._fetch_nse_historical(
            deal_type, date(2023, 7, 11), date(2023, 9, 8),
            opener=_Opener()) is None
    bulk_url, block_url = captured
    assert "optionType=bulk_deals&" in bulk_url
    assert "optionType=block_deals&" in block_url
    for url in (bulk_url, block_url):
        assert "csv=true" in url
        assert "from=11-07-2023&to=08-09-2023" in url
        assert "?from=" not in url and url.count("?") == 1


def test_csv_rows_parse_nse_download_format():
    """The csv=true payload: BOM, trailing-space quoted headers, Indian
    comma grouping — rows come back keyed for normalize_deal."""
    raw = ("﻿\"Date \",\"Symbol \",\"Security Name \",\"Client Name \","
           "\"Buy / Sell \",\"Quantity Traded \","
           "\"Trade Price / Wght. Avg. Price \",\"Remarks \"\n"
           "\"11-JUL-2023\",\"AARTECH\",\"Aartech Solonics Limited\","
           "\"MOHTA SARITA\",\"BUY\",\"70,000\",\"113\",\"-\"\n"
           "\n").encode("utf-8")
    rows = dt._csv_rows(raw)
    assert len(rows) == 1
    row = rows[0]
    deal = dt.normalize_deal(row)
    assert deal["ticker"] == "AARTECH.NS"
    assert deal["side"] == "buy"
    assert deal["qty"] == 70000
    assert deal["price"] == 113
    assert dt.parse_report_date(
        dt._first_field(row, dt._DATE_FIELDS)) == "2023-07-11"


def test_fetch_historical_parses_csv_and_tags_deal_type():
    class _Resp:
        def __init__(self, raw):
            self._raw = raw
        def read(self):
            return self._raw
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    raw = ("\"Date \",\"Symbol \",\"Client Name \",\"Buy / Sell \","
           "\"Quantity Traded \",\"Trade Price / Wght. Avg. Price \"\n"
           "\"12-JUL-2023\",\"TCS\",\"BIG FUND\",\"SELL\",\"1,000\","
           "\"3,400.5\"\n").encode()

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp(raw)

    fetched = dt._fetch_nse_historical(
        "block", date(2023, 7, 12), date(2023, 7, 12), opener=_Opener())
    assert fetched is not None
    rows, got_raw = fetched
    assert got_raw == raw
    assert rows[0]["deal_type"] == "block"
    assert dt.normalize_deal(rows[0])["ticker"] == "TCS.NS"


# --------------------------- review items -> Discord (2026-07-16 directive)

def _census_with_groups(*groups):
    return {"as_of": "2026-07-16",
            "alias_candidates": [{"prefix": g[0].split()[0], "names": list(g)}
                                 for g in groups]}


def test_new_alias_groups_fire_one_discord_card_and_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "census_alerts.jsonl"
        cards = []
        n = dt._notify_review_items(
            _census_with_groups(("SBI MUTUAL FUND", "SBI MUTUAL FUNDS LTD")),
            ledger_path=ledger, notify_fn=cards.append)
        assert n == 1
        assert len(cards) == 1                       # ONE card per run
        assert "human review" in cards[0]
        assert "SBI MUTUAL FUND" in cards[0]
        assert "client_aliases" in cards[0]          # tells the owner WHERE
        assert len(ledger.read_text().splitlines()) == 1


def test_already_announced_groups_never_respam():
    """The ledger is the memory: the same near-dup pair trading every day
    alerts exactly once, not daily."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "census_alerts.jsonl"
        cards = []
        census = _census_with_groups(("GRAVITON RESEARCH", "GRAVITON RESRCH"))
        assert dt._notify_review_items(census, ledger_path=ledger,
                                       notify_fn=cards.append) == 1
        assert dt._notify_review_items(census, ledger_path=ledger,
                                       notify_fn=cards.append) == 0
        assert len(cards) == 1
        # A genuinely NEW group still gets through alongside the old one.
        census2 = _census_with_groups(("GRAVITON RESEARCH", "GRAVITON RESRCH"),
                                      ("AXIS MF", "AXIS MF A/C 2"))
        assert dt._notify_review_items(census2, ledger_path=ledger,
                                       notify_fn=cards.append) == 1
        assert "AXIS MF" in cards[-1] and "GRAVITON" not in cards[-1]


def test_review_notify_is_fail_open():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "census_alerts.jsonl"
        def boom(text):
            raise RuntimeError("webhook down")
        # A dead notifier must not raise — and must NOT mark groups as seen
        # (no ledger write), so they re-announce once Discord is back.
        n = dt._notify_review_items(
            _census_with_groups(("A B", "A B C")),
            ledger_path=ledger, notify_fn=boom)
        assert n == 0
        assert not ledger.exists()
        # No candidates at all -> quiet no-op.
        assert dt._notify_review_items({"alias_candidates": []},
                                       ledger_path=ledger,
                                       notify_fn=lambda t: None) == 0


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

"""
Tests for the NSE historical bulk/block backfill (Phase 1) and the as-of
edge projection it depends on. Fully offline — injected fetchers, temp
ledgers/lakes, in-memory brain_map.

Run either of these from the project folder:
    python tests/test_deals_backfill.py
    python -m pytest tests/test_deals_backfill.py
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, decay_engine, lake
from src.ingestion import deals_tracker as dt
from src.knowledge_graph import entity_affinity as ea


def test_parse_report_date_handles_every_era():
    assert dt.parse_report_date("14-Jul-2023") == "2023-07-14"
    assert dt.parse_report_date("14-07-2023") == "2023-07-14"   # day-first
    assert dt.parse_report_date("2023-07-14") == "2023-07-14"
    assert dt.parse_report_date("05 Aug 2024") == "2024-08-05"
    for junk in (None, "", "not a date", 42):
        assert dt.parse_report_date(junk) is None


def test_backfill_windows_chunking():
    wins = dt._backfill_windows(date(2023, 1, 1), date(2023, 5, 10),
                                window_days=60)
    assert wins[0] == (date(2023, 1, 1), date(2023, 3, 1))
    assert wins[1][0] == date(2023, 3, 2)                # no overlap, no hole
    assert wins[-1][1] == date(2023, 5, 10)
    # Single short range -> one window.
    assert dt._backfill_windows(date(2023, 1, 1), date(2023, 1, 5)) == [
        (date(2023, 1, 1), date(2023, 1, 5))]


def _historical_row(day, symbol, client, side, qty, price):
    return {"BD_DT_DATE": day, "BD_SYMBOL": symbol, "BD_CLIENT_NAME": client,
            "BD_BUY_SELL": side, "BD_QTY_TRD": qty, "BD_TP_WATP": price}


def test_backfill_groups_by_report_date_and_appends_idempotently():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        hist = tmp / "hist.jsonl"
        rows = [
            _historical_row("14-Jul-2023", "ADANIENT", "MISTY SEAS FUND",
                            "BUY", "10,000", "2400"),
            _historical_row("14-Jul-2023", "TCS", "OTHER FII", "SELL",
                            "5,000", "3300"),
            _historical_row("17-Jul-2023", "ADANIENT", "MISTY SEAS FUND",
                            "BUY", "8,000", "2450"),
        ]
        calls = []

        def fetch_fn(deal_type, frm, to):
            calls.append((deal_type, frm, to))
            if deal_type == "bulk":
                return rows, json.dumps({"data": rows}).encode()
            return [], b'{"data": []}'

        stats = dt.backfill(date(2023, 7, 10), date(2023, 7, 20),
                            history_path=hist, watchlist_path=tmp / "no.json",
                            lake_root=tmp / "lake", fetch_fn=fetch_fn,
                            sleep_fn=lambda s: None)
        assert stats["windows"] == 2 and stats["failed_windows"] == 0
        assert stats["days_appended"] == 2 and stats["rows_appended"] == 3
        ledger = dt.read_deal_history(hist)
        assert {r["as_of"] for r in ledger} == {"2023-07-14", "2023-07-17"}
        assert ledger[0]["ticker"] == "ADANIENT.NS"
        # Idempotent: same crawl again appends nothing.
        again = dt.backfill(date(2023, 7, 10), date(2023, 7, 20),
                            history_path=hist, watchlist_path=tmp / "no.json",
                            lake_root=tmp / "lake", fetch_fn=fetch_fn,
                            sleep_fn=lambda s: None)
        assert again["rows_appended"] == 0 and again["days_appended"] == 0


def test_backfill_archives_raw_windows_and_throttles():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sleeps = []

        def fetch_fn(deal_type, frm, to):
            return [], json.dumps({"data": [], "t": deal_type}).encode()

        dt.backfill(date(2023, 1, 1), date(2023, 3, 15),
                    history_path=tmp / "h.jsonl",
                    watchlist_path=tmp / "no.json", lake_root=tmp / "lake",
                    fetch_fn=fetch_fn, sleep_fn=sleeps.append)
        # 2 windows x 2 deal types = 4 requests -> 3 throttle sleeps.
        assert len(sleeps) == 3
        blobs = list((tmp / "lake" / "deals_raw_backfill").rglob("*.json.gz"))
        assert len(blobs) == 4


def test_failed_window_is_counted_never_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def fetch_fn(deal_type, frm, to):
            if deal_type == "block":
                return None                       # simulated 401/timeout
            return [_historical_row("14-Jul-2023", "LT", "F", "BUY",
                                    "100", "3500")], b"{}"

        stats = dt.backfill(date(2023, 7, 14), date(2023, 7, 14),
                            history_path=tmp / "h.jsonl",
                            watchlist_path=tmp / "no.json",
                            lake_root=tmp / "lake", fetch_fn=fetch_fn,
                            sleep_fn=lambda s: None)
        assert stats["failed_windows"] == 1 and stats["rows_appended"] == 1


def test_backfilled_affinity_edges_age_from_their_deal_dates():
    """The whole point of the as-of seam: a 2023-dead entity's link decays
    out on the first sweep instead of reading as born-today."""
    conn = brain_map.connect(":memory:")
    groups = {"ticker_to_group": {"ADANIENT.NS": "ADANI"},
              "groups": {"ADANI": ["ADANIENT.NS"]}, "client_aliases": {}}
    old_hist = [{"ticker": "ADANIENT.NS", "client": "GHOST FUND",
                 "side": "buy", "qty": 1000, "value_rs": 100000.0,
                 "deal_type": "bulk", "as_of": "2023-07-14"}] * 3
    # Give each row a distinct day so per-day idempotency doesn't collapse it.
    for i, r in enumerate(old_hist):
        r = dict(r)
        r["as_of"] = f"2023-07-{14 + i:02d}"
        old_hist[i] = r
    acc = ea.accumulate_entity_affinity(conn, old_hist, groups,
                                        today=date(2026, 7, 10))
    assert acc["edges"] == 1
    row = conn.execute("SELECT valid_from, invalid_at FROM graph_edges "
                       "WHERE source_node = 'GHOST FUND'").fetchone()
    assert row["valid_from"].startswith("2023-07-16")   # latest deal date
    # One decay sweep, three years later: the stale link expires.
    decay_engine.apply_decay_sweep(conn)
    row = conn.execute("SELECT invalid_at FROM graph_edges "
                       "WHERE source_node = 'GHOST FUND'").fetchone()
    assert row["invalid_at"] is not None


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

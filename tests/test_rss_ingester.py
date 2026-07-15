"""
Tests for src/ingestion/rss_ingester.py — the official-RSS news pipeline.

Fully offline: feeds, the network fetch, and the classifier are all
injected; ledgers redirect to tmp; the lake is disabled (write_lake=False).

    python -m pytest tests/test_rss_ingester.py -q
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import text_intelligence as ti
from src.ingestion import rss_ingester as rss

IST = timezone(timedelta(hours=5, minutes=30))
NOW = lambda: datetime(2026, 7, 15, 20, 0, tzinfo=IST)

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>MC Markets</title>
  <item><title>Nifty ends higher on banking gains</title>
    <description>Sensex up 400 points as HDFC Bank rallies.</description>
    <link>https://mc/1</link><pubDate>Tue, 15 Jul 2026 10:00:00 +0530</pubDate></item>
  <item><title>Crude slips on demand worries</title>
    <description>Brent down 2%.</description>
    <link>https://mc/2</link><pubDate>Tue, 15 Jul 2026 09:00:00 +0530</pubDate></item>
</channel></rss>"""

FEED = {"name": "mc_markets", "url": "https://mc/rss", "sector": "markets"}


class FakeExtractor:
    def __init__(self, reachable=True):
        self._r = reachable
        self.calls = 0

    def is_reachable(self):
        return self._r

    def chat_json(self, system, user):
        self.calls += 1
        return {"target_entity": "NIFTY 50", "event_classification": "markets",
                "directional_bias": 0.5, "horizon_impact": "SHORT",
                "confidence_score": 0.6}


# ------------------------------------------------------------- parsing

def test_parse_items_reads_rss_fields():
    items = rss.parse_items(RSS_XML, FEED)
    assert len(items) == 2
    assert items[0]["title"] == "Nifty ends higher on banking gains"
    assert items[0]["sector"] == "markets" and items[0]["feed"] == "mc_markets"
    assert items[0]["link"] == "https://mc/1"
    assert "Sensex" in items[0]["summary"]


def test_parse_items_tolerates_junk():
    assert rss.parse_items("", FEED) == []
    assert rss.parse_items("<not xml", FEED) == []


def test_fetch_feed_fail_open_on_raising_fetch():
    def boom(url):
        raise RuntimeError("network down")
    assert rss.fetch_feed(FEED, fetch_fn=boom) == []


def test_load_feeds_skips_comment_and_bad_rows(tmp_path):
    p = tmp_path / "feeds.json"
    p.write_text(json.dumps({"_comment": "x", "feeds": [
        {"name": "a", "url": "http://a", "sector": "markets"},
        {"name": "b"},                      # no url -> dropped
        {"url": "http://c"}]}))             # no name -> dropped
    feeds = rss.load_feeds(p)
    assert [f["name"] for f in feeds] == ["a"]


# ------------------------------------------------------- run_daily_pull

def _cfg(cap=100):
    return {"rss_backend": "claude", "rss_model": "claude-haiku-4-5",
            "text_intelligence_daily_call_cap": cap}


def test_run_classifies_new_items_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "CALL_LEDGER", tmp_path / "calls.jsonl")
    seen = tmp_path / "seen.jsonl"
    out = tmp_path / "rss_signals.jsonl"
    monkeypatch.setattr(rss, "OUTPUT_PATH", out)
    ex = FakeExtractor()

    r1 = rss.run_daily_pull(feeds=[FEED], extractor=ex,
                            fetch_fn=lambda url: RSS_XML, seen_ledger=seen,
                            config=_cfg(), now_fn=NOW, write_lake=False)
    assert r1["items_seen"] == 2 and r1["new_items"] == 2
    assert r1["classified"] == 2 and ex.calls == 2
    assert len(out.read_text().splitlines()) == 2
    rec = json.loads(out.read_text().splitlines()[0])
    assert rec["sector"] == "markets" and rec["frame"]["target_entity"] == "NIFTY 50"

    # second run: same items are already processed -> nothing new, no calls
    ex2 = FakeExtractor()
    r2 = rss.run_daily_pull(feeds=[FEED], extractor=ex2,
                            fetch_fn=lambda url: RSS_XML, seen_ledger=seen,
                            config=_cfg(), now_fn=NOW, write_lake=False)
    assert r2["new_items"] == 0 and r2["classified"] == 0 and ex2.calls == 0


def test_run_skips_all_classification_when_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "CALL_LEDGER", tmp_path / "calls.jsonl")
    ex = FakeExtractor(reachable=False)
    r = rss.run_daily_pull(feeds=[FEED], extractor=ex,
                           fetch_fn=lambda url: RSS_XML,
                           seen_ledger=tmp_path / "seen.jsonl",
                           config=_cfg(), now_fn=NOW, write_lake=False)
    assert r["unreachable"] is True
    assert r["new_items"] == 2 and r["classified"] == 0 and ex.calls == 0
    # nothing marked processed -> a later reachable run still sees them new
    ex2 = FakeExtractor(reachable=True)
    r2 = rss.run_daily_pull(feeds=[FEED], extractor=ex2,
                            fetch_fn=lambda url: RSS_XML,
                            seen_ledger=tmp_path / "seen.jsonl",
                            config=_cfg(), now_fn=NOW, write_lake=False)
    assert r2["classified"] == 2


def test_run_respects_daily_budget_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "CALL_LEDGER", tmp_path / "calls.jsonl")
    ex = FakeExtractor()
    r = rss.run_daily_pull(feeds=[FEED], extractor=ex,
                           fetch_fn=lambda url: RSS_XML,
                           seen_ledger=tmp_path / "seen.jsonl",
                           config=_cfg(cap=1), now_fn=NOW, write_lake=False)
    assert r["classified"] == 1 and r["skipped_budget"] == 1  # cap of 1


def test_run_fail_opens_on_a_dead_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "CALL_LEDGER", tmp_path / "calls.jsonl")

    def fetch(url):
        if url == "http://dead":
            raise RuntimeError("boom")
        return RSS_XML
    feeds = [{"name": "dead", "url": "http://dead", "sector": "x"},
             dict(FEED)]
    ex = FakeExtractor()
    r = rss.run_daily_pull(feeds=feeds, extractor=ex, fetch_fn=fetch,
                           seen_ledger=tmp_path / "seen.jsonl",
                           config=_cfg(), now_fn=NOW, write_lake=False)
    assert r["feeds_failed"] == 1 and r["feeds_read"] == 1
    assert r["classified"] == 2   # the healthy feed still classified


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python", "-m", "pytest", __file__, "-q"]))

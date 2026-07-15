"""
src/ingestion/rss_ingester.py — official-RSS news pipeline (Data Department)
===========================================================================

Decision #75. Pulls headlines from PUBLISHERS' OWN RSS feeds (Moneycontrol,
Economic Times, Business Standard, …) — never scrapes the sites, so the VM
is never IP-banned (RSS is the sanctioned, polite firehose publishers offer
for exactly this). Deduplicates against a hash ledger, classifies only the
NEW items through the Text Intelligence Manager (#74), and appends the
result to `data/rss_signals.jsonl` + the lake. Advisory / capture-only —
nothing here scores, gates, or proposes a trade (same discipline as
`deals_tracker`: a new source earns its way into forecasts later).

THE MANAGER INTERFACE — the nightly cron calls exactly one function:
    run_daily_pull() -> stats dict
It is fail-open end to end: a dead feed URL, an unreachable LLM, a spent
budget, a bad write — each degrades to a skip, never an exception.

WHY stdlib XML, not `feedparser`: the codebase already parses RSS with
`xml.etree.ElementTree` + a User-Agent'd urllib fetch (see
`news_processor.py`), and Indian financial feeds are plain RSS 2.0. Reusing
that adds ZERO new VM dependency (feedparser isn't installed) and matches
convention. A per-feed try/except covers the occasional odd feed.

LLM CONFIG — this pipeline pins the CHEAP model by design: only light
classification (sector/impact/entities) is needed, not reasoning. The
"sector" is taken from the FEED's own section (config, trusted — the feed
already knows it's markets/economy/industry), so the LLM only supplies the
entity + directional impact via the existing `news_parser` 5-key frame.
Config:
  * `rss_backend`  (optional) — inherits the GLOBAL `text_intelligence_backend`
    (default "ollama") when unset, so this pipeline is COST-SAFE by default:
    on the VM (no Ollama) classification just skips, zero API spend, until
    the owner flips the backend to "claude" AFTER confirming API credits.
  * `rss_model`    (default "claude-haiku-4-5") — the cheap cloud model used
    when the backend is "claude".
Cost is doubly bounded: the manager's shared daily call cap
(`text_intelligence_daily_call_cap`) plus incremental de-dup (only NEW
headlines ever hit the LLM). RSS serves only the latest ~30 items per feed,
so nightly SINGLE-mode classification is the right shape — the Message
Batches API stays for a future historical-archive source, not RSS.

CLI: `python3 -m src.ingestion.rss_ingester`.
"""

import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import text_intelligence as ti

ROOT = Path(__file__).resolve().parent.parent.parent
FEEDS_CONFIG = ROOT / "config" / "rss_feeds.json"
OUTPUT_PATH = ROOT / "data" / "rss_signals.jsonl"
SEEN_LEDGER = ROOT / "logs" / "rss_seen.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

USER_AGENT = "Mozilla/5.0 (compatible; ADiTrader/1.0; +RSS reader)"
HTTP_TIMEOUT = 20          # seconds, per feed
FEED_THROTTLE = 1.5        # seconds between feeds (courteous, ban-averse)
MAX_ITEMS_PER_FEED = 30    # RSS serves the latest slice; cap defensively
SUMMARY_CHARS = 400        # truncate the description sent to the LLM


def load_feeds(config_path: Path = None) -> list:
    """[{"name","url","sector"}, ...] from config/rss_feeds.json, or [] on
    any error. `_comment` and malformed entries are ignored."""
    try:
        data = json.loads((config_path or FEEDS_CONFIG).read_text())
    except (OSError, ValueError) as e:
        print(f"  (rss_ingester: feeds config unreadable: {e})")
        return []
    feeds = []
    for f in data.get("feeds", []):
        if isinstance(f, dict) and f.get("url") and f.get("name"):
            feeds.append({"name": f["name"], "url": f["url"],
                          "sector": f.get("sector", "general")})
    return feeds


def _fetch_xml(url: str) -> str | None:
    """Raw feed XML via a User-Agent'd urllib GET (the news_processor
    pattern; certifi SSL so HTTPS verifies on the VM). None on any error."""
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  (rss_ingester: fetch failed for {url}: {e})")
        return None


def parse_items(xml_text: str, feed: dict) -> list:
    """RSS 2.0 <item> rows -> [{"feed","sector","title","summary","link",
    "published"}]. Tolerant: missing fields become empty; a parse error
    yields []. Atom feeds (rare here) that use <entry> are also handled."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  (rss_ingester: XML parse error for {feed.get('name')}: {e})")
        return []
    nodes = root.findall(".//item") or root.findall(
        ".//{http://www.w3.org/2005/Atom}entry")

    def _txt(node, *tags):
        for t in tags:
            el = node.find(t)
            if el is not None and (el.text or "").strip():
                return el.text.strip()
        return ""

    out = []
    for node in nodes[:MAX_ITEMS_PER_FEED]:
        title = _txt(node, "title", "{http://www.w3.org/2005/Atom}title")
        if not title:
            continue
        summary = _txt(node, "description",
                       "{http://www.w3.org/2005/Atom}summary")[:SUMMARY_CHARS]
        out.append({
            "feed": feed["name"], "sector": feed["sector"],
            "title": title, "summary": summary,
            "link": _txt(node, "link", "{http://www.w3.org/2005/Atom}id"),
            "published": _txt(node, "pubDate",
                              "{http://www.w3.org/2005/Atom}updated"),
        })
    return out


def fetch_feed(feed: dict, fetch_fn=None) -> list:
    """One feed -> its item rows, fail-open ([]) on any error. `fetch_fn`
    (url -> xml str) is the injectable network seam for tests."""
    try:
        xml_text = (fetch_fn or _fetch_xml)(feed["url"])
        return parse_items(xml_text, feed)
    except Exception as e:
        print(f"  (rss_ingester: feed {feed.get('name')} skipped: {e})")
        return []


def _item_text(item: dict) -> str:
    """The short text handed to the classifier — title plus a trimmed
    summary. This IS the dedup key (hashed), so it must be stable."""
    title = item.get("title", "").strip()
    summary = item.get("summary", "").strip()
    return f"{title}. {summary}".strip() if summary else title


def run_daily_pull(*, feeds=None, extractor=None, fetch_fn=None,
                   seen_ledger: Path = None, config: dict = None,
                   throttle: float = FEED_THROTTLE, now_fn=None,
                   write_lake: bool = True) -> dict:
    """THE manager entry point (nightly cron). Fetch every configured feed,
    dedup against the RSS hash ledger, classify only NEW items through the
    Text Intelligence Manager (cheap model, shared daily budget cap), and
    append the classified records to data/rss_signals.jsonl (+ the lake).

    Every seam is injectable for offline tests. Returns a stats dict; never
    raises. Classification is SKIPPED cost-free when the extractor is
    unreachable (e.g. ollama backend on the VM) — items stay un-marked and
    are retried once the cloud backend is enabled."""
    cfg = config if config is not None else ti._config()
    seen_ledger = seen_ledger or SEEN_LEDGER
    now = (now_fn or (lambda: datetime.now(IST)))()
    stats = {"feeds_read": 0, "items_seen": 0, "new_items": 0,
             "classified": 0, "skipped_budget": 0, "unreachable": False,
             "feeds_failed": 0}

    try:
        feed_list = feeds if feeds is not None else load_feeds()
        if extractor is None:
            extractor = ti.get_extractor(
                backend=cfg.get("rss_backend"),
                model=cfg.get("rss_model", "claude-haiku-4-5"), config=cfg)
        reachable = False
        try:
            reachable = bool(extractor.is_reachable())
        except Exception:
            reachable = False
        stats["unreachable"] = not reachable

        # 1) fetch + dedup (fetching is free; do it even when unreachable so
        #    the ledger picture is current, but classify nothing).
        new_items = []
        for i, feed in enumerate(feed_list):
            items = fetch_feed(feed, fetch_fn=fetch_fn)
            if not items:
                stats["feeds_failed"] += 1
            else:
                stats["feeds_read"] += 1
            for item in items:
                stats["items_seen"] += 1
                if not ti.already_processed(_item_text(item), ledger=seen_ledger):
                    new_items.append(item)
            if throttle and i < len(feed_list) - 1 and fetch_fn is None:
                time.sleep(throttle)
        stats["new_items"] = len(new_items)

        if not reachable or not new_items:
            return stats  # nothing to classify (or no cloud) — cost-free exit

        # 2) classify the NEW items (single mode, budget-capped) and store.
        from src.ingestion.news_parser import parse_headline
        records = []
        for item in new_items:
            if not ti.within_daily_budget(config=cfg):
                stats["skipped_budget"] += 1
                continue  # over cap — leave un-marked, retry next run
            text = _item_text(item)
            frame = parse_headline(text, extractor=extractor)
            ti.record_call()                       # one API call spent
            ti.mark_processed(text, ledger=seen_ledger)  # don't re-classify
            stats["classified"] += 1
            records.append({
                "ts": now.isoformat(timespec="seconds"),
                "feed": item["feed"], "sector": item["sector"],
                "title": item["title"], "link": item["link"],
                "published": item["published"], "frame": frame,
            })

        _store(records, now, write_lake=write_lake)
        return stats
    except Exception as e:
        print(f"  (rss_ingester: run failed — failing open: {e})")
        return stats


def _store(records: list, now, write_lake: bool = True) -> None:
    """Append classified records to the ledger + the lake. Swallows all
    errors — a storage failure never changes the pull's stats."""
    if not records:
        return
    try:
        OUTPUT_PATH.parent.mkdir(exist_ok=True)
        with open(OUTPUT_PATH, "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    except OSError as e:
        print(f"  (rss_ingester: ledger write failed: {e})")
    if write_lake:
        try:
            from src import lake
            lake.append_rows("rss", now.date().isoformat(), records)
        except Exception as e:
            print(f"  (rss_ingester: lake write failed: {e})")


def main() -> None:
    stats = run_daily_pull()
    print(f"rss_ingester: {json.dumps(stats)}")


if __name__ == "__main__":
    main()

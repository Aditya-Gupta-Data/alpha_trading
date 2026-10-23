"""
Steps 1-2 of the playbook, node side: collect candidate items and classify them.

Sources (both fail-open):
  * the inbox `data/event_inbox/` — hand-dropped primary texts (.txt / .md / .json with
    {title, text, url, published}); the owner's way to feed a policy document
  * the publishers' RSS feeds already configured for the RSS ingester (config/rss_feeds.json)

Gate (playbook step 1): a rumour, or an item with no primary text, is never classified as a
policy event. Every item is seen once (a hash ledger), so a re-run never re-spends the LLM; a
repeated headline (`title_key`) is skipped before any LLM call.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from src.event_engine import INBOX_DIR, STATE_DIR

TYPES = ("trade_deal", "import_duty", "gst", "pli", "safeguard_duty", "rbi", "sebi", "other")
STAGES = ("rumour", "proposed", "concluded", "signed", "in_force")

CLASSIFY_PROMPT = (
    "You read Indian market news. Decide if the text announces a POLICY or TRADE event that changes "
    "prices or costs for listed companies (trade deal, import duty, GST change, PLI scheme, safeguard "
    "duty, RBI or SEBI rule). Return ONLY JSON: {\"is_policy_event\": bool, \"type\": one of "
    + json.dumps(list(TYPES)) + ", \"issuer\": str, \"stage\": one of " + json.dumps(list(STAGES))
    + ", \"title\": str, \"announced_on\": \"YYYY-MM-DD\" or null, \"key_dates\": {name: date-string}}. "
    "If the text is speculation without an official source, stage is \"rumour\"."
)


def item_id(item: dict) -> str:
    basis = (item.get("url") or "") + "|" + (item.get("title") or "") + "|" + (item.get("text") or "")[:400]
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def title_key(item: dict) -> str | None:
    """'t:' + a hash of the normalised headline (lower-case, HTML entities decoded, punctuation and
    spacing dropped), or None when untitled: one story from two feeds is one item (2026-10-23)."""
    t = re.sub(r"[^a-z0-9]+", " ", html.unescape(item.get("title") or "").lower()).strip()
    return "t:" + hashlib.sha256(t.encode()).hexdigest()[:16] if t else None


def inbox_items(inbox_dir=None) -> list:
    out = []
    root = Path(inbox_dir or INBOX_DIR)
    if not root.is_dir():
        return out
    for f in sorted(root.iterdir()):
        try:
            if f.suffix == ".json":
                d = json.loads(f.read_text())
                out.append({"title": d.get("title") or f.stem, "text": d.get("text") or "",
                            "url": d.get("url") or "", "published": d.get("published") or "",
                            "source": f"inbox:{f.name}"})
            elif f.suffix in (".txt", ".md"):
                text = f.read_text()
                first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), f.stem)
                out.append({"title": first[:200], "text": text, "url": "", "published": "",
                            "source": f"inbox:{f.name}"})
        except (OSError, ValueError):
            continue
    return out


def rss_items(fetch_fn=None) -> list:
    try:
        from src.ingestion import rss_ingester as rss
        rows = []
        for feed in rss.load_feeds():
            for it in rss.fetch_feed(feed, fetch_fn=fetch_fn):
                rows.append({"title": it.get("title", ""), "text": it.get("summary", ""),
                             "url": it.get("link", ""), "published": it.get("published", ""),
                             "source": f"rss:{it.get('feed', '?')}"})
        return rows
    except Exception as exc:
        print(f"  (event_engine: RSS skipped: {exc})")
        return []


class SeenLedger:
    def __init__(self, path=None):
        self.path = Path(path or STATE_DIR / "seen_items.txt")
        try:
            self._seen = set(self.path.read_text().split())
        except OSError:
            self._seen = set()

    def __contains__(self, key):
        return key in self._seen

    def add(self, key):
        if key in self._seen:
            return
        self._seen.add(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(key + "\n")


def classify(item: dict, llm) -> dict:
    """{ok, reason, event} — `event` is the classification, normalised. Gate 1 refuses rumours
    and text-less items before any LLM spend."""
    text = (item.get("text") or "").strip()
    if len(text) < 80:
        return {"ok": False, "reason": "no primary text (gate 1: headline only)", "event": None}
    raw = llm.ask(CLASSIFY_PROMPT, f"TITLE: {item.get('title', '')}\nURL: {item.get('url', '')}\n\n{text[:6000]}")
    if not isinstance(raw, dict):
        return {"ok": False, "reason": getattr(llm, "last_skip", None) or "classification unavailable", "event": None}
    ev = {"type": raw.get("type") if raw.get("type") in TYPES else "other",
          "stage": raw.get("stage") if raw.get("stage") in STAGES else "proposed",
          "issuer": str(raw.get("issuer") or "")[:80], "title": str(raw.get("title") or item.get("title") or "")[:200],
          "announced_on": raw.get("announced_on") or None,
          "key_dates": raw.get("key_dates") if isinstance(raw.get("key_dates"), dict) else {}}
    if not raw.get("is_policy_event"):
        return {"ok": False, "reason": "not a policy event", "event": ev}
    if ev["stage"] == "rumour":
        return {"ok": False, "reason": "rumour without a primary source (gate 1)", "event": ev}
    return {"ok": True, "reason": None, "event": ev}

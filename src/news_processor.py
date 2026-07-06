"""
Alpha Trading — Phase 4D: isolated news & sentiment extractor
=============================================================

A DELIBERATELY DETACHED utility. It fetches recent news headlines for each
watchlist ticker, asks Google Gemini to condense them into one small number
per stock, and writes a tiny index to data/news_sentiment.json. That JSON is
the ONLY thing the rest of the system ever reads — the core trading scripts
never see raw article text (PRD "State Isolation Principle").

To keep that isolation real, this file imports NO core trading code
(no strategy/portfolio/journal/suggestions). It only reads the watchlist and
its own .env, and writes one JSON file.

Output — data/news_sentiment.json:
    {
      "generated": "<ISO timestamp>",
      "source": "gemini" | "fallback",
      "tickers": {
        "ONGC.NS": {
          "sentiment_score": 2,          # int, -5 (bearish) .. +5 (bullish)
          "headline_focus": "oil price rise",   # <= 3-word driver
          "last_updated": "<ISO timestamp>",
          "stale": false                 # true == neutral fallback, not real
        }, ...
      }
    }

If Gemini is unavailable (no GEMINI_API_KEY, network/quota failure, or an
unparseable reply), EVERY ticker is written as a neutral score 0 with
stale=true, so downstream never crashes on missing news and can tell that
the number is a placeholder rather than a real read.

Setup: put GEMINI_API_KEY=... in .env (free key from Google AI Studio).
Run:   python3 -m src.news_processor
"""

import json
import os
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import certifi
import yaml

# Python's urllib doesn't use the system CA store, so on macOS (and minimal
# Linux VMs) HTTPS fails with CERTIFICATE_VERIFY_FAILED. certifi ships a CA
# bundle (listed explicitly in requirements.txt); point SSL at it so both the
# news RSS fetch and the Gemini call verify cleanly everywhere.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
WATCHLIST_PATH = ROOT / "config" / "watchlist.yaml"
OUTPUT_PATH = ROOT / "data" / "news_sentiment.json"

# "-latest" alias, not a pinned version: pinned model names get deprecated
# and start 404ing (gemini-2.0-flash did, as of 2026-07). The alias always
# points at Google's current lite/cheap flash-tier model.
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
HEADLINES_PER_TICKER = 6
HTTP_TIMEOUT = 20  # seconds


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_tickers() -> list:
    """De-duped watchlist tickers, read straight from the YAML (no import of
    src.suggest, to keep this script isolated)."""
    if not WATCHLIST_PATH.exists():
        return []
    with open(WATCHLIST_PATH) as f:
        config = yaml.safe_load(f) or {}
    seen, tickers = set(), []
    for item in config.get("watchlist", []):
        ticker = item.get("ticker")
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def fetch_headlines(ticker: str) -> list:
    """Top Google News RSS headlines for a ticker. Returns [] on any error —
    a ticker with no headlines simply gets a neutral read downstream."""
    symbol = ticker.split(".")[0]
    query = urllib.parse.quote(f"{symbol} stock NSE")
    url = (f"https://news.google.com/rss/search?q={query}"
           f"&hl=en-IN&gl=IN&ceid=IN:en")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"  {ticker}: headline fetch failed ({e})")
        return []
    # Select item titles specifically — the channel's own <title> (the feed
    # name) is excluded, so we don't waste a slot / feed noise to the LLM.
    titles = [t.text for t in root.findall(".//item/title") if t.text]
    return titles[:HEADLINES_PER_TICKER]


def _build_prompt(headlines_by_ticker: dict) -> str:
    blocks = []
    for ticker, headlines in headlines_by_ticker.items():
        joined = "\n".join(f"  - {h}" for h in headlines) or "  (no recent headlines)"
        blocks.append(f"{ticker}:\n{joined}")
    body = "\n\n".join(blocks)
    return (
        "You are a markets news classifier for Indian (NSE) stocks. For each "
        "ticker below, read its recent headlines and judge the near-term "
        "sentiment for that stock.\n\n"
        "Return ONLY a JSON object mapping each ticker to an object with:\n"
        '  "sentiment_score": integer from -5 (very bearish) to +5 (very '
        "bullish), 0 if neutral or unclear,\n"
        '  "headline_focus": at most 3 words naming the main driver '
        '(e.g. "earnings beat", "oil prices", "regulatory probe").\n\n'
        "Do not include any ticker not listed. Do not add commentary.\n\n"
        f"{body}"
    )


def _call_gemini(prompt: str, api_key: str) -> dict:
    """Single batched call. Raises on any failure so the caller can fall back."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
        body = json.loads(resp.read())
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def _clean_entry(raw: dict, now: str) -> dict:
    """Coerce one model result into the strict output schema."""
    try:
        score = int(round(float(raw.get("sentiment_score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(-5, min(5, score))
    focus = str(raw.get("headline_focus", "")).strip() or "no clear driver"
    focus = " ".join(focus.split()[:3])
    return {
        "sentiment_score": score,
        "headline_focus": focus,
        "last_updated": now,
        "stale": False,
    }


def _neutral_entry(now: str) -> dict:
    return {
        "sentiment_score": 0,
        "headline_focus": "no data",
        "last_updated": now,
        "stale": True,
    }


def build_sentiment(tickers: list) -> dict:
    """Fetch + score all tickers into the output schema. Never raises: on any
    LLM failure every ticker becomes a neutral, stale entry."""
    now = _now()
    if not tickers:
        return {"generated": now, "source": "fallback", "tickers": {}}

    headlines_by_ticker = {t: fetch_headlines(t) for t in tickers}
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        print("  GEMINI_API_KEY not set — writing neutral sentiment (stale).")
        return {
            "generated": now, "source": "fallback",
            "tickers": {t: _neutral_entry(now) for t in tickers},
        }

    try:
        scored = _call_gemini(_build_prompt(headlines_by_ticker), api_key)
    except Exception as e:
        print(f"  Gemini call failed ({e}) — writing neutral sentiment (stale).")
        return {
            "generated": now, "source": "fallback",
            "tickers": {t: _neutral_entry(now) for t in tickers},
        }

    out = {}
    for ticker in tickers:
        raw = scored.get(ticker)
        out[ticker] = _clean_entry(raw, now) if isinstance(raw, dict) else _neutral_entry(now)
    return {"generated": now, "source": "gemini", "tickers": out}


def write_sentiment(data: dict) -> None:
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(data, f, indent=2)


def run() -> dict:
    _load_env()
    tickers = load_tickers()
    print(f"News processor: scoring {len(tickers)} ticker(s)...")
    data = build_sentiment(tickers)
    write_sentiment(data)
    real = sum(1 for e in data["tickers"].values() if not e["stale"])
    print(f"Wrote {OUTPUT_PATH.name}: source={data['source']}, "
          f"{real}/{len(data['tickers'])} real reads.")
    return data


if __name__ == "__main__":
    run()

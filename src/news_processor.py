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

Output — data/news_sentiment.json (v3, dual-horizon — owner spec 2026-07-20):
    {
      "generated": "<ISO timestamp>",
      "source": "gemini" | "fallback",
      "tickers": {
        "ONGC.NS": {
          "sentiment_score": 2,          # int, -5..+5 — ALWAYS equals the
                                         # short-term score (back-compat:
                                         # forecast/evidence/brain_map read
                                         # this key and keep working)
          "short_term_catalyst_score": 2,  # days/weeks — the swing driver
          "long_term_macro_score": 1,      # months/structural narrative
                                           # (None = model gave no read;
                                           # unknown is not neutral)
          "headline_focus": "oil price rise",   # <= 3-word driver
          "last_updated": "<ISO timestamp>",
          "stale": false,                # true == neutral fallback, not real
          "prev": {                      # the PRIOR run's fresh read, or null
            "short_term": -2, "long_term": 1,
            "read_at": "<ISO timestamp>"
          },
          "reversal": {                  # drastic overnight narrative break
            "short_term": true,          # (crossed neutral, moved >= 3 pts
            "long_term": false           #  vs prev — see detect_reversal)
          }
        }, ...
      }
    }

A flagged reversal on any horizon also fires ONE compact Discord note per
run (fail-open, lazy src.notifier import — the only, deliberately
side-door, non-core import; trading state stays untouched).

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
from datetime import datetime, timedelta, timezone
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
        "ticker below, read its recent headlines and judge the sentiment for "
        "that stock on TWO separate horizons.\n\n"
        "Return ONLY a JSON object mapping each ticker to an object with:\n"
        '  "short_term_catalyst_score": integer from -5 (very bearish) to +5 '
        "(very bullish) for the coming days/weeks — catalysts, results, "
        "orders, upgrades/downgrades, price-moving events. 0 if neutral or "
        "unclear,\n"
        '  "long_term_macro_score": integer from -5 to +5 for the '
        "months-ahead structural story — business model, sector cycle, "
        "regulation, competitive position. 0 if neutral or unclear. Judge "
        "the two INDEPENDENTLY (a stock can have a bad quarter inside a "
        "strong structural story, and vice versa),\n"
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


def _coerce_score(value, default=0):
    """One score -> a clamped int in [-5, 5], or `default` when unusable.
    `default=None` distinguishes 'the model gave nothing' from 'neutral'."""
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(-5, min(5, score))


def _clean_entry(raw: dict, now: str) -> dict:
    """Coerce one model result into the strict output schema.

    v3 (owner spec 2026-07-20): TWO horizons per share —
    short_term_catalyst_score (days; what a swing trade rides) and
    long_term_macro_score (months/structural narrative). `sentiment_score`
    stays present and EQUALS the short-term score, so every existing
    consumer (forecast, evidence, brain_map ingest) keeps working
    unchanged. A model answering the old single-score schema feeds
    short-term from it; its long-term is None — unknown is not neutral,
    and None can never fire a reversal flag."""
    short = _coerce_score(raw.get("short_term_catalyst_score",
                                  raw.get("sentiment_score", 0)))
    long_ = _coerce_score(raw.get("long_term_macro_score"), default=None)
    focus = str(raw.get("headline_focus", "")).strip() or "no clear driver"
    focus = " ".join(focus.split()[:3])
    return {
        "sentiment_score": short,
        "short_term_catalyst_score": short,
        "long_term_macro_score": long_,
        "headline_focus": focus,
        "last_updated": now,
        "stale": False,
        "prev": None,
        "reversal": {"short_term": False, "long_term": False},
    }


def _neutral_entry(now: str) -> dict:
    return {
        "sentiment_score": 0,
        "short_term_catalyst_score": 0,
        "long_term_macro_score": 0,
        "headline_focus": "no data",
        "last_updated": now,
        "stale": True,
        "prev": None,
        "reversal": {"short_term": False, "long_term": False},
    }


# How old a sentiment entry may be before consumers must ignore it. 48h
# tolerates ONE missed 19:10 refresh (yesterday's read still speaks) but
# no more. Chosen after the 2026-07-05→16 incident, where this job was
# unscheduled for 11 days and forecast.py kept trading a July-5 read at
# full weight the whole time.
NEWS_MAX_AGE_HOURS = 48


def entry_is_fresh(entry: dict, now: datetime = None,
                   max_age_hours: float = NEWS_MAX_AGE_HOURS) -> bool:
    """True when a ticker entry's last_updated is recent enough to act on.

    The `stale` flag alone is NOT freshness: it only records "the Gemini
    call did not fail" at write time and never ages afterwards, so an
    entry can sit stale=false for weeks if this job stops running.
    Freshness is a time judgment made at READ time — here, in the module
    that owns the file format, so forecast.py and confluence/evidence.py
    can never disagree about it. A missing or unparseable timestamp is
    NOT fresh: news is an advisory driver, so excluding it is the honest
    default."""
    if not isinstance(entry, dict):
        return False
    ts = entry.get("last_updated")
    if not ts:
        return False
    try:
        written = datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - written) <= timedelta(hours=max_age_hours)


# A reversal = the narrative crossed (or touched) neutral AND moved at
# least this many points since the previous fresh read. +5→+2 is the same
# story softening (no flag); +2→−2 is a different story (flag); +3→0 is a
# strong story evaporating (flag). Owner spec 2026-07-20: "ekdum change
# agar ho toh flag karo" — drastic changes only, both horizons.
REVERSAL_MIN_DELTA = 3


def detect_reversal(prev, today) -> bool:
    """True when today's score is a drastic break from the previous read.
    None on either side means 'unknown', and unknown never flags."""
    if prev is None or today is None:
        return False
    return abs(today - prev) >= REVERSAL_MIN_DELTA and prev * today <= 0


def link_previous(entry: dict, prev_entry: dict) -> dict:
    """Stamp `prev` + `reversal` onto a freshly-cleaned entry from the
    prior run's entry for the same ticker. The baseline must be REAL and
    RECENT: a stale (fallback-neutral) prev is a fake number, and an aged
    prev is not \"kal\" — both leave prev=None and the flags down (the
    same entry_is_fresh gate every consumer uses, Issue 22). A legacy
    single-score prev links short-term from sentiment_score; its
    long-term baseline is unknown, so the long flag can never fire."""
    if not isinstance(prev_entry, dict) or prev_entry.get("stale", True) \
            or not entry_is_fresh(prev_entry):
        return entry
    prev_short = _coerce_score(prev_entry.get("short_term_catalyst_score",
                                              prev_entry.get("sentiment_score")),
                               default=None)
    prev_long = _coerce_score(prev_entry.get("long_term_macro_score"),
                              default=None)
    entry["prev"] = {"short_term": prev_short, "long_term": prev_long,
                     "read_at": prev_entry.get("last_updated")}
    entry["reversal"] = {
        "short_term": detect_reversal(prev_short,
                                      entry["short_term_catalyst_score"]),
        "long_term": detect_reversal(prev_long,
                                     entry["long_term_macro_score"]),
    }
    return entry


def _load_previous(path: Path = None) -> dict:
    """Ticker -> entry from the PRIOR run's output file, read before this
    run overwrites it. {} on any problem — first run, missing, unreadable."""
    path = Path(path) if path is not None else OUTPUT_PATH
    try:
        data = json.loads(path.read_text())
        tickers = data.get("tickers", {})
        return tickers if isinstance(tickers, dict) else {}
    except (OSError, ValueError):
        return {}


def _default_notify(text: str) -> None:
    """One-line Discord note (the discovery/ops pattern): fail-open, and
    the notifier muzzles itself under pytest (Phase 6J guard)."""
    try:
        import asyncio
        from src.notifier import send_discord_message
        asyncio.run(send_discord_message(text))
    except Exception as exc:
        print(f"  (news processor: notify failed [{exc}])")


def _reversal_note(flagged: list) -> str:
    """One compact card line for all of today's flagged reversals."""
    parts = []
    for ticker, entry in flagged:
        prev = entry["prev"] or {}
        segs = []
        if entry["reversal"]["short_term"]:
            segs.append(f"ST {prev.get('short_term'):+d}→"
                        f"{entry['short_term_catalyst_score']:+d}")
        if entry["reversal"]["long_term"]:
            segs.append(f"LT {prev.get('long_term'):+d}→"
                        f"{entry['long_term_macro_score']:+d}")
        parts.append(f"{ticker} {' / '.join(segs)}"
                     f" ({entry['headline_focus']})")
    return "📰 Sentiment reversal: " + "; ".join(parts)


def build_sentiment(tickers: list, previous_path: Path = None,
                    notify_fn=None) -> dict:
    """Fetch + score all tickers into the output schema. Never raises: on any
    LLM failure every ticker becomes a neutral, stale entry.

    v3: each real read is LINKED to the prior run's read for the same
    ticker (`prev` + `reversal` flags, both horizons), and one compact
    Discord note fires when anything flagged — the owner sees "kal +3
    bola tha, aaj -4" the moment the narrative breaks. Fallback (stale)
    runs never link and never flag: a fake 0 must not read as a collapse.
    `previous_path`/`notify_fn` are injectable for tests."""
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

    previous = _load_previous(previous_path)
    out, flagged = {}, []
    for ticker in tickers:
        raw = scored.get(ticker)
        if not isinstance(raw, dict):
            out[ticker] = _neutral_entry(now)
            continue
        entry = link_previous(_clean_entry(raw, now), previous.get(ticker))
        out[ticker] = entry
        if entry["reversal"]["short_term"] or entry["reversal"]["long_term"]:
            flagged.append((ticker, entry))
    if flagged:
        note = _reversal_note(flagged)
        print(f"  {note}")
        (notify_fn or _default_notify)(note)
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

"""
FastAPI web layer — read-only dashboard, paper mode only.

Imports from the existing engine (data_fetcher, rules) without modifying them.
Run with:  uvicorn src.web.api:app --reload
"""

from datetime import datetime, timedelta
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse

from src.data_fetcher import get_quote
from src.rules import check_rule, describe

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "watchlist.yaml"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Alpha Dashboard", docs_url=None, redoc_url=None)

# 30-second in-memory cache so rapid UI refreshes don't hammer yfinance
_quote_cache: dict = {}
_cache_at: datetime | None = None
_CACHE_TTL = timedelta(seconds=30)


def _load_watchlist() -> list:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("watchlist", []) if cfg else []


def _get_quotes() -> dict:
    global _quote_cache, _cache_at
    now = datetime.utcnow()
    if _cache_at and now - _cache_at < _CACHE_TTL:
        return _quote_cache
    items = _load_watchlist()
    quotes = {}
    for item in items:
        t = item["ticker"]
        if t not in quotes:
            quotes[t] = get_quote(t)
    _quote_cache = quotes
    _cache_at = now
    return quotes


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/watchlist")
def watchlist():
    items = _load_watchlist()
    quotes = _get_quotes()
    result = []
    for item in items:
        t = item["ticker"]
        q = quotes.get(t)
        result.append({
            "ticker": t,
            "condition": item["condition"],
            "value": item["value"],
            "price": q["current_price"] if q else None,
            "percent_change": q["percent_change"] if q else None,
            "error": q is None,
        })
    return result


@app.get("/api/alerts")
def alerts():
    items = _load_watchlist()
    quotes = _get_quotes()
    triggered = []
    for item in items:
        t = item["ticker"]
        q = quotes.get(t)
        if q and check_rule(q, item["condition"], item["value"]):
            triggered.append({
                "ticker": t,
                "message": describe(q, item["condition"], item["value"]),
                "price": q["current_price"],
                "percent_change": q["percent_change"],
                "condition": item["condition"],
                "value": item["value"],
            })
    return triggered


@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")

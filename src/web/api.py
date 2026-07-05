"""
FastAPI web layer — paper mode only. Read-only on the market side.

Imports the existing engine (data_fetcher, rules) without modifying it. Watchlist
edits go through src.web.watchlist_store, which writes config/watchlist.yaml while
preserving its comments. No broker, no orders, no API keys.

Run with:  uvicorn src.web.api:app --reload
"""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.data_fetcher import get_quote
from src.rules import check_rule, describe
from src.web import watchlist_store as store

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Alpha Dashboard", docs_url=None, redoc_url=None)

# 30-second in-memory cache so rapid UI refreshes don't hammer yfinance.
_quote_cache: dict = {}
_cache_at: datetime | None = None
_CACHE_TTL = timedelta(seconds=30)


def _get_quotes(invalidate: bool = False) -> dict:
    """Fetch one quote per distinct ticker in the watchlist (cached 30s)."""
    global _quote_cache, _cache_at
    now = datetime.utcnow()
    if not invalidate and _cache_at and now - _cache_at < _CACHE_TTL:
        return _quote_cache
    quotes = {}
    for item in store.load_items():
        t = item.get("ticker")
        if t and t not in quotes:
            quotes[t] = get_quote(t)
    _quote_cache = quotes
    _cache_at = now
    return quotes


def _invalidate_cache() -> None:
    global _cache_at
    _cache_at = None


class AddRequest(BaseModel):
    symbol: str
    type: str = "stock"  # "stock" or "index"


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/watchlist")
def watchlist():
    """
    One entry per instrument (grouped by ticker), each with its type, live price,
    % change today, and the list of alert rules configured for it (may be empty
    for a watch-only item).
    """
    items = store.load_items()
    quotes = _get_quotes()

    grouped: dict = {}
    order: list = []
    for item in items:
        t = item.get("ticker")
        if not t:
            continue
        if t not in grouped:
            order.append(t)
            type_ = item.get("type") or store.infer_type(t)
            q = quotes.get(t)
            grouped[t] = {
                "ticker": t,
                "symbol": store.display_name(t, type_),
                "type": type_,
                "exchange": store.exchange_of(t, type_),
                "price": q["current_price"] if q else None,
                "percent_change": q["percent_change"] if q else None,
                "error": q is None,
                "rules": [],
            }
        if "condition" in item and item.get("condition") is not None:
            grouped[t]["rules"].append({
                "condition": item["condition"],
                "value": item.get("value"),
            })

    return [grouped[t] for t in order]


@app.get("/api/alerts")
def alerts():
    """Only the rules that are triggered right now."""
    items = store.load_items()
    quotes = _get_quotes()
    triggered = []
    for item in items:
        condition = item.get("condition")
        if condition is None:
            continue  # watch-only entry, nothing to evaluate
        t = item.get("ticker")
        q = quotes.get(t)
        if q and check_rule(q, condition, item["value"]):
            triggered.append({
                "ticker": t,
                "message": describe(q, condition, item["value"]),
                "price": q["current_price"],
                "percent_change": q["percent_change"],
                "condition": condition,
                "value": item["value"],
            })
    return triggered


@app.post("/api/watchlist")
def add_to_watchlist(req: AddRequest):
    """Add a stock or index. Validates a live price before saving; rejects if invalid."""
    result = store.add_item(req.symbol, req.type)
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    _invalidate_cache()
    return result


@app.delete("/api/watchlist/{symbol:path}")
def delete_from_watchlist(symbol: str):
    """Remove an instrument (and any of its rules) by its ticker, e.g. RELIANCE.NS or ^NSEI."""
    result = store.remove_item(symbol)
    if not result.get("ok"):
        return JSONResponse(status_code=404, content=result)
    _invalidate_cache()
    return result


@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")

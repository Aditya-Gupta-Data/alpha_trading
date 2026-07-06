"""
Alpha Trading — unified local REST API (the frontend's single entry point)
==========================================================================

ONE FastAPI app for the whole local frontend. It merges what used to be two
separate apps:
  * the market-data / watchlist dashboard layer (formerly src/web/api.py), and
  * the analyst / decision / scorecard engine layer.

It imports the existing engine modules (data_fetcher, rules, suggestions,
strategy, portfolio, journal, web.watchlist_store) WITHOUT changing them —
no indicator math, rule logic, or persistence is re-implemented here. This
file is engine-side and framework-free (no frontend imports); it is the only
thing the frontend calls, and the frontend never reads data/ files directly.

Routes (all JSON unless noted):
  GET    /api/health                 liveness + mode
  GET    /api/watchlist              instruments + live price + rules
  POST   /api/watchlist              add a stock/index (validates a price)
  DELETE /api/watchlist/{symbol}     remove an instrument and its rules
  GET    /api/alerts                 rules triggered right now
  POST   /api/chat                   analyst logic -> generated trade plan(s)
  POST   /api/decision               Approve / Paper / Dismiss -> journal (+paper)
  GET    /api/scorecard              journaled outcomes rolled up for the UI
  GET    /                           the legacy static dashboard (src/web/static)

Design rule: PAPER ONLY. There is no broker anywhere. A REAL-money decision
is refused on purpose (see /api/decision) — the strongest thing this API can
do is a paper buy/sell against data/portfolio.json, using the same rails as
the terminal `src.trade` session.

Run:  uvicorn src.api:app --reload --port 8000
"""

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src import journal
from src import portfolio as pf
from src.data_fetcher import get_quote
from src.rules import check_rule, describe
from src.strategy import propose_plans
from src.suggestions import analyze
from src.web import watchlist_store as store

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

app = FastAPI(title="Alpha Trading Local API", docs_url="/api/docs", redoc_url=None)

# The Vite/Bun dev server runs on localhost (its exact port varies), so a
# localhost-only regex keeps this open for local frontend dev without
# exposing the API to the internet.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================ market data
# (formerly src/web/api.py — watchlist + alerts, read-only on the market)

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


@app.get("/api/watchlist")
def watchlist():
    """One entry per instrument (grouped by ticker): type, live price, %
    change, and the alert rules configured for it (may be empty)."""
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
    """Add a stock or index. Validates a live price before saving."""
    result = store.add_item(req.symbol, req.type)
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    _invalidate_cache()
    return result


@app.delete("/api/watchlist/{symbol:path}")
def delete_from_watchlist(symbol: str):
    """Remove an instrument (and any of its rules) by ticker, e.g. RELIANCE.NS."""
    result = store.remove_item(symbol)
    if not result.get("ok"):
        return JSONResponse(status_code=404, content=result)
    _invalidate_cache()
    return result


# ================================================================= /api/chat

class ChatRequest(BaseModel):
    ticker: str | None = None
    message: str | None = None


def _plan_to_payload(plan: dict) -> dict:
    """Flatten an engine plan dict into the flat shape the frontend's
    PLAN_DECISION payload uses."""
    stop = plan.get("stop_loss") or {}
    target = plan.get("target") or {}
    capital = round(plan["shares"] * plan["price"], 2)
    return {
        "ticker": plan["ticker"],
        "action": plan["action"],
        "variant": plan.get("variant", "primary"),
        "signal": plan["signal"],
        "entry": plan["price"],
        "entry_rule": plan.get("entry_rule"),
        "target": target.get("price"),
        "stop_price": stop.get("price"),
        "stop_pct": stop.get("pct"),
        "rr_ratio": plan.get("risk_reward"),
        "max_risk": plan.get("max_loss_rs"),
        "position_size": plan["shares"],
        "capital": capital,
        "invalidation": plan.get("invalidation"),
        "rationale": plan.get("rationale"),
    }


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Run the analyst logic for one ticker and return the generated plan(s).

    Mirrors what a `src.trade` session does per ticker: analyze -> propose
    plans against the current paper portfolio and live prices. Read-only:
    nothing is journaled here."""
    if not req.ticker:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Provide a `ticker`, e.g. ONGC.NS."},
        )
    ticker = req.ticker.strip().upper()
    if "." not in ticker:
        ticker += ".NS"

    analysis = analyze(ticker)
    if analysis is None:
        return {
            "ok": True, "ticker": ticker, "plans": [],
            "reply": f"Not enough price history for {ticker} to build a plan "
                     f"yet (needs ~200 trading days).",
        }

    book = pf.load()
    prices = {ticker: analysis["price"]}
    plans = propose_plans(analysis, book, prices)
    payloads = [_plan_to_payload(p) for p in plans]

    if payloads:
        primary = payloads[0]
        reply = (f"{ticker}: {primary['signal']}. Plan — enter ~Rs."
                 f"{primary['entry']:,.2f}, stop Rs.{primary['stop_price']:,.2f}, "
                 f"target Rs.{primary['target']:,.2f} "
                 f"({primary['rr_ratio']:g}:1), {primary['position_size']} shares.")
    else:
        trend = "uptrend" if analysis["uptrend"] else "downtrend"
        reply = (f"{ticker}: no trade signal right now ({trend}, "
                 f"RSI {analysis['rsi']:.0f} if available). Nothing to propose.")

    return {"ok": True, "ticker": ticker, "plans": payloads, "reply": reply}


# ============================================================= /api/decision

class DecisionRequest(BaseModel):
    ticker: str
    decision: str                 # "APPROVE_REAL" | "PAPER_TRADE" | "DISMISS"
    signal: str = "manual"
    action: str = "BUY"           # "BUY" | "SELL" (dashboard flow is usually BUY)
    entry: float
    target: float | None = None
    stop_price: float | None = None
    stop_pct: float | None = None
    rr_ratio: float | None = None
    max_risk: float | None = None
    position_size: int = 0
    capital: float | None = None
    why: str = ""
    pattern_tags: list[str] = []
    plan_id: str | None = None


def _decision_to_proposal(req: DecisionRequest) -> dict:
    """Rebuild the engine `proposal` dict journal.new_entry expects from the
    flat frontend payload."""
    proposal = {
        "action": req.action.upper(),
        "ticker": req.ticker,
        "shares": req.position_size,
        "price": req.entry,
        "signal": req.signal,
        "variant": "primary",
        "entry_rule": f"{req.action.upper()} {req.position_size} @ ~Rs.{req.entry:,.2f}",
        "risk_reward": req.rr_ratio,
        "max_loss_rs": req.max_risk,
        "invalidation": None,
        "rationale": req.why or None,
    }
    if req.stop_price is not None:
        proposal["stop_loss"] = {"pct": req.stop_pct, "price": req.stop_price}
    if req.target is not None:
        proposal["target"] = {"price": req.target, "rr": req.rr_ratio}
    return proposal


@app.post("/api/decision")
def decision(req: DecisionRequest):
    """The local replacement for the frontend's Supabase `emit()`.

    * DISMISS       -> journals a rejected decision (nothing executes).
    * PAPER_TRADE   -> executes a paper buy/sell against data/portfolio.json
                       (same cash / 25%-per-stock rails as the terminal) and
                       journals it as approved.
    * APPROVE_REAL  -> REFUSED. This project is paper-only by design; there
                       is no broker. The UI keeps the button, but the engine
                       will not pretend to place a real order.
    """
    decision_kind = req.decision.upper()

    if decision_kind == "APPROVE_REAL":
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "Real-money execution is disabled by design — this "
                         "engine is paper-only and has no broker connection. "
                         "Use PAPER_TRADE to log a paper position.",
            },
        )

    proposal = _decision_to_proposal(req)
    book = pf.load()
    executed = False

    if decision_kind == "PAPER_TRADE":
        try:
            if proposal["action"] == "BUY":
                shares = min(
                    proposal["shares"],
                    pf.max_affordable_shares(book, proposal["price"], {proposal["ticker"]: proposal["price"]}),
                )
                if shares <= 0:
                    return JSONResponse(
                        status_code=400,
                        content={"ok": False, "error": "Paper rails reject this: "
                                 "0 affordable shares (cash or 25%/stock cap)."},
                    )
                proposal["shares"] = shares
                pf.buy(book, proposal["ticker"], shares, proposal["price"])
            else:  # SELL
                if proposal["ticker"] not in book["holdings"]:
                    return JSONResponse(
                        status_code=400,
                        content={"ok": False, "error": f"Cannot sell "
                                 f"{proposal['ticker']} — no paper position held."},
                    )
                proposal["shares"] = book["holdings"][proposal["ticker"]]["shares"]
                pf.sell(book, proposal["ticker"], proposal["price"])
            pf.save(book)
            executed = True
        except ValueError as e:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
        journal_decision = "approved"
    else:  # DISMISS
        journal_decision = "rejected"

    entry = journal.new_entry(
        proposal, journal_decision, req.why or "(no reason given)",
        sl_pct=req.stop_pct, size=req.capital, pattern_tags=req.pattern_tags,
    )
    journal.log(entry)

    return {
        "ok": True,
        "decision": journal_decision,
        "executed_on_paper": executed,
        "entry": entry,
        "portfolio": {"cash": round(book["cash"], 2), "holdings": book["holdings"]},
    }


# ============================================================ /api/scorecard

def _derive_archetype(signal: str) -> str:
    """Same buckets the frontend's deriveArchetype() uses, computed here so
    the engine stays the single source of truth."""
    s = (signal or "").lower()
    if "breakout" in s:
        return "Breakout"
    if "pullback" in s or "dip" in s:
        return "Pullback"
    if "golden" in s:
        return "Golden Cross"
    if "death" in s:
        return "Death Cross"
    if "election" in s:
        return "Election Front-Run"
    if "earnings" in s:
        return "Earnings Reaction"
    return signal or "Manual Setup"


@app.get("/api/scorecard")
def scorecard():
    """Roll up journaled outcomes for the Scorecard UI: overall win/loss/flat
    totals plus per-archetype win-rate and average R-multiple. Read-only."""
    entries = journal.read_all()
    scored = [e for e in entries if e.get("outcome")]

    wins = losses = flat = 0
    archetypes: dict = {}

    for e in scored:
        verdict = e["outcome"].get("verdict", "")
        good = verdict.startswith(("WIN", "GOOD"))
        bad = verdict.startswith(("LOSS", "MISSED", "SHOULD"))
        if good:
            wins += 1
        elif bad:
            losses += 1
        else:
            flat += 1

        name = _derive_archetype(e.get("signal", ""))
        a = archetypes.setdefault(name, {"archetype": name, "sample_size": 0,
                                         "_wins": 0, "_r_total": 0.0, "_r_n": 0})
        a["sample_size"] += 1
        if good:
            a["_wins"] += 1
        r = e["outcome"].get("r_multiple")
        if r is not None:
            a["_r_total"] += r
            a["_r_n"] += 1

    archetype_stats = []
    for a in archetypes.values():
        archetype_stats.append({
            "archetype": a["archetype"],
            "sample_size": a["sample_size"],
            "win_rate": round(a["_wins"] / a["sample_size"] * 100, 1) if a["sample_size"] else 0.0,
            "avg_r": round(a["_r_total"] / a["_r_n"], 2) if a["_r_n"] else None,
        })

    total = wins + losses + flat
    return {
        "generated": date.today().isoformat(),
        "summary": {
            "scored": total,
            "good_calls": wins,
            "bad_calls": losses,
            "flat": flat,
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
        },
        "archetype_stats": archetype_stats,
        "open_positions": pf.load()["holdings"],
    }


# ================================================================== misc

@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "paper-only"}


@app.get("/")
def dashboard():
    """The legacy static dashboard (kept from the old web layer)."""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"status": "ok", "note": "No static dashboard bundled; "
                         "use the Bun frontend against this API."})

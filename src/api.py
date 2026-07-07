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

When exposed via Cloudflare Tunnel (or any public ingress), set API_KEY in
.env — every route then requires a matching X-API-Key header (or
Authorization: Bearer <key>), except GET /api/health for liveness probes.
Leave API_KEY unset for localhost-only dev (no auth).
"""

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src import journal
from src import notifier
from src import portfolio as pf
from src.data_fetcher import get_quote
from src.plan_tracker import run_tracker
from src.rules import check_rule, describe
from src.strategy import propose_plans
from src.suggestions import analyze
from src.web import watchlist_store as store

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


def _load_env() -> None:
    """Load .env into os.environ (same self-contained reader the other entry
    points use) so GEMINI_API_KEY is available without an external loader."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_env()

# Optional gate for public exposure (Cloudflare Tunnel). Unset = open (local dev).
_PUBLIC_PATHS = frozenset({"/api/health"})


def _read_api_key() -> str | None:
    """Configured API_KEY from the environment, or None when auth is off."""
    raw = os.environ.get("API_KEY") or ""
    key = raw.strip().strip('"').strip("'")
    return key or None


def _extract_api_key(request: Request) -> str | None:
    """Client key from X-API-Key or Authorization: Bearer."""
    header = request.headers.get("X-API-Key")
    if header and header.strip():
        return header.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def _keys_match(provided: str, expected: str) -> bool:
    if len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require API_KEY when configured. OPTIONS and /api/health stay open."""

    async def dispatch(self, request: Request, call_next):
        expected = _read_api_key()
        if expected is None:
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        provided = _extract_api_key(request)
        if not provided or not _keys_match(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Unauthorized — valid X-API-Key required."},
            )
        return await call_next(request)


if _read_api_key():
    print("[API] API_KEY is set — all routes except GET /api/health require "
          "X-API-Key (or Authorization: Bearer).", flush=True)

# How often the background market-refresh loop runs. 1 hour by default;
# overridable via env (handy for tests) but never below a sane floor.
AUTO_SYNC_INTERVAL = max(5, int(os.environ.get("AUTO_SYNC_INTERVAL_SECONDS", "3600")))

# How often the fast polling loop runs. 60 seconds by default.
POLL_INTERVAL = max(5, int(os.environ.get("WATCHLIST_POLL_INTERVAL_SECONDS", "60")))

# Keep track of notified breaches to avoid duplicate emails/logs within the same day.
# Format: (ticker, condition, value, date_str)
_notified_breaches: set[tuple[str, str, float, str]] = set()


async def _poll_watchlist_loop() -> None:
    """Every POLL_INTERVAL seconds: poll prices via DhanHQ for all watchlist items,
    evaluate configured alert rules, and trigger notifications on new breaches.
    Blocking engine calls run in a worker thread so the event loop never stalls."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            items = await asyncio.to_thread(store.load_items)
            if not items:
                continue

            # Identify unique tickers that have rules
            tickers_with_rules = set()
            rules_by_ticker = {}
            for item in items:
                ticker = item.get("ticker")
                condition = item.get("condition")
                value = item.get("value")
                if ticker and condition is not None and value is not None:
                    tickers_with_rules.add(ticker)
                    rules_by_ticker.setdefault(ticker, []).append((condition, value))

            if not tickers_with_rules:
                continue

            # Fetch fresh quotes for these tickers via DhanHQ (in worker thread)
            quotes = {}
            for ticker in tickers_with_rules:
                q = await asyncio.to_thread(get_quote, ticker)
                if q:
                    quotes[ticker] = q
                await asyncio.sleep(0.01)

            # Evaluate alert rules
            today_str = date.today().isoformat()
            new_breaches = []

            for ticker, rules in rules_by_ticker.items():
                q = quotes.get(ticker)
                if not q:
                    continue
                for condition, value in rules:
                    key = (ticker, condition, value, today_str)
                    if key in _notified_breaches:
                        continue  # Already notified today

                    if check_rule(q, condition, value):
                        message = describe(q, condition, value)
                        new_breaches.append(message)
                        _notified_breaches.add(key)

            # Trigger alert notifications if any new breaches occurred
            if new_breaches:
                print(f"[Watchlist Poll] {len(new_breaches)} new breach(es) detected!", flush=True)
                for line in new_breaches:
                    print(f"  [ALERT] {line}", flush=True)

                from src.notifier import send_digest
                await asyncio.to_thread(send_digest, "ADiTrader Watchlist Alert", new_breaches)
                await notifier.send_discord_message(
                    "\n".join(["🔔 **ADiTrader Watchlist Alert**"] + new_breaches))

        except Exception as e:
            print(f"[Watchlist Poll] Error in background poll loop: {e}", flush=True)


async def _auto_sync_loop() -> None:
    """Every AUTO_SYNC_INTERVAL seconds: resolve OPEN paper trades against the
    market (same logic as POST /api/sync-market) and refresh the watchlist
    price cache. Blocking engine calls run in a worker thread so the event
    loop (and HTTP serving) never stalls. One bad cycle never kills the loop."""
    while True:
        await asyncio.sleep(AUTO_SYNC_INTERVAL)
        try:
            episodes: list = []
            resolved = await asyncio.to_thread(run_tracker, False, episodes.append)
            await asyncio.to_thread(_get_quotes, True)  # invalidate + refetch
            print(f"[Auto-Sync] 1-hour market refresh complete — resolved "
                  f"{resolved} open trade(s); watchlist price cache refreshed.",
                  flush=True)
            # Episodic encoding: push each resolution's context snapshot to
            # Discord. Fail-safe — an unconfigured webhook returns False.
            for episode in episodes:
                await notifier.send_discord_message(notifier.format_episode(episode))
        except Exception as e:
            print(f"[Auto-Sync] refresh failed (will retry next cycle): {e}",
                  flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[Auto-Sync] background market refresh armed — running every "
          f"{AUTO_SYNC_INTERVAL}s.", flush=True)
    print(f"[Watchlist Poll] background watchlist polling armed — running every "
          f"{POLL_INTERVAL}s.", flush=True)
    task_sync = asyncio.create_task(_auto_sync_loop())
    task_poll = asyncio.create_task(_poll_watchlist_loop())
    try:
        yield
    finally:
        task_sync.cancel()
        task_poll.cancel()
        try:
            await asyncio.gather(task_sync, task_poll, return_exceptions=True)
        except Exception:
            pass
        print("[Auto-Sync] background loops stopped.", flush=True)


app = FastAPI(title="Alpha Trading Local API", docs_url="/api/docs",
              redoc_url=None, lifespan=lifespan)

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
app.add_middleware(ApiKeyMiddleware)


# ============================================================ market data
# (formerly src/web/api.py — watchlist + alerts, read-only on the market)

# 30-second in-memory cache so rapid UI refreshes don't hammer the Dhan
# quote API (which is rate-limited).
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
#
# The analyst pipeline talks to Google Gemini DIRECTLY via the official
# google-genai SDK using the local GEMINI_API_KEY — there is NO Lovable AI
# Gateway (or any cloud gateway) in this path. Structured trade math still
# comes from the local engine (strategy.py); Gemini only does the natural-
# language analyst reply and free-thesis -> plan translation.

GEMINI_MODEL = "gemini-flash-lite-latest"  # -latest alias: survives model deprecations

_gemini_client = None


def _get_gemini():
    """Lazily build (and cache) the google-genai client from GEMINI_API_KEY.
    Returns None if the key isn't set, so callers can degrade gracefully
    instead of crashing (mirrors news_processor's fallback philosophy)."""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    from google import genai  # imported lazily so the API boots without the SDK
    _gemini_client = genai.Client(api_key=key)
    return _gemini_client


def _gemini_json(system_prompt: str, user_text: str, temperature: float) -> dict | None:
    """One Gemini call that returns parsed JSON, or None on any failure."""
    client = _get_gemini()
    if client is None:
        return None
    from google.genai import types
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                temperature=temperature,
            ),
        )
        return json.loads(resp.text)
    except Exception as e:
        print(f"[/api/chat] Gemini call failed: {e}", flush=True)
        return None


_ANALYST_PROMPT = (
    "You are the ADiTrader AI Lead Analyst — a seasoned, human-sounding "
    "portfolio analyst on an Indian (NSE) equities desk. Reply like a real "
    "analyst on a trading floor: concise, direct, no corporate boilerplate, "
    "no fake system-log preamble.\n\n"
    "Classify the user's message into exactly one intent:\n"
    "- \"conversation\": greetings, small talk, status checks, meta questions. "
    "Reply naturally in 1-2 short sentences. Do NOT produce a trade plan.\n"
    "- \"analysis\": a real market thesis, a named ticker, or a macro/catalyst "
    "setup. Reply in 2-4 tight sentences framing your read, and note that a "
    "structured trade plan is being prepared.\n\n"
    "Return ONLY valid JSON: {\"intent\":\"conversation\"|\"analysis\","
    "\"reply\":string}"
)

_PLAN_PROMPT = (
    "You are ADiTrader's Autonomous Signal Engine, an elite quant analyst on "
    "NSE-listed Indian equities. Translate a free-form macro thesis into ONE "
    "mathematically sound trade plan.\n"
    "- Pick ONE liquid NSE ticker that best expresses the thesis.\n"
    "- Choose bias LONG or SHORT to fit the thesis.\n"
    "- Use realistic current-ish INR entry prices.\n"
    "- target and stop must be on the correct side of entry for the bias.\n"
    "- stop_pct between 1.5 and 8. rr between 1.2 and 4.5.\n"
    "- archetype: short Title-Case label (e.g. \"Election Front-Run\").\n"
    "- rationale: 1-2 crisp sentences citing mechanism + precedent.\n"
    "- tags: 2-4 short labels without '#'.\n"
    "- asset_class defaults \"EQUITY\"; use \"OPTIONS\" only for clear convex/"
    "event plays (then also give strike_price, expiry_date ISO YYYY-MM-DD, "
    "option_type CALL/PUT).\n"
    "Return ONLY valid JSON of shape: {\"ticker\":string,\"name\":string,"
    "\"archetype\":string,\"bias\":\"LONG\"|\"SHORT\",\"entry\":number,"
    "\"target\":number,\"stop_pct\":number,\"rr\":number,\"rationale\":string,"
    "\"tags\":string[],\"asset_class\":\"EQUITY\"|\"OPTIONS\","
    "\"strike_price\":number|null,\"expiry_date\":string|null,"
    "\"option_type\":\"CALL\"|\"PUT\"|null}"
)


def _num_or(v, fallback: float) -> float:
    try:
        n = float(v)
        return n if n > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def _coerce_plan(o: dict) -> dict:
    """Coerce Gemini's plan JSON into a safe, direction-consistent payload
    (mirrors the old frontend coerce so the UI contract is unchanged)."""
    o = o or {}
    ticker = str(o.get("ticker", "RELIANCE")).upper()[:24]
    bias = "SHORT" if o.get("bias") == "SHORT" else "LONG"
    entry = _num_or(o.get("entry"), 1000)
    target = _num_or(o.get("target"), entry * (1.06 if bias == "LONG" else 0.94))
    if bias == "LONG" and target <= entry:
        target = entry * 1.05
    if bias == "SHORT" and target >= entry:
        target = entry * 0.95
    stop_pct = _clamp(_num_or(o.get("stop_pct"), 3), 1.5, 8)
    rr = _clamp(_num_or(o.get("rr"), abs(target - entry) / (entry * stop_pct / 100)), 1.2, 4.5)
    tags = [str(t).lstrip("#")[:30] for t in (o.get("tags") or [])][:5]
    asset_class = "OPTIONS" if o.get("asset_class") == "OPTIONS" else "EQUITY"
    return {
        "ticker": ticker,
        "name": str(o.get("name", ticker))[:40],
        "archetype": str(o.get("archetype", "Manual Thesis"))[:60],
        "bias": bias,
        "entry": round(entry, 2),
        "target": round(target, 2),
        "stop_pct": round(stop_pct, 2),
        "rr": round(rr, 2),
        "rationale": str(o.get("rationale", "System-generated plan from thesis."))[:500],
        "tags": tags,
        "asset_class": asset_class,
        "strike_price": _num_or(o.get("strike_price"), entry) if asset_class == "OPTIONS" else None,
        "expiry_date": o.get("expiry_date") if asset_class == "OPTIONS" else None,
        "option_type": (o.get("option_type") or ("CALL" if bias == "LONG" else "PUT"))
                       if asset_class == "OPTIONS" else None,
    }


def _plan_to_payload(plan: dict) -> dict:
    """Flatten an engine strategy plan into the flat shape the frontend uses."""
    stop = plan.get("stop_loss") or {}
    target = plan.get("target") or {}
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
        "capital": round(plan["shares"] * plan["price"], 2),
        "invalidation": plan.get("invalidation"),
        "rationale": plan.get("rationale"),
    }


class ChatRequest(BaseModel):
    ticker: str | None = None
    message: str | None = None
    thesis: str | None = None
    threadTag: str | None = None


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Unified analyst endpoint — local engine + direct Gemini (no gateway).

    Three modes by input:
      * thesis  -> Gemini translates a free-form thesis into a structured
                   trade plan (mode "plan", key `generated_plan`).
      * message -> Gemini gives a conversational analyst reply + intent
                   (mode "chat"). If a watchlist ticker is named, the local
                   strategy engine's plan(s) ride along in `plans`.
      * ticker  -> pure local engine: analyze + propose_plans (mode "ticker").
    Read-only: nothing is journaled here."""

    # --- thesis -> plan (Gemini) ---
    if req.thesis and req.thesis.strip():
        raw = _gemini_json(_PLAN_PROMPT, f"Thesis:\n{req.thesis.strip()}\n\n"
                           "Return the JSON now.", temperature=0.4)
        if raw is None:
            return JSONResponse(status_code=503, content={
                "ok": False, "mode": "plan",
                "error": "Gemini unavailable (check GEMINI_API_KEY in .env).",
            })
        return {"ok": True, "mode": "plan", "generated_plan": _coerce_plan(raw)}

    # --- message -> conversational analyst reply (Gemini) ---
    if req.message and req.message.strip():
        user = (f"Active thread: {req.threadTag}\nUser message: {req.message}"
                if req.threadTag else f"User message: {req.message}")
        raw = _gemini_json(_ANALYST_PROMPT, user, temperature=0.7)
        if raw is None:
            reply = ("On the desk, but my analyst brain (Gemini) is offline — "
                     "check GEMINI_API_KEY in .env.")
            return {"ok": True, "mode": "chat", "intent": "conversation",
                    "reply": reply, "plans": []}
        intent = "analysis" if raw.get("intent") == "analysis" else "conversation"
        reply = str(raw.get("reply") or
                    ("Reading the setup now — structured plan coming through."
                     if intent == "analysis" else "On the desk. What are you seeing?"))
        return {"ok": True, "mode": "chat", "intent": intent, "reply": reply}

    # --- ticker -> pure local engine (strategy.py) ---
    if req.ticker and req.ticker.strip():
        ticker = req.ticker.strip().upper()
        if "." not in ticker:
            ticker += ".NS"
        analysis = analyze(ticker)
        if analysis is None:
            return {"ok": True, "mode": "ticker", "ticker": ticker, "plans": [],
                    "reply": f"Not enough price history for {ticker} yet "
                             f"(needs ~200 trading days)."}
        book = pf.load()
        prices = {ticker: analysis["price"]}
        payloads = [_plan_to_payload(p) for p in propose_plans(analysis, book, prices)]
        if payloads:
            p = payloads[0]
            reply = (f"{ticker}: {p['signal']}. Plan — enter ~Rs.{p['entry']:,.2f}, "
                     f"stop Rs.{p['stop_price']:,.2f}, target Rs.{p['target']:,.2f} "
                     f"({p['rr_ratio']:g}:1), {p['position_size']} shares.")
        else:
            trend = "uptrend" if analysis["uptrend"] else "downtrend"
            reply = f"{ticker}: no trade signal right now ({trend})."
        return {"ok": True, "mode": "ticker", "ticker": ticker,
                "plans": payloads, "reply": reply}

    return JSONResponse(status_code=400, content={
        "ok": False, "error": "Provide `thesis`, `message`, or `ticker`."})


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


_RESOLUTION_TO_OUTCOME = {
    "target_hit": "TARGET_HIT",
    "stop_hit": "STOP_HIT",
    "time_stop": "MANUAL_CLOSE",
}


def _executed_trade(entry: dict, idx: int) -> dict:
    """Map an APPROVED journal entry to the frontend's ExecutedTrade shape."""
    plan = entry.get("plan") or {}
    stop = plan.get("stop_loss") or {}
    target = plan.get("target") or {}
    outcome = entry.get("outcome") or {}
    rev = entry.get("review") or {}
    return {
        "id": f"{entry['date']}-{entry['ticker']}-{idx}",
        "date": entry["date"],
        "ticker": entry["ticker"],
        "archetype": _derive_archetype(entry.get("signal", "")),
        "bias": "LONG" if entry["action"] == "BUY" else "SHORT",
        "entry_price": entry["price"],
        "target_price": target.get("price", 0) or 0,
        "stop_price": stop.get("price", 0) or 0,
        "exit_price": outcome.get("price", 0) or 0,
        "position_size": entry["shares"],
        "capital_deployed": round(entry["shares"] * entry["price"], 2),
        "outcome": _RESOLUTION_TO_OUTCOME.get(outcome.get("resolution"), "OPEN"),
        "r_multiple": outcome.get("r_multiple") or 0,
        "net_pnl": outcome.get("pnl_rs") or 0,
        "mode": "PAPER",
        "created_at": entry["date"],
        # Post-mortem review (from /api/review), so the modal can pre-fill.
        "pm_right": rev.get("pm_right"),
        "pm_wrong": rev.get("pm_wrong"),
        "pm_error_category": rev.get("pm_error_category"),
        "reviewed_at": rev.get("reviewed_at"),
    }


def _skipped_trade(entry: dict, idx: int) -> dict:
    """Map a REJECTED journal entry to the frontend's SkippedTrade shape."""
    plan = entry.get("plan") or {}
    stop = plan.get("stop_loss") or {}
    target = plan.get("target") or {}
    outcome = entry.get("outcome") or {}
    verdict_text = outcome.get("verdict", "")
    if verdict_text.startswith("GOOD"):
        verdict = "GOOD_SKIP"
    elif verdict_text.startswith("MISSED"):
        verdict = "MISSED_GAIN"
    else:
        verdict = "PENDING"
    return {
        "id": f"{entry['date']}-{entry['ticker']}-{idx}",
        "date": entry["date"],
        "ticker": entry["ticker"],
        "archetype": _derive_archetype(entry.get("signal", "")),
        "bias": "LONG" if entry["action"] == "BUY" else "SHORT",
        "proposed_entry": entry["price"],
        "proposed_target": target.get("price", 0) or 0,
        "proposed_stop": stop.get("price", 0) or 0,
        "hypothetical_r": outcome.get("r_multiple") or 0,
        "hypothetical_pnl": outcome.get("pnl_rs") or 0,
        "verdict": verdict,
        "reject_reason": entry.get("why"),
        "created_at": entry["date"],
    }


@app.get("/api/scorecard")
def scorecard():
    """Roll up journaled outcomes for the Scorecard UI: overall win/loss/flat
    totals, per-archetype win-rate and average R-multiple, plus the executed
    and skipped trade rows the ledger tables render. Read-only."""
    entries = journal.read_all()
    scored = [e for e in entries if e.get("outcome")]

    executed_trades, skipped_trades = [], []
    for i, e in enumerate(entries):
        if e.get("decision") == "approved":
            executed_trades.append(_executed_trade(e, i))
        elif e.get("decision") == "rejected":
            skipped_trades.append(_skipped_trade(e, i))

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
        "executed_trades": executed_trades,
        "skipped_trades": skipped_trades,
        "open_positions": pf.load()["holdings"],
    }


# =============================================================== /api/review

class ReviewRequest(BaseModel):
    ticker: str
    date: str
    pm_right: str | None = None
    pm_wrong: str | None = None
    pm_error_category: str | None = None
    # Optional: the scorecard row id ("<date>-<ticker>-<journalIndex>"), used
    # to pinpoint the exact journal line when a ticker was traded more than
    # once on the same day.
    id: str | None = None


def _find_review_target(entries: list, req: ReviewRequest):
    """Locate the journal entry a post-mortem belongs to. Prefer the exact
    line index encoded in the scorecard row id; fall back to the first entry
    matching ticker + date. Returns the index, or None if not found."""
    if req.id:
        try:
            idx = int(req.id.rsplit("-", 1)[1])
        except (ValueError, IndexError):
            idx = None
        if idx is not None and 0 <= idx < len(entries):
            e = entries[idx]
            if e.get("ticker") == req.ticker and e.get("date") == req.date:
                return idx
    for i, e in enumerate(entries):
        if e.get("ticker") == req.ticker and e.get("date") == req.date:
            return i
    return None


@app.post("/api/review")
def review(req: ReviewRequest):
    """Attach a post-mortem (what went right/wrong + error category) to a
    specific journal entry. Writes the review fields onto that JSON object in
    data/journal.jsonl. This is additive — it never alters the trade's own
    fields or its scored outcome."""
    entries = journal.read_all()
    idx = _find_review_target(entries, req)
    if idx is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"No journal entry for "
                     f"{req.ticker} on {req.date}."},
        )

    entries[idx]["review"] = {
        "pm_right": (req.pm_right or "").strip() or None,
        "pm_wrong": (req.pm_wrong or "").strip() or None,
        "pm_error_category": req.pm_error_category or None,
        "reviewed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    journal.rewrite_all(entries)
    return {"ok": True, "ticker": req.ticker, "date": req.date,
            "review": entries[idx]["review"]}


# =========================================================== /api/sync-market

@app.post("/api/sync-market")
def sync_market():
    """Resolve OPEN paper trades against the market.

    Delegates to the Phase 4C plan tracker (src/plan_tracker.py) rather than
    re-implementing resolution here, so there is ONE source of truth for how a
    trade resolves. The tracker scans each open plan's daily OHLC since entry:
      * price traded through the stop  -> STOP_HIT
      * price traded through the target -> TARGET_HIT
      * neither within plan_max_days    -> time stop (closed at market)
    On a resolved BUY it also closes the paper position in portfolio.json at
    the exit price (bracket-order semantics), and writes r_multiple / pnl_rs /
    days_in_trade into the entry's `outcome` — exactly what /api/scorecard
    reads. Same-day stop+target ambiguity breaks pessimistically (stop first).

    Note: only plan-carrying trades are trackable. Older entries with no
    stop/target (e.g. a pre-4B manual buy) are intentionally left OPEN here —
    they are scored by the 7-day review path instead, not resolved on a
    stop/target that was never set. Email digest is suppressed for the API.
    """
    try:
        resolved = run_tracker(email=False)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"Market sync failed: {e}"},
        )
    return {
        "ok": True,
        "resolved": resolved,
        "message": (f"Resolved {resolved} trade(s) against the market."
                    if resolved else
                    "No open plan-carrying trades resolved (nothing hit its "
                    "stop or target yet)."),
    }


# ================================================================== misc

@app.get("/api/health")
def health():
    payload = {"status": "ok", "mode": "paper-only"}
    if _read_api_key():
        payload["auth"] = "required"
    return payload


@app.get("/")
def dashboard():
    """The legacy static dashboard (kept from the old web layer)."""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"status": "ok", "note": "No static dashboard bundled; "
                         "use the Bun frontend against this API."})

# DATA_CONTRACT.md — Frontend ⇄ Python Engine Contract

**Audience: the frontend builder (Lovable or any web/mobile client).**
This file is the single source of truth for every JSON structure the Python
backend produces. Build UI state against these shapes exactly.

## Branch strategy (adopted 2026-07-05) — READ FIRST

This repo uses a **decoupled two-branch model**:

- **`main`** — the core Python trading engine (`src/`). Runs headless (cloud
  cron jobs, Discord bot) with no Node/build step. Backend code stays strictly
  independent of any frontend framework.
- **`lovable-ui`** — the Lovable web/mobile frontend. It reads schemas from
  `main` (this file) but **commits all frontend code to `lovable-ui`, never to
  `main`.**

The two branches are kept isolated on purpose: frontend churn must not touch
the execution pipeline. **Do not merge `lovable-ui` into `main`** (or vice
versa) unless the repo owner explicitly says so. When the UI needs backend
data, the fix is a new read-only endpoint added on `main` (see Part 3), not
frontend code creeping into the engine.

Why this file exists: all live engine output is written to `data/`, which is
**git-ignored** (it holds the user's personal paper portfolio and journal).
You will never see real data files in this repository — do not guess their
shape from the Python source; it is documented precisely here instead.

## Ground rules for the frontend

1. **Display-only client.** Do NOT re-implement indicators (SMA/RSI), rule
   evaluation, news parsing, sentiment scoring, position sizing, or any
   engine math in JavaScript. The Python engine computes; the client renders.
2. **Paper trading only, human-in-the-loop.** There is no broker anywhere in
   this system. Never build order-placement UI. The strongest verbs the UI
   may ever offer are Approve / Dismiss on engine-generated proposals — and
   even those are not yet exposed over HTTP (see "Not yet served" below).
3. **Currency is INR** (display as `Rs.` or `₹`), tickers are Yahoo Finance
   style: `RELIANCE.NS` (NSE stock), `^NSEI` (index).
4. **Nullable means nullable.** Fields marked nullable WILL be null in real
   data (e.g. quote fetch failures, unresolved outcomes). Render gracefully.

---

## Part 1 — Live HTTP API (served by the unified `src/api.py`, FastAPI)

Base: wherever `uvicorn src.api:app` runs (default `http://localhost:8000`).
All responses JSON. As of 2026-07-06 this is ONE app — the old `src/web/api.py`
was merged into `src/api.py`, so watchlist/alerts, chat, decision, and
scorecard all live under the same server.

### `GET /api/health`
```json
{ "status": "ok" }
```

### `GET /api/watchlist`
One entry per instrument; quotes cached 30s server-side.
```json
[
  {
    "ticker": "RELIANCE.NS",
    "symbol": "RELIANCE",
    "type": "stock",              // "stock" | "index"
    "exchange": "NSE",
    "price": 1304.0,              // null if the quote fetch failed
    "percent_change": -0.42,      // vs previous close; null on failure
    "error": false,               // true when the quote fetch failed
    "rules": [                    // may be empty (watch-only instrument)
      { "condition": "percent_up",   "value": 3 },
      { "condition": "percent_down", "value": 3 }
    ]
  }
]
```
`condition` is one of exactly: `price_above`, `price_below`, `percent_up`,
`percent_down`. `value` is a price (Rs.) for the first two, a percentage for
the last two.

### `GET /api/alerts`
Only rules that are triggered *right now* (empty array = quiet day).
```json
[
  {
    "ticker": "INFY.NS",
    "message": "INFY.NS is up 5.64% today (Rs.1047.20)",
    "price": 1047.2,
    "percent_change": 5.64,
    "condition": "percent_up",
    "value": 3
  }
]
```

### `POST /api/watchlist`
Body: `{ "symbol": "TCS", "type": "stock" }` (`type` optional, default
`"stock"`, or `"index"`). Validates a live price before saving.
Success `200`: `{ "ok": true, ... }` · Failure `400`: `{ "ok": false, "error": "..." }`

### `DELETE /api/watchlist/{ticker}`
E.g. `/api/watchlist/RELIANCE.NS`. `200 {"ok": true}` or `404 {"ok": false}`.

---

## Part 2 — Engine artifacts NOT yet served over HTTP

These are the JSON files/structures the Phase 3–4 engine writes locally.
Since 2026-07-06 three of them ARE now served by `src/api.py`
(`POST /api/chat` returns generated plans, `POST /api/decision` writes a
decision to the journal + paper portfolio, `GET /api/scorecard` rolls up
journaled outcomes). The raw files below (portfolio, full journal, news
sentiment, forecast, brain weights) still have no direct endpoint. When the
dashboard needs one, the backend adds a read-only FastAPI route in
`src/api.py` serving these exact shapes — the frontend must NOT stub them
with invented fields.

### 2.1 Paper portfolio — `data/portfolio.json`
```json
{
  "cash": 75091.06,
  "holdings": {
    "ONGC.NS": { "shares": 106, "avg_price": 234.99 }
  },
  "created": "2026-07-03"
}
```
Total value = cash + Σ(shares × latest price). Starting capital was
Rs. 1,00,000 (fake). Whole shares only; one stock ≤ 25% of the portfolio.

### 2.2 Trade journal — `data/journal.jsonl` (one JSON object per line)
Entries evolve by phase; the frontend must tolerate all three generations.

**Oldest (pre-plan) entry:**
```json
{
  "date": "2026-07-03",
  "action": "BUY",                     // "BUY" | "SELL"
  "ticker": "ONGC.NS",
  "shares": 106,
  "price": 234.99,
  "signal": "uptrend with a dip (RSI 26) — buying the pullback",
  "decision": "approved",              // "approved" | "rejected"
  "why": "testing default suggestions",
  "outcome": null                      // null until scored
}
```

**Current entries add** (all optional in old lines — check before reading):
```json
{
  "risk_levers": { "sl_pct": 3.0, "size": 10000 },
  "pattern_tags": ["RSI_Oversold"],
  "plan": {                            // null on entries without a plan
    "variant": "primary",              // "primary" | "alternative"
    "entry_rule": "Buy at market (~Rs.234.99)",
    "stop_loss": { "pct": 3.0, "price": 227.94 },   // null on SELL plans
    "target":    { "price": 249.09, "rr": 2.0 },    // null on SELL plans
    "risk_reward": 2.0,                // null on SELL plans
    "max_loss_rs": 747.3,              // null on SELL plans
    "invalidation": "a daily close below Rs.227.94 ...",
    "rationale": "uptrend with a dip ... 2:1 reward-to-risk."
  }
}
```

**`outcome`, once filled, comes in two flavors:**

Simple 7-day review (old-style entries, `src/review.py`):
```json
{ "checked": "2026-07-10", "price": 241.10, "pct": 2.6,
  "verdict": "WIN — it rose after you bought" }
```

Plan tracker (plan-carrying entries, `src/plan_tracker.py`):
```json
{
  "checked": "2026-07-20",
  "resolution": "target_hit",          // "stop_hit" | "target_hit" | "time_stop"
  "price": 249.09,                     // exit price
  "exit_date": "2026-07-18",
  "pct": 6.0,
  "r_multiple": 2.0,                   // nullable
  "days_in_trade": 15,
  "pnl_rs": 1494.6,
  "hypothetical": false,               // true when the plan was rejected
  "position_closed": true,
  "verdict": "WIN — target hit"
}
```
Verdict strings always begin with one of: `WIN`, `LOSS`, `GOOD SKIP`,
`GOOD HOLD`, `MISSED GAIN`, `SHOULD HAVE SOLD`, `flat` — safe to key
badge colors off that prefix.

### 2.3 News sentiment — `data/news_sentiment.json`
```json
{
  "generated": "2026-07-05T07:36:22+00:00",
  "source": "gemini",                  // "gemini" (real) | "fallback" (placeholder)
  "tickers": {
    "TCS.NS": {
      "sentiment_score": -5,           // int, -5 bearish .. +5 bullish
      "headline_focus": "sharp price crash",   // ≤ 3 words
      "last_updated": "2026-07-05T07:36:22+00:00",
      "stale": false                   // true == neutral placeholder, NOT a real read
    }
  }
}
```
When `stale` is true, render "no news data", never a neutral-0 gauge.

### 2.4 Forecast (computed on demand by `src/forecast.py`, not persisted)
```json
{
  "ticker": "TCS.NS",
  "bias": "bearish",                   // "bullish" | "bearish" | "neutral"
  "confidence": 60,                    // int percent 0-100
  "score": -6.0,                       // signed checklist points (±10 nominal)
  "drivers": [                         // 1-5 strings, strongest first
    "steady downtrend (50-day SMA below 200-day)",
    "negative news — sharp price crash (sentiment -5/5)"
  ],
  "time_horizon": "swing (multi-day to multi-week)",
  "price": 2093.5
}
```

### 2.5 Learning-loop weights — `data/brain_weights.json`
```json
{
  "generated": "2026-07-05T12:13:14+00:00",
  "min_samples": 5,
  "resolved_trade_count": 0,
  "sample_counts": { "fresh_cross": 0 },
  "weights": { "fresh_cross": 1.0, "rsi_oversold": 1.0 },  // 1.0 = neutral, clamp [0.5, 1.5]
  "pattern_tag_report": {
    "Breakout": { "count": 3, "avg_r_multiple": 1.2 }
  }
}
```

### 2.6 Coming later (do not build against yet)
`data/brain_map.db` (SQLite, Phase 6 macro-event memory) — schema is still
being designed; it will be exposed only through backend endpoints, never
read directly by the client.

---

## Part 3 — How to request a new endpoint

The frontend never reads `data/` files directly and never fakes them. When a
screen needs Part-2 data, the backend side adds a read-only route (e.g.
`GET /api/portfolio`, `GET /api/journal`, `GET /api/forecast`,
`GET /api/sentiment`) returning the exact shapes above. Treat any endpoint
not listed in Part 1 as "planned, not yet available" and design loading /
empty states for it.

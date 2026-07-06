# Decisions

Choices we've locked in, and why. Update this whenever we make a new call so we
never have to re-argue it in a future chat.

| # | Decision | Why |
|---|----------|-----|
| 1 | Tool is for personal use only | No multi-user infrastructure, no compliance overhead of serving others. |
| 2 | Market: Indian stocks (NSE/BSE) | User trades from India; Indian brokers have the most API-friendly setups. |
| 3 | Language: Python | Best ecosystem for finance libraries; user is non-technical, so Claude writes the code. |
| 4 | Data source (for now): yfinance | Free, no API key, supports NSE (.NS) / BSE (.BO). Can lag ~15 min — fine for alerts. Swap to a broker feed (Zerodha/Upstox) when live trading is added. |
| 5 | Alert rules are configurable, not fixed | User wants to decide triggers over time; engine is built so new condition types are a one-function add. |
| 6 | Build order: alerting -> suggesting -> trading | Incremental; each phase stacks on a stable base. Extra steps inserted as needed. |
| 7 | Repo on GitHub | https://github.com/Aditya-Gupta-Data/alpha_trading |
| 8 | UI via Google Stitch, added later | Design there, wire up logic behind it. Phase 1 uses notifications, not a dashboard. |
| 9 | Trading phase will start in paper-trading mode with safety rails | Test with fake money before any real order. |

| 10 | Alert delivery: email, not Telegram | Telegram is banned/unavailable in India for the user. Email via Gmail is the fallback. |
| 11 | Paper trading is human-in-the-loop with a reasoning journal | User's call: the engine proposes, the user approves/rejects and logs a one-line "why". Review scores both the engine's signals and the user's instincts, so the system (and user) improve from real feedback. |
| 12 | Paper portfolio starts at Rs. 1,00,000 | Realistic personal-portfolio size; big enough for several positions. Safety rails: whole shares only, never overspend cash, max 25% of portfolio in one stock. |
| 13 | Phase 4 replanned around a user-provided "Plan and scope" doc, not the original `aditrader-phase4-master-handoff-prd.md` 4A/4B/4C breakdown | That PRD only covered structured journaling, news sentiment, and a win-rate auto-tuner — it didn't include forecasting, full trade plans (entry/stop/target/invalidation), or automatic plan-outcome tracking, all of which the scope doc requires. See `PLAN.md` for the new 4A-4F order. |
| 14 | Markets: NSE/BSE equities AND options, options deferred to Phase 5 | Options are real scope, not dropped, but yfinance's NSE options data isn't reliable enough to build on yet — needs its own data-source decision first. |
| 15 | Holding period: swing (multi-day) first, intraday explicitly later | Matches the existing daily 50/200-day SMA + RSI cadence; intraday needs real-time data and a faster loop, which is a separate rebuild. |
| 16 | News sourced via free RSS/Google News, LLM-summarized to a tiny sentiment JSON | No subscription cost; keeps core trading scripts isolated from raw article text (only ever reads `data/news_sentiment.json`). |
| 17 | No dedicated in-tool dialogue/chat feature for v1 | The user already discusses theses with Claude directly in sessions with full journal context; Phase 4A's structured levers capture the outcome of that conversation instead of rebuilding chat inside the app. |
| 18 | Hosting: GCP Compute Engine (`e2-micro`, `us-central1-a`), not Oracle Cloud | GCP's Always Free tier covers this exact size/region combo ($0/month even after the $300 trial credit expires). An earlier draft doc (`ARCHITECTURE.md`, since rewritten) had specified Oracle Cloud; GCP is what was actually built and is live. |
| 19 | Local, file-based JSON/JSONL state for the engine — no Supabase/Postgres/cloud DB | The engine's own state (`data/portfolio.json`, `journal.jsonl`, `news_sentiment.json`, `brain_weights.json`) is small, personal, single-user, and needs to be inspectable/editable by hand. A cloud DB adds an external dependency, auth surface, and cost for no real benefit at this scale. (Note: the *frontend*, built separately in Lovable, originally shipped with Supabase for its own UI-side event log — that was fully stripped 2026-07-06 per decision #21 below; it never became the engine's source of truth.) |
| 20 | Decoupled branch strategy: Python engine on `main`, Lovable React UI on `lovable-ui` | Keeps the backend framework-free and independently runnable (cron jobs, Discord bot need no Node/build step) and stops frontend churn from destabilizing the trading engine. `lovable-frontend/` is gitignored entirely on `main`. |
| 21 | Frontend: stripped Supabase/Lovable-Cloud auth+DB and the Lovable AI Gateway; UI now talks only to the local FastAPI backend | The UI was built via Lovable and shipped wired to Supabase (auth + `trades`/`skips` tables) and to `ai.gateway.lovable.dev` (LLM calls) by default. Both were cloud dependencies this project doesn't want: single local user needs no cloud auth, and the LLM should be called directly with the user's own key, not proxied. `emit()` now `fetch()`es the local API; `/api/chat` calls Gemini directly via `google-genai`. |
| 22 | Data source: migrated from yfinance to the DhanHQ Data API | yfinance has no reliable NSE options data and is a scraped/unofficial feed with no SLA. Once the user subscribed to Dhan's Data API (a paid add-on, separate from a trading account), `src/dhan_client.py` became the sole market-data source — verified security IDs pulled from Dhan's own scrip master (not hand-typed), real daily OHLC (so `plan_tracker` still resolves stop/target on true daily high/low, not a naive last-price check), and Dhan's option-chain API unblocks the previously-deferred options work (decision #14). Dhan tokens are short-lived (~24h) and must be refreshed regularly — see `HANDOVER.md`. |
| 23 | `src/api.py` is a single unified FastAPI app, not two separate backends | An earlier iteration had a standalone dashboard API (`src/web/api.py`) built before the analyst/decision/scorecard endpoints existed. Once both existed, running two servers was pure tech debt (two ports, two CORS configs, duplicate watchlist logic) — merged into one `src/api.py`; `src/web/api.py` deleted. |

## Still open
- Options trading build-out (Phase 5) — Dhan's option-chain data is now
  available (decision #22 unblocks this), but the strategy/plan logic for
  options hasn't been designed yet.
- Redeploying the GCP VM with the post-Dhan code (`config.json`, `dhan_client.py`,
  Discord bot) — the VM still runs the old pre-Dhan, pre-config.py code. See
  `HANDOVER.md` deploy notes.
- Phase 6 (SQLite "Brain Map" for macro/event pattern memory) — schema
  designed (see chat history / future `PLAN.md` entry), not built.
- Phase 7 (historical backtest simulator) — not started.
- Intraday holding period — needs real-time streaming data and a faster
  loop; explicitly deferred, not investigated.

## Resolved gotchas worth remembering
- Gemini API keys created via AI Studio's "create in new project" button get zero free-tier quota (HTTP 429, limit:0). Create keys against the existing billed `alpha-trading-app-2026` GCP project instead.
- Pin Gemini model names loosely: `gemini-2.0-flash` was deprecated (HTTP 404) within months. `src/news_processor.py` (and now `src/api.py`'s chat endpoint) use the `gemini-flash-lite-latest` alias, which auto-tracks Google's current model.
- Dhan's own docs example maps ONGC to security id `2885` — that ID actually belongs to RELIANCE. Always verify security IDs against Dhan's official scrip master (`api-scrip-master-detailed.csv`), never hand-type them; a wrong ID silently prices the wrong stock.
- Dhan's market-data endpoints are separately rate-limited (roughly 1 request/sec) from account/order endpoints; a valid, authenticating token can still return an empty/failure response under rapid repeated calls. `src/dhan_client.py` retries once after a short pause before giving up.
- Dhan's `historical_daily_data` rejects a same-day (or future) date range with `DH-902`/`DH-905` rather than returning an empty result — `dhan_client.get_ohlc_since` short-circuits to `[]` for a start date of today or later so `plan_tracker` just waits for the next session instead of logging noisy errors.

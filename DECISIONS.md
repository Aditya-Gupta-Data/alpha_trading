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
| 24 | Backend redeployed to a fresh GCP VM running the FastAPI server as a systemd service | The original cron VM (project `alpha-trading-app-2026`) had a lost login. A new VM (`alpha-trading-vm`, project `project-37632031-10d0-47dd-b6f`, Debian 13) was built 2026-07-06: code via `git clone`, deps in a venv, and `src.api:app` runs continuously via a systemd service (`Restart=always`, enabled on boot) instead of the old per-schedule cron jobs. See `HANDOVER.md` → "GCP VM (cloud hosting)". |
| 25 | Phase 6 "Brain Map" is a relational SQLite store, additive to the existing learning loop — **built and fully wired 2026-07-06** | The current `tuner.py`/`brain_weights.json` loop is a flat numerical nudge on two BUY archetypes; it can't answer "has *this cluster of events* happened before and did it pay?". The Brain Map adds that as a separate `data/brain_map.db` (native `sqlite3`, per decision #19's no-cloud-DB rule and VISION_PLAN's no-Postgres/Mongo rule). Kept strictly additive so it can't destabilize the working forecast/tuner path — see the dedicated design section below. |
| 26 | Brain Map memory enters `forecast.py` as advisory context only, never as score points | When the current setup carries active pattern tags, forecast() attaches the map's historical stats as `memory`/`memory_context` payload fields (a "Historical Performance for active patterns [...]" line every consumer — terminal, Discord /analyze, API, LLM prompts embedding forecasts — sees). It deliberately adds **zero** points to the checklist score: score adjustment from outcomes is already the tuner's job (decision #25's additive rule), and double-counting the same resolved trades in both mechanisms would compound. Fail-safe: empty/missing DB, no tags, or any query error degrade to `memory: null` with the standard flow untouched. |

## Still open
- Options trading build-out (Phase 5) — Dhan's option-chain data is now
  available (decision #22 unblocks this), but the strategy/plan logic for
  options hasn't been designed yet.
- Cloud-scheduled email jobs on the new VM — the old cron VM ran `src.main`
  (alerts) and `src.suggest` (suggestions) on a schedule; the new VM
  (decision #24) runs only the API server, so those are not restored yet.
  See `HANDOVER.md` → "GCP VM (cloud hosting)".
- Exposing the VM API to the internet (for a deployed frontend) — currently
  local to the VM only; recommended path is a Cloudflare Tunnel + an API-key
  check in `src.api`. Not built.
- ~~Phase 6 (SQLite "Brain Map")~~ — **DONE 2026-07-06** (all steps: store +
  tests, ingestion + journal short_ids, forecast wiring — decisions #25/#26).
  Ongoing operational note: re-run `python3 -m src.brain_map ingest` after
  trades resolve so the map keeps accumulating outcomes.
- Phase 7 (historical backtest simulator) — not started. Note its
  VISION_PLAN prompt still says "fetch from yfinance"; that must be updated
  to use `src/dhan_client.py` (decision #22) when built.
- Intraday holding period — needs real-time streaming data and a faster
  loop; explicitly deferred, not investigated.

## Phase 6 — Brain Map design (banked 2026-07-06, not yet built)

Finalized design for the SQLite event-pattern memory (`data/brain_map.db`),
built via a new standalone `src/brain_map.py` using Python's native
`sqlite3`. Upgrades the flat `brain_weights.json` nudge into a relational
store that can answer *"has this cluster of events happened before, and did
it make money?"*

**Three tables** (kept small per the no-bloat rule):
- **`events`** — one row per observation: `id, date, ticker, event_type,
  tag, sentiment, entities (JSON), source`. `tag` is the normalized pattern
  key that clustering/queries match on.
- **`outcomes`** — one row per resolved trade: `id, journal_ref, date,
  ticker, archetype, r_multiple, result` (win/loss/scratch).
- **`event_outcome_link`** — many-to-many glue (`event_id ↔ outcome_id`)
  recording which events were "in the air" (same ticker, around the trade
  date) when a trade resolved. This link table is what makes cluster
  queries possible.

**Public API in `src/brain_map.py`:**
- `record_event(...)`, `record_outcome(...)`, `link_event_outcome(...)`
- `query_similar_events(tags) -> {count, win_rate, avg_r_multiple, examples}`
  — the core question-answerer.
- `ingest_existing()` — seeds `events` from data we already produce
  (`pattern_tags`, strategy signals, `data/news_sentiment.json`) and
  backfills `outcomes` from resolved `journal.jsonl` entries, keyed by a
  deterministic composite `journal_ref` (`date|ticker|action|price`) since
  journal rows have no stable id yet.

**Strict rules (non-negotiable for the build):**
- **Additive only.** `tuner.py` and `brain_weights.json` stay untouched and
  keep running; the Brain Map is a parallel store, not a replacement.
- **Paper-only / read-history.** It only records and reads past events and
  outcomes; it never touches execution or `data/portfolio.json`.
- Native `sqlite3` only — no Postgres/Mongo (VISION_PLAN guardrail,
  decision #19).

**Build status: ALL steps landed 2026-07-06.** Store + tests, then
`ingest_existing()` + journal `short_id`s, then the forecast wiring
(advisory-context-only — decision #26). `tuner.py`/`brain_weights.json`
were never touched, per the additive rule above.

## Resolved gotchas worth remembering
- Gemini API keys created via AI Studio's "create in new project" button get zero free-tier quota (HTTP 429, limit:0). Create keys against the existing billed `alpha-trading-app-2026` GCP project instead.
- Pin Gemini model names loosely: `gemini-2.0-flash` was deprecated (HTTP 404) within months. `src/news_processor.py` (and now `src/api.py`'s chat endpoint) use the `gemini-flash-lite-latest` alias, which auto-tracks Google's current model.
- Dhan's own docs example maps ONGC to security id `2885` — that ID actually belongs to RELIANCE. Always verify security IDs against Dhan's official scrip master (`api-scrip-master-detailed.csv`), never hand-type them; a wrong ID silently prices the wrong stock.
- Dhan's market-data endpoints are separately rate-limited (roughly 1 request/sec) from account/order endpoints; a valid, authenticating token can still return an empty/failure response under rapid repeated calls. `src/dhan_client.py` retries once after a short pause before giving up.
- Dhan's `historical_daily_data` rejects a same-day (or future) date range with `DH-902`/`DH-905` rather than returning an empty result — `dhan_client.get_ohlc_since` short-circuits to `[]` for a start date of today or later so `plan_tracker` just waits for the next session instead of logging noisy errors.

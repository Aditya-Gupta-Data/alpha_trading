# Decisions

Choices we've locked in, and why. Update this whenever we make a new call so we
never have to re-argue it in a future chat.

| # | Decision | Why |
|---|----------|-----|
| 1 | Tool is for personal use only | No multi-user infrastructure, no compliance overhead of serving others. |
| 2 | Market: Indian stocks (NSE/BSE) today; MCX Commodities (Gold, Crude Oil) + Global Indices are a *planned* expansion, not yet built | User trades from India; Indian brokers have the most API-friendly setups. The engine today prices/trades NSE/BSE equities and NSE options only. The DhanHQ migration (decision #22) natively covers commodities and global indices too, so the "Cross-Asset Integration" expansion can reuse the same data layer with no new provider — scoped as a pending phase (see `HANDOVER.md` → Pending Phases), executed only when the user greenlights it. |
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
| 27 | Phase 5 (Options Trading) locked to defined-risk multi-leg spreads only, with VIX regime filtering, max-loss sizing, 2026 cost friction, and forced early exits — **FULLY BUILT 2026-07-06** (construction, sizing, frictions, tracking, and the proposal wiring `src/options_proposer.py`) | User-issued architectural constraints for the options build. Zero naked legs: user does not want unlimited-risk exposure in a paper/learning system. India VIX regime gates strategy family (see design section below) so range-bound spreads aren't proposed into breakout risk. Sizing by absolute max loss (not stop-distance) because a spread's loss is capped by construction, not by a technical stop. Margin math must simulate NSE SPAN offsets for hedged spreads rather than blocking on raw (unhedged) capital, or every valid spread would appear unaffordable. 2026 STT (0.15% on options premium sales) and dynamic bid-ask slippage (0.10%-0.50% by liquidity) keep the paper P&L realistic instead of overstating edge. Zero expiry holding + forced 60-70% max-profit auto-exit eliminates gamma risk near expiry and locks in gains before late-cycle decay/whipsaw. |
| 28 | Options sizing gets its own risk budget: `options_risk_per_trade_pct` (config.json, default 10%) instead of the equity `RISK_PER_TRADE_PCT` (1%) | A spread's max loss is a hard structural ceiling (the wings guarantee it), unlike an equity stop that can gap through — so a bigger per-trade budget is defensible. Practically it's also required: a single NIFTY lot-75 condor carries ~Rs.6k of max loss, and at the equity 1% budget (Rs.1,000 on the Rs.1,00,000 paper book) every options proposal would size to 0 lots forever. 10% (Rs.10,000) allows exactly 1 lot of a typical structure — sized to learn, not to bet the book. Optional config key with an in-code default so older config.json copies (e.g. the VM's) keep working. |
| 29 | Discord push wired via a channel **webhook** (`DISCORD_WEBHOOK_URL`), a separate mechanism from the existing interactive **bot** (`DISCORD_BOT_TOKEN`) | The bot (decision from Phase 5's Discord Analyst work) handles two-way `/analyze` chat and needs a running gateway connection; alerts and trade episodes are one-way pushes that don't need a bot online at all — a webhook is simpler, can't be rate-limited by bot presence, and needs no gateway. Verified live end-to-end 2026-07-06: real webhook created on the "Alpha Trading" Discord server, `DISCORD_WEBHOOK_URL` set in `.env` locally and on the VM (base64-paste method, `HANDOVER.md`), confirmed via `python3 -m src.plan_tracker --mock-trade-strategy IRON_BUTTERFLY` (`Discord delivery: OK`, journals nothing) and the VM's systemd service restarting clean. |
| 30 | An LLM (local or cloud) must NEVER be used for continuous 24/7 market monitoring — price/rule checks stay pure Python | Checking whether a price crossed a level or a moving average crossed another is deterministic math, already correctly implemented in `src/rules.py`/`src/dhan_client.py`/`src/api.py`'s poll loop. Routing that through any LLM (local Ollama or cloud Gemini) would be a large, pointless compute cost for zero accuracy gain and would risk hallucinated trigger decisions on a task that has one right answer. This rule scopes **Phase 10B** (`VISION_PLAN.md`/`HANDOVER.md`): a local LLM's only legitimate job is "light work" — parsing unstructured text (news, Discord chat, journal summaries) into structured JSON for the Brain Map, run off-market-hours, never live price decisions. Phase 10B was BUILT 2026-07-06 (`src/local_parser.py`, `src/sleep_phase.py`) and this rule is enforced by import-guard tests that fail if either module imports a market-data module (`tests/test_local_parser.py`, `tests/test_sleep_phase.py`). |
| 31 | Headless (market-loop) proposals journal as `pending_approval` and are tracked HYPOTHETICALLY, exactly like rejected entries | The market loop (`src/market_loop.py`) auto-generates proposals when nobody is at the terminal; they must not execute (decision #11) but also must not vanish. User's call (2026-07-06, over the alternatives of hiding pending entries from the tracker or auto-expiring them to rejected after 24h): the tracker resolves them hypothetically as-is — immediate learning data on what the setup would have done, zero tracker code changes. Accepted quirk: an undecided pending entry's verdict reads like a skip ("MISSED GAIN / GOOD SKIP") even though the user was never asked. The decision path is `python3 -m src.options_proposer --review-pending` (offline, from the stored spread payload): y -> `approved` on paper (tracker takes over; NO broker call — "execute" means paper, dhan_client stays data-only), n -> `rejected` (the codebase's canonical skip term, kept so scorecard/review flows see it). Entries the tracker already resolved are not decidable after the fact — no approving with hindsight. |
| 32 | All inbound internet traffic reaches the VM ONLY via a Cloudflare Tunnel to a strict fail-closed API-key gateway (`src/api_server.py`) — no firewall port is ever opened | Opening port 8000 on the GCP firewall would expose a raw HTTP server to the whole internet. Instead `cloudflared` dials OUT from the VM (free HTTPS, nothing listening publicly, survives IP changes) and forwards to `localhost:8000`, where `src/api_server.py` runs: it mounts the full `src.api` app (one port, one process, dashboard + bridge) behind a STRICT gate — every request needs an `x-api-key` header matching `.env`'s `API_KEY` (401 otherwise, constant-time compare), and if `API_KEY` is unset the gateway refuses everything with 503 rather than serving open (`src.api`'s key-optional mode remains for localhost-only dev). The two-way Discord bridge `POST /api/discord/action` ({action: approve/reject, trade_id: journal short_id}) decides `pending_approval` entries with exactly the `--review-pending` CLI semantics via the shared `options_proposer.decide_pending()` (decision #31): approve -> paper "approved", reject -> "rejected", tracker-resolved entries return 409 (no hindsight approvals). Built + tested offline (`tests/test_api_server.py`) 2026-07-07. |
| 33 | Knowledge Graph reasoning (Phase 6C) is a READ-ONLY inference layer; it never alters trading rules, only informs the AI Analyst's rationale | The graph reasoning layer (`src/graph_engine.py`, a `networkx.DiGraph` built at runtime from the additive `graph_edges` table in `data/brain_map.db`) walks causal links to surface historically-linked patterns as advisory context on a proposal. It is strictly non-authoritative, by the same logic as the Brain Map memory in `forecast.py` (decision #26): it adds ZERO score points, changes no VIX gate / sizing / strategy-selection rule, and places no trades — it only enriches the rationale shown in the Discord PROPOSAL ALERT (`options_proposer.py`) so the human sees what the setup rhymes with before approving. Fail-safe and memory-resident: it loads once, never writes during inference, and an empty/missing `graph_edges` table degrades to an empty context block with no behavior change. Persistence stays SQLite; `networkx` is only the in-memory reasoning layer — no new database (the Phase 6C strict constraint). Edges are written by the Sleep Phase (`src/sleep_phase.py`); until that writer lands the block is simply empty. Built + tested offline (`tests/test_graph_engine.py`) 2026-07-07. |

## Still open
- ~~Options trading build-out (Phase 5)~~ — **DONE 2026-07-06, end to end**
  (decisions #27/#28 + design section below): friction stack + SPAN
  offsets in `src/portfolio.py`, `StrategyConstructor` + VIX gate +
  max-loss sizing in `src/strategy.py`, atomic-basket resolution with
  65%/pre-expiry auto-exits in `src/plan_tracker.py`, India VIX in
  `src/dhan_client.py`, and the proposal wiring `src/options_proposer.py`
  (`python3 -m src.options_proposer` — real Dhan chain -> regime-matched
  spread -> human approve/reject -> journal). Covered by
  `tests/test_options_spreads.py` + `tests/test_options_proposer.py`.
  Discord-surfaced 2026-07-06 (🚨 PROPOSAL ALERT before the y/n pause +
  ✅/❌ decision follow-up, both fail-safe); the decision itself stays
  human-in-the-loop in the terminal (decision #11's flow). Dashboard
  surfacing still open.
- Cloud-scheduled email jobs on the new VM — the old cron VM ran `src.main`
  (alerts) and `src.suggest` (suggestions) on a schedule; the new VM
  (decision #24) runs only the API server, so those are not restored yet.
  See `HANDOVER.md` → "GCP VM (cloud hosting)".
- ~~Exposing the VM API to the internet (for a deployed frontend)~~ — **Backend half DONE 2026-07-07**: `src/api_server.py` (strict fail-closed `x-api-key` gateway wrapping `src.api`) and the two-way Discord bridge `POST /api/discord/action` (approve/reject pending journal entries by `short_id`, `--review-pending` semantics). Still open: installing and configuring `cloudflared` on the VM.
- ~~Phase 6 (SQLite "Brain Map")~~ — **DONE 2026-07-06** (all steps: store +
  tests, ingestion + journal short_ids, forecast wiring — decisions #25/#26).
  Ongoing operational note: re-run `python3 -m src.brain_map ingest` after
  trades resolve so the map keeps accumulating outcomes.
- Phase 7 (historical backtest simulator) — not started. Note its
  VISION_PLAN prompt still says "fetch from yfinance"; that must be updated
  to use `src/dhan_client.py` (decision #22) when built.
- Intraday holding period — needs real-time streaming data and a faster
  loop; explicitly deferred, not investigated.
- Phase 10B (local LLM Episodic Event Extractor + Sleep Phase, via Ollama)
  — **FULLY BUILT 2026-07-06** (decision #30): `src/local_parser.py`
  (`LocalExtractor` + `process_unstructured_input()`; brain_map untouched)
  and `src/sleep_phase.py` (`python3 -m src.sleep_phase` — ingestion with
  hash-dedupe + provenance in `ingest_log`, LLM theme consolidation into
  `semantic_nodes`/`semantic_event_link` with reinforcement instead of
  duplication, and the exponential decay engine `score * e^(-λ·Δt)` that
  flags nodes inactive below 0.20, never deletes). The three sleep-phase
  tables are created/owned by `sleep_phase.py` in the same brain_map.db —
  the core schema stays untouched. Covered by `tests/test_local_parser.py`
  + `tests/test_sleep_phase.py`, both with decision-#30 import guards.
  Ollama + `llama3` confirmed installed on the host (2026-07-06), and
  the sleep phase is scheduled via `scripts/setup_cron.sh` entry #4
  (20:00 IST daily -> `logs/sleep_phase.log`). Placement caveat: it only
  does real work on the machine holding `data/` + Ollama (the Mac); on
  the VM it degrades to a harmless decay-only pass. Distinct from the
  Phase 10 "maker/checker" auditor idea — same tool, different job.

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

## Phase 5 — Options Trading design (banked and BUILT 2026-07-06)

Strict architectural directives for the options build, given by the user.
Non-negotiable for the implementation — record here so a future session
can't accidentally regress to naked legs or stop-distance sizing.

**1. Strategy (`src/strategy.py`) — zero naked options, defined-risk spreads only:**
- Trend regime (Bullish) -> propose Bull Call Spreads (debit).
- Trend regime (Bearish) -> propose Bear Put Spreads (debit).
- Neutral / mean-reversion regime -> propose Iron Condors / Iron Butterflies
  (credit, range-bound), subject to the VIX gate below.
- No single-leg (naked) calls/puts anywhere in the proposal path.

**2. Regime filtering via India VIX:**
- VIX < 15 -> "safe" regime, debit spreads (directional) clear normally.
- VIX > 16 -> **strict block** on range-bound/credit strategies (Iron
  Condor, Iron Butterfly) — elevated VIX means breakout risk is too high
  for a strategy that loses on a large move in either direction.
- (15-16 is a narrow gap between the two named thresholds; treat as the
  same caution zone as >16 for credit strategies until refined.)

**3. Sizing & margin (`src/portfolio.py`):**
- Position sizing = **absolute max loss**, not technical stop-loss
  distance — for a spread this is `(Spread Width - Net Credit) * Lot Size`
  (credit spreads) or `Net Debit * Lot Size` (debit spreads), since the
  max loss is fixed by the spread's construction.
- Margin math simulates NSE SPAN margin **offsets** for hedged spreads
  (the long leg reduces the margin the short leg would otherwise require)
  rather than blocking capital as if each leg were a raw/naked position.

**4. 2026 cost friction (paper portfolio):**
- Deduct April 2026 STT: 0.15% on options **premium sales** (sell-side
  legs only, per current STT rules on options).
- Apply dynamic bid-ask slippage drag to net P&L: 0.10%-0.50%, scaled by
  the instrument's liquidity (tighter/more liquid strikes near the money
  get the low end; far/illiquid strikes get the high end).

**5. Execution & tracking (`src/plan_tracker.py`):**
- Zero expiry holding — every options plan must exit before expiry to
  eliminate late-cycle gamma risk.
- Force an early auto-exit once realized profit hits 60-70% of the
  spread's maximum possible profit (defined-risk max, from point 3),
  rather than waiting for full theoretical max profit.

**Status: BUILT 2026-07-06** across `portfolio.py` (frictions + SPAN
offsets), `strategy.py` (`StrategyConstructor`, VIX gate, max-loss
sizing), `plan_tracker.py` (atomic basket exits, 65% profit take,
2-days-before-expiry rule, defined-risk P&L clamp), and
`dhan_client.py` (India VIX, id 21 verified against the scrip master).
Implementation notes vs the design above: the VIX gate blocks
range-bound strategies when VIX > 16 **or when VIX is unavailable**
(fail-safe); the 15-16 caution zone resolved to a strict `> 16` cutoff
(16.0 exactly still allows); the profit-take band landed as a single
65% constant (`OPTION_PROFIT_TAKE_FRACTION`). Proposal wiring (option
chains -> journal entries) is the remaining open piece — see "Still
open".

## Resolved gotchas worth remembering
- Gemini API keys created via AI Studio's "create in new project" button get zero free-tier quota (HTTP 429, limit:0). Create keys against the existing billed `alpha-trading-app-2026` GCP project instead.
- Pin Gemini model names loosely: `gemini-2.0-flash` was deprecated (HTTP 404) within months. `src/news_processor.py` (and now `src/api.py`'s chat endpoint) use the `gemini-flash-lite-latest` alias, which auto-tracks Google's current model.
- Dhan's own docs example maps ONGC to security id `2885` — that ID actually belongs to RELIANCE. Always verify security IDs against Dhan's official scrip master (`api-scrip-master-detailed.csv`), never hand-type them; a wrong ID silently prices the wrong stock.
- Dhan's market-data endpoints are separately rate-limited (roughly 1 request/sec) from account/order endpoints; a valid, authenticating token can still return an empty/failure response under rapid repeated calls. `src/dhan_client.py` retries once after a short pause before giving up.
- Dhan's `historical_daily_data` rejects a same-day (or future) date range with `DH-902`/`DH-905` rather than returning an empty result — `dhan_client.get_ohlc_since` short-circuits to `[]` for a start date of today or later so `plan_tracker` just waits for the next session instead of logging noisy errors.

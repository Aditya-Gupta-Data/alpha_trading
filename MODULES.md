# MODULES.md — Master Component Index

One-line-plus purpose for every file that matters. If a file isn't listed
here, it's either generated (`__pycache__/`), static assets, or trivial. For
system flow between these, see `ARCHITECTURE.md`.

## Data layer (market data)

| File | Purpose |
|---|---|
| `src/dhan_client.py` | THE market-data source. DhanHQ SDK wrapper: `SECURITY_ID_MAP` (verified against Dhan's scrip master), `get_daily_ohlc`, `get_ohlc_since`, `get_live_price`, `get_quote`, `get_daily_closes`, `get_option_chain`/`get_expiry_list`. Data-only — no order methods. |
| `src/data_fetcher.py` | Thin re-export of `dhan_client.get_quote` — kept for the original `get_quote(ticker)` contract older callers use. |
| `src/indicators.py` | Pure-Python SMA and Wilder's RSI, no dependencies. |

## Signal & suggestion engine

| File | Purpose |
|---|---|
| `src/rules.py` | Alert rule evaluation: `price_above`, `price_below`, `percent_up`, `percent_down`. |
| `src/suggestions.py` | Trend (50/200 SMA) + momentum (14-day RSI) read per stock; `analyze()`/`describe()`. |
| `src/suggest.py` | Entry point: emails a daily suggestions digest (`python -m src.suggest`). |
| `src/news_processor.py` | Isolated: Google News RSS -> Gemini -> `data/news_sentiment.json` (score -5..+5 + driver). Imports NO core trading code (isolation principle). |
| `src/forecast.py` | Combines technicals + news sentiment into a transparent, rule-based checklist: bias/confidence/drivers/time-horizon. Reads `data/brain_weights.json` for learned per-archetype adjustments. |

## Trading & risk

| File | Purpose |
|---|---|
| `src/strategy.py` | Turns signals into full trade PLANS: entry rule, stop-loss, target, risk:reward, rationale, risk-based position sizing. `propose_plans()` / `propose()`. |
| `src/portfolio.py` | Paper portfolio math: `data/portfolio.json`, buy/sell, cash + 25%-per-stock rails. |
| `src/journal.py` | Appends every approved/rejected decision to `data/journal.jsonl` with signal, risk levers, pattern tags, plan block, outcome. |
| `src/plan_tracker.py` | Resolves OPEN plan-carrying trades against real daily OHLC high/low (stop/target/time-stop) — NOT a naive last-price check. Closes paper positions on resolution (bracket-order semantics). |
| `src/review.py` | Legacy 7-day price-drift scorecard for pre-plan (non-4B) journal entries. |
| `src/tuner.py` | Learning loop: scores resolved BUY archetypes (fresh-cross vs RSI-oversold), writes `data/brain_weights.json`, which `forecast.py` consumes. |
| `src/trade.py` | Interactive terminal paper-trading session (`python -m src.trade`) — the original human-in-the-loop flow. |

## Alerting & notifications

| File | Purpose |
|---|---|
| `src/main.py` | Alert entry point (`python -m src.main`) — watchlist -> rule check -> email digest. |
| `src/notifier.py` | Gmail SMTP sender (`send_digest`), self-contained `.env` reader. |
| `src/config.py` | Loads + validates `config.json` at import time (fails loudly on missing/bad keys) — RSI thresholds, SMA windows, risk levers, tuner params. |

## Interfaces (front doors)

| File | Purpose |
|---|---|
| `src/api.py` | Unified local FastAPI backend — the ONLY thing the React dashboard talks to. All `/api/*` routes (see `ARCHITECTURE.md`). Runs the hourly auto-sync background loop. |
| `src/discord_bot.py` | Discord analyst bot: `/analyze` slash command (forecast), chat replies via Gemini. Read-only on the engine (imports only `forecast.py`). |
| `src/web/watchlist_store.py` | Reads/writes `config/watchlist.yaml` (ruamel round-trip, preserves comments); ticker validation goes through `dhan_client`. |
| `src/web/static/index.html` | Legacy static dashboard HTML, served by `src/api.py`'s `GET /`. Superseded by `lovable-frontend/` for active development. |
| `lovable-frontend/` | React (TanStack Start + Vite) dashboard. **Gitignored on `main`** — lives only on the `lovable-ui` branch. See `DATA_CONTRACT.md`. |

## Config & data files (not code)

| File | Purpose |
|---|---|
| `config.json` | Non-secret tunables: RSI/SMA thresholds, risk levers, tuner params. Required keys enforced by `src/config.py`. |
| `config/watchlist.yaml` | The watchlist: tickers, type (stock/index), optional alert rules. |
| `.env` / `.env.example` | Secrets (git-ignored) / safe placeholder template (versioned). Keys: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `GEMINI_API_KEY`, `DISCORD_BOT_TOKEN`, `ALERT_EMAIL_*`. |
| `data/portfolio.json` | Paper portfolio (cash + holdings). Git-ignored, personal, live. |
| `data/journal.jsonl` | Every trade decision ever logged (one JSON object per line). Git-ignored, personal, live. |
| `data/news_sentiment.json` | Latest Gemini-scored news sentiment per ticker. Git-ignored (regenerable). |
| `data/brain_weights.json` | Tuner's learned per-archetype weights. Git-ignored (regenerable). |

## Tests

| File | Purpose |
|---|---|
| `tests/test_rules.py` | Alert rule logic, offline. |
| `tests/test_portfolio.py` | Portfolio math + strategy proposals, offline. |
| `tests/test_forecast.py` | Forecast checklist scoring, offline (monkeypatches `suggestions.analyze`). |
| `tests/test_tuner.py` | Tuner weight learning, offline (fake journal entries). |

## Documentation suite (this file's siblings)

| File | Purpose |
|---|---|
| `OVERVIEW.md` | Vision + non-negotiables. Read first. |
| `ARCHITECTURE.md` | System flow diagrams + hosting/state model. |
| `MODULES.md` | This file. |
| `DECISIONS.md` | Log of locked architectural decisions + why. |
| `HANDOVER.md` | Cold-start: credentials, env vars, boot commands, current state. |
| `DATA_CONTRACT.md` | Exact JSON schemas + API contract for the frontend. |
| `PLAN.md` | Phase 4 (4A-4F) build plan and status — historical build log. |
| `VISION_PLAN.md` | User's Phase 5+ master blueprint (Discord, Brain Map, simulator, event ingestion). |
| `DECISIONS.md`, `PLAN.md`, `VISION_PLAN.md` are historical/planning docs and are NOT rewritten at every milestone the way `OVERVIEW`/`ARCHITECTURE`/`MODULES`/`HANDOVER` are — they're appended to as new phases/decisions land. |

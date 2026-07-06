# ARCHITECTURE.md — System Flow

Text-based system diagram + component map. For "why" behind each choice, see
`DECISIONS.md`. For a per-file index, see `MODULES.md`. This file describes
architecture as of the 2026-07-06 milestone (post Dhan migration).

> Superseded content warning: an earlier version of this file (pre-2026-07)
> described yfinance, Zerodha Kite Connect, Oracle Cloud, and a PWA — none of
> that is current. This is the authoritative rewrite.

## 1. High-level flow

```
                         ┌───────────────────────────────┐
                         │         DhanHQ Data API        │
                         │  (market quotes + daily OHLC +  │
                         │   option chain — READ ONLY)     │
                         └───────────────┬─────────────────┘
                                         │
                                         ▼
                         ┌───────────────────────────────┐
                         │   src/dhan_client.py            │
                         │   (single data-fetch layer)     │
                         └───────────────┬─────────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
   src/data_fetcher.py         src/suggestions.py           src/plan_tracker.py
   (quotes for alerts/         (SMA/RSI trend read           (resolves OPEN paper
    watchlist)                  for suggestions/forecast)     trades vs daily OHLC)
              │                          │                          │
              ▼                          ▼                          │
      src/main.py (alerts)      src/forecast.py                     │
      src/rules.py               (technicals + news                 │
                                  -> bias/confidence)                │
              │                          │                          │
              │                  src/news_processor.py               │
              │                  (Google News RSS -> Gemini           │
              │                   -> data/news_sentiment.json)        │
              │                          │                          │
              └──────────────┬───────────┴──────────────────────────┘
                             ▼
                    src/strategy.py (trade PLANS: entry/stop/
                      target/rationale, risk-based sizing)
                             │
                             ▼
              ┌──────────────┴───────────────┐
              ▼                              ▼
     src/trade.py (terminal,        src/api.py (POST /api/chat,
      interactive y/n session)       POST /api/decision — same
              │                      propose_plans() core)
              │                              │
              └──────────────┬───────────────┘
                             ▼
              src/portfolio.py + src/journal.py
              (data/portfolio.json, data/journal.jsonl —
               paper-only state, git-ignored)
                             │
                             ▼
                    src/tuner.py (learns per-archetype
                     weights from resolved outcomes
                     -> data/brain_weights.json, fed
                     back into forecast.py)
```

## 2. Two front doors into the engine

```
┌─────────────────────┐        ┌──────────────────────────┐
│  Discord             │        │  lovable-frontend/         │
│  (src/discord_bot.py) │        │  React + TanStack Start     │
│  /analyze slash cmd,  │        │  (gitignored on `main`,     │
│  chat replies via     │        │  lives on `lovable-ui`      │
│  Gemini               │        │  branch only)               │
└──────────┬───────────┘        └────────────┬─────────────────┘
           │  reads forecast.py directly       │  HTTP (localhost)
           ▼                                   ▼
                        ┌──────────────────────────────┐
                        │   src/api.py (FastAPI, unified)│
                        │   GET/POST /api/*               │
                        └──────────────────────────────┘
```
- **Discord bot**: read-only on the engine (imports only `src.forecast`), no
  portfolio/trade/strategy access, cannot execute anything. Chat goes to
  Gemini directly (no cloud AI gateway).
- **React dashboard**: talks ONLY to `src/api.py` over HTTP — never reads
  `data/*.json` files directly. See `DATA_CONTRACT.md` for exact schemas.
- **src/api.py** is the single unified backend (the old separate
  `src/web/api.py` dashboard app was merged in and deleted 2026-07-06). It
  imports engine modules directly; it does not duplicate their logic.
  Endpoints: `/api/watchlist`, `/api/alerts`, `/api/chat`, `/api/decision`,
  `/api/scorecard`, `/api/review`, `/api/sync-market`, `/api/health`. It also
  runs an hourly background `asyncio` loop (FastAPI `lifespan`) that
  resolves OPEN paper trades and refreshes the watchlist price cache.

## 3. Hosting (current + roadmap)

- **Cloud VM** (rebuilt 2026-07-06): GCP Compute Engine, `alpha-trading-vm`,
  project `project-37632031-10d0-47dd-b6f`, `us-central1-a`, `e2-micro`,
  Debian 13, Python 3.13. Runs the current DhanHQ-backed FastAPI server
  (`src.api:app`, port 8000) continuously as a systemd service
  (`alpha-trading`, `Restart=always`, enabled on boot), including the hourly
  auto-sync loop. Deployed by `git clone` of `main` into `~/alpha_trading`
  with a venv; updates via `git pull` + `systemctl restart`.
  ⚠️ **Known gaps**: (1) the old VM's scheduled cron emails (`src.main`
  alerts, `src.suggest` suggestions) are not yet set up on this VM — only the
  API runs; (2) the API is not exposed to the internet yet (local to the VM,
  no firewall rule). See `HANDOVER.md` → "GCP VM (cloud hosting)" for
  operations, the `.env` token-transfer gotcha, and next steps.
- **Local (Mac)**: paper trading (`src/trade.py`), the FastAPI server
  (`src/api.py`), the Discord bot (`src/discord_bot.py`), and the React
  dashboard dev server all run locally today. Interactive/stateful pieces
  (anything touching `data/portfolio.json`) deliberately stay local, not on
  the VM — the VM is for unattended, read-only-on-portfolio jobs only.
- **Roadmap**: move the Discord bot and/or FastAPI server to the VM (or a
  similar always-on host) once the Dhan-based deploy is current, so the
  Discord analyst and dashboard work without the Mac being on. Not started.

## 4. State & storage

- **All engine state is local, file-based JSON/JSONL under `data/`**
  (git-ignored — see `OVERVIEW.md` / `DECISIONS.md` for why, no cloud DB):
  `portfolio.json`, `journal.jsonl`, `news_sentiment.json`,
  `brain_weights.json`. Config (non-secret, versioned) lives in
  `config.json` (root) and `config/watchlist.yaml`.
- **Secrets**: `.env` (git-ignored, `.env.example` is the versioned
  template) — `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `GEMINI_API_KEY`,
  `DISCORD_BOT_TOKEN`, `ALERT_EMAIL_*`. See `HANDOVER.md`.
- **Frontend**: `lovable-frontend/` is gitignored on `main` entirely — it is
  version-controlled only on the separate `lovable-ui` branch. Never commit
  it to `main`.

## 5. Non-negotiables enforced by this architecture

See `OVERVIEW.md` for the full list. Structurally enforced here:
- No broker/order-placement import exists anywhere in `src/`.
- `src/dhan_client.py` only calls Dhan's data endpoints (quote/historical/
  option-chain) — never order/fund/trade endpoints.
- `/api/decision` refuses `APPROVE_REAL` at the API layer (403), regardless
  of what the frontend sends.

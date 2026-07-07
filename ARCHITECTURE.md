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
           │  reads forecast.py directly       │  HTTP Public / Tunnel
           ▼                                   ▼
                         ┌──────────────────────────────┐
                         │   Cloudflare Tunnel (Public) │
                         └──────────────┬───────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────┐
                         │ src/api_server.py (Gateway)  │
                         │ - Strict fail-closed API key │
                         │ - Discord action bridge      │
                         └──────────────┬───────────────┘
                                        │ localhost (port 8000)
                                        ▼
                         ┌──────────────────────────────┐
                         │ src/api.py (FastAPI unified) │
                         │ GET/POST /api/*              │
                         └──────────────────────────────┘
```
- **Discord bot**: read-only on the engine internals (imports only
  `src.forecast`) — it never touches portfolio/trade/strategy modules and
  places no real orders (paper-only holds). Chat replies go to Gemini
  directly. For the **two-way bridge**, approve/reject actions are sent to the
  gated `POST /api/discord/action` endpoint; that endpoint (not the bot) then
  updates a `pending_approval` journal entry to approved-on-paper or rejected.
  So the only state the bot can change is a paper journal decision, and only
  through the authenticated gateway.
- **React dashboard**: talks to `src/api_server.py` via public HTTPS forwarded
  through the Cloudflare Tunnel, attaching the mandatory `X-API-Key` or
  `Authorization: Bearer` token.
- **src/api_server.py**: The strict fail-closed API-key gateway (Phase 9 backend)
  that mounts `src/api.py` internally on `localhost:8000`. It ensures no
  unauthenticated traffic can access any endpoints, and includes a direct bridge
  `POST /api/discord/action` to decide `pending_approval` entries from Discord webhooks.
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
  Debian 13, Python 3.13. Runs the FastAPI server as a systemd service
  (`alpha-trading`, `Restart=always`, enabled on boot). Deployed by `git clone`
  of `main` into `~/alpha_trading` with a venv; updates via `git pull` +
  `systemctl restart`. Scheduled cron jobs (`src.renew_token` at 07:00 IST,
  `src.main` at 15:35 IST, `src.suggest` at 08:00 IST, and `src.sleep_phase` at
  20:00 IST) are fully deployed via `scripts/setup_cron.sh`.
- **API Server & Cloudflare Tunnel**: All inbound internet traffic reaches the VM
  only via `cloudflared` dialing out to form a Cloudflare Tunnel, which forwards
  public HTTPS traffic to the internal port 8000. On `localhost:8000`, the strict
  gateway `src/api_server.py` listens, requiring an API key. No firewall port is
  ever opened on the GCP VM.
- **Local (Mac)**: paper trading (`src/trade.py`), the FastAPI server
  (`src/api.py`), the Discord bot (`src/discord_bot.py`), the React
  dashboard dev server, and local Ollama + `llama3` for news extraction/the sleep
  phase run locally today. Interactive/stateful pieces (anything touching
  `data/portfolio.json`) stay local on the Mac—the VM only hosts read-only-on-portfolio
  tasks.
- **Roadmap**: Continue migrating frontend integrations to the public Cloudflare
  Tunnel URL, so the dashboard and Discord webhook actions work fully end-to-end
  without local Mac hosting.

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

# HANDOVER.md — Cold-Start Brief

Read this to pick up the project cold in a new agent session. For vision see
`OVERVIEW.md`, for system flow see `ARCHITECTURE.md`, for the file index see
`MODULES.md`, for why past calls were made see `DECISIONS.md`. **This file is
updated only at milestone states, not on every commit** — check `git log`
for anything more recent than what's written here.

## Current production state (as of 2026-07-06)

- **Phases 1-4 (alerting, suggestions, paper trading, journal/plans/tracking/
  news/forecast/tuner) are feature-complete.**
- **Phase 5 (frontend + local API) is live**: unified FastAPI backend
  (`src/api.py`), a React dashboard (`lovable-frontend/`, Supabase-free),
  direct Gemini integration (no cloud AI gateway), an hourly auto-sync loop,
  and a Discord analyst bot (`src/discord_bot.py`).
- **Market data has been fully migrated from yfinance to the DhanHQ Data
  API** (`src/dhan_client.py`). This is the single source of prices/OHLC for
  the whole engine now.
- **Known gap**: the GCP VM's cron jobs still run the OLD pre-Dhan code — see
  "GCP VM redeploy" below.

## Credentials & environment variables

All secrets live in `.env` (repo root, git-ignored — `.env.example` is the
safe versioned template). Load pattern used everywhere: a self-contained
reader in each entry point (`_load_env()`), not a shared library, by design
(modularity — see `DECISIONS.md`).

| Variable | Purpose | Notes |
|---|---|---|
| `DHAN_CLIENT_ID` | DhanHQ account id | `1109738713` as of this writing |
| `DHAN_ACCESS_TOKEN` | DhanHQ Data API token | **Short-lived (~24h)** — regenerate from the Dhan web/app dashboard when calls start failing with an auth error. This is a recurring manual step, not automated yet. |
| `GEMINI_API_KEY` | Google Gemini (news sentiment + chat) | Get from Google AI Studio, create the key against the *existing billed* `alpha-trading-app-2026` GCP project (a key from AI Studio's "new project" flow gets zero free-tier quota — see `DECISIONS.md`). |
| `DISCORD_BOT_TOKEN` | Discord bot login | From the Discord Developer Portal, needs "Message Content Intent" enabled. |
| `ALERT_EMAIL_FROM` / `ALERT_EMAIL_APP_PASSWORD` / `ALERT_EMAIL_TO` | Gmail SMTP for alert/suggestion/session digests | App Password (16-char), not the normal Gmail password. |

`lovable-frontend/.env` (separate, its own git-ignore inside that folder)
needs only `VITE_API_BASE_URL="http://localhost:8000"` — no Supabase keys
(stripped 2026-07-06).

## Boot commands

```bash
# 1. Python engine dependencies (from repo root)
python3 -m pip install -r requirements.txt

# 2. The unified local API (serves the dashboard + all /api/* routes)
uvicorn src.api:app --reload --port 8000

# 3. The React dashboard (separate terminal)
cd lovable-frontend && npm install && npm run dev   # localhost:8080 (falls back :8081)

# 4. The Discord analyst bot (separate terminal, optional)
python3 -m src.discord_bot

# 5. Interactive paper-trading session (terminal, when you want to trade)
python3 -m src.trade

# 6. Offline test suite (no internet/API calls needed)
for f in tests/test_*.py; do python3 "$f"; done   # expect 30/30 passing
```

Manual/on-demand engine scripts (not on a schedule locally — only via VM cron
or run by hand): `python3 -m src.main` (alerts), `python3 -m src.suggest`
(suggestions), `python3 -m src.news_processor` (refresh news sentiment),
`python3 -m src.forecast` (print forecasts), `python3 -m src.tuner` (refresh
learned weights), `python3 -m src.plan_tracker` (manual resolve sweep — also
runs automatically at the start of every `src.trade` session and every hour
inside `src.api`), `python3 -m src.review` (7-day scorecard for pre-plan
entries).

## GCP VM (cloud hosting)

- `alpha-trading-vm`, zone `us-central1-a`, machine type `e2-micro` (GCP
  Always Free tier — $0/month), Debian 12, timezone `Asia/Kolkata`.
- SSH: `gcloud compute ssh alpha-trading-vm --zone=us-central1-a` (run from
  this project folder with gcloud CLI + billing project already configured).
- Cron (`crontab -l` on the VM): `35 15 * * 1-5` -> `python3 -m src.main`
  (alerts), `0 8 * * 1-5` -> `python3 -m src.suggest` (suggestions). Logs to
  `~/alpha_trading/logs/` on the VM (separate from local `logs/`).
- **⚠️ REDEPLOY NEEDED**: the VM still has the pre-Dhan, pre-`config.json`
  code. It will crash or silently keep using yfinance-era behavior until
  redeployed. The full, current deploy command (copy everything the engine
  needs, nothing it doesn't):
  ```bash
  gcloud compute scp --recurse --zone=us-central1-a \
    src config config.json requirements.txt .env \
    alpha-trading-vm:~/alpha_trading/
  # then on the VM:
  pip install -r requirements.txt
  ```
  `config.json` and `.env` are NOT optional — `src/config.py` fails loudly
  at import if `config.json` is missing, and `src/dhan_client.py` needs
  `.env`'s Dhan keys. `data/`, `tests/`, `logs/` are deliberately NOT copied
  (paper-trading state stays local only; see `OVERVIEW.md`).

## Watchlist (current)

10 tickers in `config/watchlist.yaml`, each with `percent_up`/`percent_down`
alert rules at 3%: `HDFCBANK.NS`, `ICICIBANK.NS`, `TCS.NS`, `INFY.NS`,
`RELIANCE.NS`, `ONGC.NS`, `HINDUNILVR.NS`, `ITC.NS`, `MARUTI.NS`, `TMPV.NS`.
All 10 are present in `src/dhan_client.py`'s `SECURITY_ID_MAP` — a ticker not
in that map cannot be priced by the current data layer.

## Live paper-trading data (IMPORTANT — do not reset)

`data/journal.jsonl` and `data/portfolio.json` are git-ignored and hold real
(paper) user activity: an original ONGC.NS buy (2026-07-03) plus several
2026-07-06 dashboard test trades (TCS/MARUTI/ONGC) made by clicking the
frontend's seeded demo proposal cards — kept intentionally, per the user.
Note those demo trades used bare tickers (`TCS`, not `TCS.NS`); resolving
them correctly depends on `dhan_client`'s alias resolution.
**Never reset these files.** When testing anything that writes to them, back
up first and restore after (or point at an isolated temp dataset) — this is
the working pattern used throughout this project's history.

## Where to look for more detail

- **Deep phase-by-phase build history** (what was built, when, and how it
  was verified) lived in this file through 2026-07-06 and has moved to git
  history / commit messages — `git log --oneline` and the commit bodies are
  the detailed record now. This file stays a lean cold-start brief going
  forward, per the user's instruction not to bloat it on every change.
- **Phase 4's step-by-step plan** (4A-4F): `PLAN.md`.
- **The Phase 5+ vision** (Discord, Brain Map, simulator, event ingestion):
  `VISION_PLAN.md`.
- **Frontend JSON contracts**: `DATA_CONTRACT.md`.

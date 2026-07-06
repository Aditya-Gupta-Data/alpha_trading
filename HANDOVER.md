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
- **The backend is deployed to a fresh GCP VM (2026-07-06)** running the
  DhanHQ-backed FastAPI server continuously as a systemd service — see
  "GCP VM (cloud hosting)" below. The old cron VM is superseded.
- **Known gap**: the scheduled email jobs (`src.main` alerts, `src.suggest`
  suggestions) that the *old* VM ran via cron are NOT yet set up on the new
  VM — only the API server runs there. See the VM section for how to add them.

## Credentials & environment variables

All secrets live in `.env` (repo root, git-ignored — `.env.example` is the
safe versioned template). Load pattern used everywhere: a self-contained
reader in each entry point (`_load_env()`), not a shared library, by design
(modularity — see `DECISIONS.md`).

| Variable | Purpose | Notes |
|---|---|---|
| `DHAN_CLIENT_ID` | DhanHQ account id | `1109738713` as of this writing |
| `DHAN_ACCESS_TOKEN` | DhanHQ Data API token | **Short-lived (~24h)**. `python3 -m src.renew_token` renews it in place via Dhan's `/v2/RenewToken` (rewrites the .env line, keeps `.env.bak`) — but it can only renew a still-valid token, so it must run at least daily (cron it). If it prints CRITICAL (token already expired, e.g. DH-906), do one manual refresh from the Dhan dashboard and the automation takes over again. |
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

**Rebuilt from scratch 2026-07-06.** The original cron VM (project
`alpha-trading-app-2026`) had a lost login and is abandoned; a new VM was
created and now runs the current DhanHQ FastAPI backend.

- **VM**: `alpha-trading-vm`, project `project-37632031-10d0-47dd-b6f`
  ("My First Project", org `adigupta1998-org`), zone `us-central1-a`, machine
  type `e2-micro`, Debian 13 (trixie), Python 3.13. Billing has ₹28,321
  free-trial credit expiring 2026-10-01.
- **External IP**: `35.239.254.99` — ⚠️ *ephemeral*, can change if the VM is
  stopped/started. Reserve a static IP before relying on it externally.
- **SSH**: GCP Console → Compute Engine → VM instances → **SSH** button
  (browser terminal, no key files). `gcloud compute ssh` also works if the
  gcloud CLI is configured locally, but it is not set up as of this writing.
- **Code lives at** `~/alpha_trading` on the VM, cloned from GitHub (`main`),
  with a Python venv at `~/alpha_trading/venv`.
- **Runtime**: the unified FastAPI API (`src.api:app`) runs continuously on
  port 8000 as a **systemd service** named `alpha-trading`
  (`/etc/systemd/system/alpha-trading.service`): `Restart=always`, enabled on
  boot. This includes the built-in hourly auto-sync loop. Health check:
  `http://localhost:8000/api/health` → `{"status":"ok","mode":"paper-only"}`.

  ```bash
  # deploy an update (on the VM)
  cd ~/alpha_trading && git pull && venv/bin/pip install -r requirements.txt
  sudo systemctl restart alpha-trading

  # operate
  systemctl status alpha-trading          # is it running?
  sudo journalctl -u alpha-trading -f      # live logs (Ctrl+C to exit)
  sudo systemctl restart|stop alpha-trading
  ```

- **`.env` on the VM** is NOT in git and must be transferred by hand. ⚠️
  **Do not paste the DhanHQ JWT directly into the browser SSH terminal** — a
  secret-scanner silently replaces the `eyJ...` token with bullet characters,
  causing `'latin-1' codec can't encode` errors at runtime. Working method:
  on the Mac, `base64`-encode `.env` and pipe a decode command to the
  clipboard, then paste that (the base64 blob isn't recognized as a token, so
  it survives):
  ```bash
  # on the Mac (fills clipboard with a ready-to-run command):
  printf 'echo %s | base64 -d > ~/alpha_trading/.env && echo OK\n' \
    "$(base64 < ~/Documents/Claude/alpha_trading/.env | tr -d '\n')" | pbcopy
  # then paste into the VM SSH window + Enter, then restart the service.
  ```
  Because `DHAN_ACCESS_TOKEN` is short-lived (~24h), keep it alive with the
  auto-renewal script instead of daily manual pastes: after ONE manual seed
  of a valid token, schedule `python3 -m src.renew_token` on the VM
  (`crontab -e`, e.g. `0 6 * * * cd ~/alpha_trading && venv/bin/python -m
  src.renew_token >> logs/renew_token.log 2>&1`). The manual base64 paste
  above is then only needed if a renewal window is missed and the token
  dies (script prints CRITICAL).
- **Not exposed to the internet**: port 8000 is reachable only on the VM
  itself (no firewall rule opened). To connect a deployed frontend, the
  recommended path is a **Cloudflare Tunnel** (free HTTPS, nothing exposed,
  survives IP changes) plus an API-key check in `src.api` — deferred, not yet
  done.
- **⚠️ Scheduled email jobs not migrated**: the old VM ran `src.main`
  (alerts, `35 15 * * 1-5`) and `src.suggest` (suggestions, `0 8 * * 1-5`)
  via cron. The new VM runs *only* the API server. To restore the daily
  cloud emails, add those two cron entries on the new VM (`crontab -e`, using
  `~/alpha_trading/venv/bin/python -m src.main`, logging to
  `~/alpha_trading/logs/`).
- `data/`, `tests/`, `logs/` are not part of the deploy (paper-trading state
  stays local only; see `OVERVIEW.md`). `config.json` and `.env` are required
  — `src/config.py` fails loudly at import without `config.json`, and
  `src/dhan_client.py` needs `.env`'s Dhan keys.

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

## Next steps / roadmap

**Phase 6 (Brain Map) steps 1–2 landed 2026-07-06**: `src/brain_map.py`
(native `sqlite3` store at `data/brain_map.db` — `events`, `outcomes`,
`event_outcome_link` tables, record/link helpers, and
`query_similar_events(tags)` returning `{count, win_rate, avg_r_multiple,
examples}`) plus `tests/test_brain_map.py` (offline in-memory tests). The
design remains banked in `DECISIONS.md` → "Phase 6 — Brain Map design".

**Phase 6 steps 3–4 landed later on 2026-07-06**: new journal entries now
carry a stable `short_id` (8-char uuid hex, `src/journal.py` — older lines
without one are fine, readers fall back to a composite
`date|ticker|action|price` key via `brain_map.journal_ref_for()`), and
`ingest_existing()` in `src/brain_map.py` idempotently seeds the map from
resolved `journal.jsonl` trades and `data/news_sentiment.json`. Run it any
time with `python3 -m src.brain_map ingest` (re-running is safe and picks
up newly resolved trades). The real `data/brain_map.db` now exists,
holding 10 news events; 0 outcomes so far because no journal trade has
resolved yet. Full suite: **55/55**.

**Phase 6 step 5 (the final step) landed later on 2026-07-06 — PHASE 6 IS
COMPLETE.** `forecast.py` now queries the map: when the current setup has
active pattern tags (fresh Golden Cross → `fresh_cross`+`golden_cross`,
oversold RSI → `rsi_oversold`), the forecast payload gains `memory` stats
and a `memory_context` line ("Historical Performance for active patterns
[...]: Win Rate: X%, ...") that `describe()` prints (terminal + Discord
`/analyze`). Advisory only — zero score points (decision #26 in
`DECISIONS.md`); empty/missing DB degrades to `memory: null` with the
standard flow untouched. `tuner.py`/`brain_weights.json` were never
modified. Suite: **63/63**. Contract addition documented in
`DATA_CONTRACT.md` § 2.4.

**Phase 6 core loop also landed 2026-07-06 (after step 5)** — the
feedback loop is now fully automatic. The moment `plan_tracker` resolves
a plan it (a) captures the original thesis + realized execution metrics,
(b) asks the new post-mortem analyst (`src/analyst.py`, Gemini,
never-raises) for a structured `{variance_analysis, unexpected_variables,
future_guardrails}` JSON, and (c) writes outcome + events + post-mortem
into the Brain Map keyed by the entry's `short_id`
(`brain_map.record_resolved_entry`, shared with `ingest_existing`). The
`outcomes` table gained a `post_mortem` column (auto-migrated in place on
connect). All fail-safe: no Gemini key / locked DB just prints a note,
journal resolution is never blocked. Suite: **71/71**.

**Ongoing Brain Map operation**: nothing manual needed anymore — resolved
trades flow in live via the tracker. `python3 -m src.brain_map ingest`
remains available as a backfill/repair sweep (it won't have post-mortems,
which only generate at live resolution). `memory_context` lines appear in
forecasts once the first trades resolve.

**Next up (nothing started)**: the other open items below — restore the
VM's scheduled email jobs, Cloudflare Tunnel for the API, Phase 7
historical simulator (see `DECISIONS.md` → "Still open").

Other open items (not next, but tracked): restore the cloud-scheduled email
jobs on the new VM, expose the VM API via a Cloudflare Tunnel for a deployed
frontend, and (Phase 7) the historical simulator — see `DECISIONS.md` →
"Still open".

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

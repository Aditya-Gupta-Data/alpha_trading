# Handover

The living status of the project. At the end of a chat, Claude refreshes this;
at the start of the next chat, paste it back in (or point Claude at the repo).

---

## Multi-tool workflow (read this)

This repo is built with more than one AI coding tool (Claude Code is primary,
Gemini CLI is an occasional backup). A few simple rules keep things from getting
tangled:

- **This folder on the Mac is the single source of truth.** GitHub is just the
  backup / cloud copy.
- **Only ONE tool edits at a time.** Never run two tools on this folder at once.
- **Always commit and push before ending a session or switching tools**, so the
  next tool or session starts from a clean, current state.
- **Before starting work, run `git pull` first** to make sure you have the latest
  pushed changes.
- Commits may come from either Claude Code or Gemini CLI — that is expected and fine.

---

## Where we are
**Phase 1: Alerting — web dashboard with an editable watchlist, running locally.**

Done:
- Full working pipeline: watchlist -> fetch prices -> check rules -> send alert.
- Rule engine tested with fake data (all 5 tests pass, no internet needed).
- **Web dashboard:** FastAPI backend + single-page frontend.
  - `GET  /api/health` — liveness check.
  - `GET  /api/watchlist` — one entry per instrument: symbol, type (stock/index),
    live price, % change today, and its configured rules (may be empty = watch-only).
  - `GET  /api/alerts` — only the currently-triggered rules.
  - `POST /api/watchlist` — **add** a stock or index `{symbol, type}`. Validates a
    real yfinance price BEFORE saving; rejects bad/duplicate symbols.
  - `DELETE /api/watchlist/{ticker}` — **remove** an instrument and its rules.
  - `GET  /` — dashboard UI (design matches alpha_dashboard_clean.html).
  - 30-second price cache so the UI can auto-refresh without hammering yfinance.
- **Editable watchlist (new this milestone):** add/remove stocks AND Indian indices
  from the Watchlist screen; changes are saved back to config/watchlist.yaml with
  comments preserved (ruamel.yaml). Indices map friendly names to "^" symbols
  (NIFTY 50 = ^NSEI, BANK NIFTY = ^NSEBANK, SENSEX = ^BSESN, NIFTY IT = ^CNXIT,
  NIFTY MIDCAP 50 = ^NSEMDCP50, INDIA VIX = ^INDIAVIX).
- UI: paper mode badge + "simulated · no real orders" rail always visible. Green/red
  on price numbers only. No order/execute/buy buttons anywhere.
- Live test confirmed: added RELIANCE (already present, dup rejected), added
  NIFTY 50 (₹24,021.65, +0.83%) and BANK NIFTY (₹58,150.35, +1.69%), both showed
  live prices via /api/watchlist, then deleted NIFTY 50 and confirmed it left the
  yaml. watchlist.yaml restored to its original 3 stocks after testing.

How to use add/remove (in the browser):
- **Add a stock:** keep the toggle on "Stock", type a symbol like `RELIANCE`
  (NSE assumed; or type `TCS.BO` for BSE), press Add.
- **Add an index:** click "Index", type a friendly name like `NIFTY 50` or
  `Bank Nifty`, press Add.
- **Remove:** click the ✕ button on any row.

Note on the repo: a parallel app from the other tool (`app/server.py`,
`src/engine.py`) still exists from an earlier merge. This milestone built on the
`src/web/` app per the task. Both were made tolerant of watch-only entries.

## How to run the web app

```bash
# From the project folder:
pip install -r requirements.txt
python3 -m uvicorn src.web.api:app --reload --port 8000
```

Then open: **http://localhost:8000**

The page auto-refreshes every 60 seconds. Hit the Refresh button for an instant update.

## File layout (what's new)
```
src/web/
  __init__.py
  api.py             ← FastAPI app (health, watchlist GET/POST/DELETE, alerts, serves index.html)
  watchlist_store.py ← add/remove items + index mapping + safe YAML writes (new)
  static/
    index.html       ← single-page dashboard with add/remove UI, vanilla JS, no framework
```
Engine files data_fetcher.py and rules.py were NOT changed. main.py and src/engine.py
got a one-line guard to skip watch-only entries (entries with no alert rule).

## Next steps (in order)
1. Let users set/edit alert RULES from the UI too (right now adds are watch-only;
   rules are still edited in the yaml).
2. Add Telegram (or email) alert delivery — plug into notifier.py, nothing else changes.
3. Add a scheduler so it checks automatically during market hours.
4. Host somewhere free (Railway, Fly.io, etc.) so the laptop doesn't need to stay on.
5. Decide whether to retire the duplicate `app/` version and keep one app.
6. Expand watchlist condition types, then move toward Phase 2 (suggestions).

## Open questions for the user
- Telegram or email for alerts? (Claude recommends Telegram.)
- Roughly how many stocks will you watch? (affects polling frequency.)

## Working notes
- User is non-technical: Claude writes all code and gives copy-paste steps.
- yfinance lags ~15 min; fine for alerts. Swap to Zerodha/Upstox feed when live trading is added.
- Engine layer (src/data_fetcher.py, src/rules.py) is kept separate from the web layer (src/web/) per DECISIONS.md.

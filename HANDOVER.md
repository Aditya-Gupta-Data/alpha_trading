# Handover

The living status of the project. At the end of a chat, Claude refreshes this;
at the start of the next chat, paste it back in (or point Claude at the repo).

---

## Where we are
**Phase 1: Alerting — web dashboard added, running locally.**

Done:
- Full working pipeline: watchlist -> fetch prices -> check rules -> send alert.
- Rule engine tested with fake data (all 5 tests pass, no internet needed).
- **Web dashboard (new):** FastAPI backend + single-page frontend.
  - `GET /api/health` — liveness check.
  - `GET /api/watchlist` — live prices + rules for every ticker in watchlist.yaml.
  - `GET /api/alerts` — only the currently-triggered rules.
  - `GET /` — dashboard UI (design matches alpha_dashboard_clean.html).
  - 30-second price cache so the UI can auto-refresh without hammering yfinance.
- UI: paper mode badge + "simulated · no real orders" rail always visible. Green/red on price numbers only. No order buttons anywhere.
- Live test confirmed (prices fetched successfully): RELIANCE.NS ₹1,313, TCS.NS ₹2,109, INFY.NS ₹1,056.

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
  api.py           ← FastAPI app (3 endpoints + serves index.html)
  static/
    index.html     ← single-page dashboard, vanilla JS, no framework
```
Engine files (data_fetcher.py, rules.py, notifier.py, main.py) were not changed.

## Next steps (in order)
1. Add Telegram (or email) alert delivery — plug into notifier.py, nothing else changes.
2. Add a scheduler so it checks automatically during market hours.
3. Host somewhere free (Railway, Fly.io, etc.) so the laptop doesn't need to stay on.
4. Expand watchlist condition types, then move toward Phase 2 (suggestions).

## Open questions for the user
- Telegram or email for alerts? (Claude recommends Telegram.)
- Roughly how many stocks will you watch? (affects polling frequency.)

## Working notes
- User is non-technical: Claude writes all code and gives copy-paste steps.
- yfinance lags ~15 min; fine for alerts. Swap to Zerodha/Upstox feed when live trading is added.
- Engine layer (src/data_fetcher.py, src/rules.py) is kept separate from the web layer (src/web/) per DECISIONS.md.

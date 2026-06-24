# Alpha Trading

A personal tool for Indian stocks (NSE/BSE), built in phases:

1. **Alerting** ← we are here
2. Suggesting
3. Trading
4. (more steps as needed)

Repo: https://github.com/Aditya-Gupta-Data/alpha_trading

---

## How this repo is built

This project is developed with local AI coding tools — primarily Claude Code,
with Gemini CLI as an occasional backup. Both work directly on the files in this
folder, which is the source of truth. GitHub is the remote backup copy.

---

## What works right now (Phase 1)

You keep a watchlist of stocks and rules in one plain file. The tool fetches the
latest prices, checks your rules, and tells you which ones triggered. Right now
alerts print to the screen; phone/email delivery is the next step.

```
config/watchlist.yaml   <- the only file you edit day-to-day
src/data_fetcher.py     <- gets prices (yfinance, free, no API key)
src/rules.py            <- the alert conditions + the engine that checks them
src/notifier.py         <- sends the alert (prints for now; Telegram next)
src/main.py             <- CLI entry point
src/web/api.py          <- FastAPI web server (3 API endpoints)
src/web/static/         <- dashboard frontend (single HTML file, no framework)
tests/test_rules.py     <- proves the rule logic works (no internet needed)
```

---

## How to run it

You need Python installed. If you're not sure you have it, search
"install Python" for your computer and follow the official python.org installer.

**1. Install dependencies** (run this once, in the project folder):

```
pip install -r requirements.txt
```

**2. Check the logic works** (instant, no internet needed):

```
python -m pytest tests/
```

You should see all 5 tests pass.

**3. Start the web dashboard** (during or after Indian market hours for live numbers):

```
python3 -m uvicorn src.web.api:app --reload --port 8000
```

Then open **http://localhost:8000** in your browser. You'll see your watchlist
with live prices and any triggered alerts. The page auto-refreshes every 60 seconds.

**Alternative — run the alerter from the terminal instead:**

```
python -m src.main
```

It prints each stock's status and an `[ALERT]` line for anything that triggered.

---

## Editing your watchlist

Open `config/watchlist.yaml` in any text editor. Each rule is three lines:
a `ticker` (NSE ends in `.NS`, BSE ends in `.BO`), a `condition`, and a `value`.
The file has comments explaining every option. No coding needed.

---

## Roadmap / what's next

- [ ] Send alerts to your phone (Telegram recommended) instead of just printing
- [ ] Run automatically on a schedule during market hours
- [ ] Host it so your laptop doesn't have to stay open
- [ ] More condition types (moving-average crossovers, volume spikes, etc.)
- [ ] Phase 2: suggestions
- [ ] Phase 3: trading (paper-trading first, with safety rails)

See `HANDOVER.md` for the current state and `DECISIONS.md` for why things are
built the way they are.

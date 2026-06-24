# Alpha Trading

A personal tool for Indian stocks (NSE/BSE), built in phases:

1. **Alerting** ← we are here
2. Suggesting
3. Trading
4. (more steps as needed)

Repo: https://github.com/Aditya-Gupta-Data/alpha_trading

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
src/main.py             <- ties it all together
tests/test_rules.py     <- proves the rule logic works (no internet needed)
```

---

## How to run it (one-time setup, then it's two commands)

You need Python installed. If you're not sure you have it, search
"install Python" for your computer and follow the official python.org installer.

**1. Install the two libraries** (run this once, in the project folder):

```
pip install -r requirements.txt
```

**2. Check the logic works** (instant, no internet needed):

```
python tests/test_rules.py
```

You should see all tests pass.

**3. Run the alerter** (during or after Indian market hours for live numbers):

```
python -m src.main
```

It prints each stock's status and an `[ALERT]` line for anything that triggered.

---

## Run the web app

A basic web app lets you see your watchlist and run a check in the browser
(works on your phone's browser too). One-time install, then one command:

```
pip install -r requirements.txt
uvicorn app.server:app --reload
```

Then open http://localhost:8000 in your browser and tap "Check now".

(The look is a simple placeholder for now — it'll be reskinned with the Google
Stitch design later.)

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

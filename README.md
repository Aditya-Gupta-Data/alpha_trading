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
src/notifier.py         <- sends alerts/digests (prints + emails via Gmail)
src/main.py             <- Phase 1: alerting, ties it all together
src/indicators.py       <- SMA + RSI technical indicators
src/suggestions.py      <- Phase 2: combines trend + momentum into a plain-English read
src/suggest.py          <- Phase 2: entry point, emails a daily suggestions digest
src/portfolio.py        <- Phase 3: the fake-money portfolio (data/portfolio.json)
src/strategy.py         <- Phase 3: turns signals into trade proposals
src/trade.py            <- Phase 3: interactive session — engine proposes, YOU decide
src/journal.py          <- Phase 3: logs every decision + your one-line "why"
src/review.py           <- Phase 3: scores old decisions after a week (the scorecard)
tests/test_rules.py     <- proves the rule logic works (no internet needed)
tests/test_portfolio.py <- proves the portfolio math + safety rails work
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

## Getting alerts by email

Alerts always print to the screen. To also get them emailed to you:

**1. Turn on 2-Step Verification** on your Google account (if not already on):
https://myaccount.google.com/security

**2. Create an App Password:**
https://myaccount.google.com/apppasswords
Name it "Alpha Trading" and copy the 16-character code it gives you.

**3. Set up your `.env` file** (one time):

```
cp .env.example .env
```

Open `.env` in a text editor and fill in:
- `ALERT_EMAIL_FROM` — your Gmail address
- `ALERT_EMAIL_APP_PASSWORD` — the code from step 2
- `ALERT_EMAIL_TO` — where alerts should land (defaults to `ALERT_EMAIL_FROM` if left blank)

That's it — `python -m src.main` will now email you whenever a rule triggers,
on top of printing to the screen. `.env` is git-ignored, so your password
never gets uploaded anywhere.

---

## Phase 2: Suggestions

On top of alerts, the tool can also give you a daily plain-English read on
each stock in your watchlist — combining trend (50-day vs 200-day moving
average) and momentum (14-day RSI). It's advisory only: it never places a
trade, it just tells you what it sees.

Run it manually anytime with:
```
python3 -m src.suggest
```

It's also scheduled to run automatically every weekday at 8:00 AM IST
(before market open) and email you the digest, using the same Gmail setup as
alerts. If you haven't already, load the schedule once with:
```
launchctl load ~/Library/LaunchAgents/com.alphatrading.dailysuggestions.plist
```

---

## Phase 3: Paper trading (fake money, real prices)

The engine proposes trades based on the Phase 2 signals — but **you** decide.
For every proposal you answer y/n and give a one-line "why". Approved trades
execute against a fake Rs. 1,00,000 portfolio; everything (including what you
rejected, and your reasoning) goes into a journal.

Start a trading session (best in the evening, after market close):
```
python3 -m src.trade
```

A week later, see how those decisions actually turned out:
```
python3 -m src.review
```

The scorecard rates every decision — the engine's signals AND your own calls
("WIN", "LOSS", "GOOD SKIP", "MISSED GAIN"...) — so over time you learn which
signals and which of your instincts to trust.

Safety rails: no broker is connected anywhere in this project, so it cannot
touch real money. Whole shares only, it can never overspend the fake cash, and
no single stock may exceed 25% of the portfolio.

---

## Hosting (the backend runs in the cloud)

The DhanHQ-backed API server (`src/api.py`) runs 24/7 on a free-tier Google
Cloud VM (`alpha-trading-vm`, project `project-37632031-10d0-47dd-b6f`),
rebuilt fresh on 2026-07-06. It starts automatically on boot and restarts
itself if it crashes (a systemd service called `alpha-trading`). Phase 3
(paper trading) stays on your Mac since it's interactive.

To get onto the VM: GCP Console → Compute Engine → VM instances → click the
**SSH** button next to `alpha-trading-vm` (opens a terminal in your browser —
no keys or extra tools needed).

To check on it (in that SSH window):
```
systemctl status alpha-trading             # is the server running?
sudo journalctl -u alpha-trading -n 40      # recent logs
python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/health').read().decode())"
```

To ship code changes (nothing auto-deploys — push to GitHub first, then on
the VM):
```
cd ~/alpha_trading && git pull && venv/bin/pip install -r requirements.txt
sudo systemctl restart alpha-trading
```

Notes: the server isn't exposed to the internet yet (reachable only on the VM
itself), and the old VM's scheduled email alerts/suggestions aren't running on
this new VM yet. See `HANDOVER.md` → "GCP VM (cloud hosting)" for the full
picture, the `.env` token-transfer gotcha, and next steps.

---

## Editing your watchlist

Open `config/watchlist.yaml` in any text editor. Each rule is three lines:
a `ticker` (NSE ends in `.NS`, BSE ends in `.BO`), a `condition`, and a `value`.
The file has comments explaining every option. No coding needed.

---

## Roadmap / what's next

- [x] Send alerts by email (Telegram is banned in India, so email is the channel)
- [x] Run automatically on a schedule during market hours (daily via macOS launchd)
- [x] Host it so your laptop doesn't have to stay open (running on a free-tier Google Cloud VM)
- [ ] More condition types (volume spikes, etc.)
- [x] Phase 2: suggestions (trend + momentum digest, emailed daily at 8am)
- [x] Phase 3 (paper): human-in-the-loop paper trading with a reasoning journal + scorecard
- [ ] Phase 3 (real): connect a broker — only after the paper scorecard earns trust

See `HANDOVER.md` for the current state and `DECISIONS.md` for why things are
built the way they are.

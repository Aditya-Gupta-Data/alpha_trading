# Handover

The living status of the project. At the end of a chat, Claude refreshes this;
at the start of the next chat, paste it back in (or point Claude at the repo).

---

## Where we are
**Phase 1: Alerting — scaffold built and pipeline working.**

Done:
- GitHub repo created (empty), scaffold ready to upload: https://github.com/Aditya-Gupta-Data/alpha_trading
- Full working pipeline: watchlist -> fetch prices -> check rules -> send alert.
- Rule engine tested with fake data (all tests pass, no internet needed).
- Alerts currently print to screen.
- Configurable watchlist in `config/watchlist.yaml` (4 condition types:
  price_above, price_below, percent_up, percent_down).

## Next steps (in order)
1. Get the scaffold into the GitHub repo (upload via browser, one time).
2. User runs it locally to see live alerts print.
3. Add real alert delivery — Telegram recommended (needs a ~5-min one-time bot setup).
4. Add a scheduler so it checks automatically during market hours.
5. Host it somewhere free so the laptop doesn't need to stay open.
6. Then expand condition types, and move toward Phase 2 (suggestions).

## Open questions for the user
- Telegram or email for alerts? (Claude recommends Telegram.)
- Roughly how many stocks will you watch? (affects polling frequency.)

## Working notes
- User is non-technical: Claude writes all code and gives copy-paste steps.
- yfinance can't be live-tested inside Claude's sandbox (Yahoo not reachable there),
  so the data fetcher is verified by code review + the rule engine is verified by tests.
  Live price fetching is confirmed when the user runs it on their own machine.

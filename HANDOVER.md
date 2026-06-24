# Handover

The living status of the project. At the end of a chat, Claude refreshes this;
at the start of the next chat, paste it back in (or point Claude at the repo).

---

## Where we are
**Phase 1: Alerting — engine done, and a basic web app added.**

Done:
- Core engine: watchlist -> fetch prices (yfinance) -> check rules -> results.
- Shared `evaluate_watchlist()` in `src/engine.py` — one function the CLI and the
  web app both call (returns results as plain data).
- Basic web app (FastAPI backend + one responsive page):
  - `GET /`              -> the web page (watchlist + "Check now" button)
  - `GET /api/watchlist` -> current watchlist
  - `GET /api/check`     -> runs a live check, returns results as JSON
  - Verified: server boots, all endpoints return 200, errors handled gracefully.
- CLI (`python -m src.main`) and tests still work unchanged.

## The app direction (decided)
- End goal: a phone app. Path: build a **web app first** (shared backend), then make
  it installable on the phone (PWA) for an app feel without the app store. A true
  native wrapper (Capacitor) stays an option later and reuses the web code.
- The current web UI is a plain, functional MVP — it will be reskinned with the
  Google Stitch design later.

## Next steps (in order)
1. Run the web app locally and open it in a browser (see README).
2. Add a small database (SQLite) for the watchlist + a history of past alerts.
3. Reskin the UI with the Google Stitch design.
4. Add a scheduler so it checks automatically during market hours.
5. Host it online + make it installable on the phone (PWA).
6. Real alert delivery (Telegram/email) as a push channel — can slot in anytime.
7. Then Phase 2 (suggestions) and Phase 3 (trading) become new screens.

## Open questions for the user
- Telegram or email for phone pings? (Claude recommends Telegram.)
- Do you have a Google Stitch design ready, or build basic and reskin later?

## Working notes
- User is non-technical: Claude writes all code, gives copy-paste steps.
- This chat can't push to GitHub; Claude Code (connected locally) does commits/pushes.
- yfinance can't be live-tested in Claude's sandbox (Yahoo blocked there), so data
  fetching is verified by running on the user's machine; logic is verified by tests.

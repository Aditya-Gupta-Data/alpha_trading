# Decisions

Choices we've locked in, and why. Update this whenever we make a new call so we
never have to re-argue it in a future chat.

| # | Decision | Why |
|---|----------|-----|
| 1 | Tool is for personal use only | No multi-user infrastructure, no compliance overhead of serving others. |
| 2 | Market: Indian stocks (NSE/BSE) | User trades from India; Indian brokers have the most API-friendly setups. |
| 3 | Language: Python | Best ecosystem for finance libraries; user is non-technical, so Claude writes the code. |
| 4 | Data source (for now): yfinance | Free, no API key, supports NSE (.NS) / BSE (.BO). Can lag ~15 min — fine for alerts. Swap to a broker feed (Zerodha/Upstox) when live trading is added. |
| 5 | Alert rules are configurable, not fixed | User wants to decide triggers over time; engine is built so new condition types are a one-function add. |
| 6 | Build order: alerting -> suggesting -> trading | Incremental; each phase stacks on a stable base. Extra steps inserted as needed. |
| 7 | Repo on GitHub | https://github.com/Aditya-Gupta-Data/alpha_trading |
| 8 | UI via Google Stitch, added later | Design there, wire up logic behind it. Phase 1 uses notifications, not a dashboard. |
| 9 | Trading phase will start in paper-trading mode with safety rails | Test with fake money before any real order. |
| 10 | Repo may be edited by more than one local AI coding tool (Claude Code primary, Gemini CLI as backup) | Avoids getting blocked if one tool is unavailable. All tools edit the same files in this local folder; the folder is the source of truth. No tool needs to "see" another — they just open the same folder. |
| 11 | Watchlist is now user-editable from inside the web app (add/remove stocks and indices) | No more hand-editing YAML for everyday changes. The web layer validates a live price before saving and writes config/watchlist.yaml with comments preserved (ruamel.yaml). |
| 12 | Indices are supported via their yfinance "^" symbols (e.g. NIFTY 50 = ^NSEI, BANK NIFTY = ^NSEBANK) | User picks a friendly name; we store/fetch the raw ticker. A watchlist entry can be "watch-only" (price shown, no alert rule). |
| 13 | Kite/Zerodha stays out until the trading phase | Phase 1 is read-only, paper mode. No broker, no API keys, no orders. Any future action is Approve/Dismiss only. |
| 14 | `src/web/` is the single canonical web app | An earlier merge of unrelated histories left a duplicate app (`app/server.py` + `src/engine.py`). It was retired so there's one app to build on, run, and reason about. Run command: `uvicorn src.web.api:app`. The design reference lives at `design/alpha_dashboard_clean.html`. |

## Still open
- Alert delivery channel (Telegram vs email) — user undecided; Claude recommends Telegram.
- Where it runs always-on (hosting) — to be decided after the logic is solid.

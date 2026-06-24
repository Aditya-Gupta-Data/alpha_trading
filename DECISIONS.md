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

## Still open
- Alert delivery channel (Telegram vs email) — user undecided; Claude recommends Telegram.
- Where it runs always-on (hosting) — to be decided after the logic is solid.

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

| 10 | Alert delivery: email, not Telegram | Telegram is banned/unavailable in India for the user. Email via Gmail is the fallback. |
| 11 | Paper trading is human-in-the-loop with a reasoning journal | User's call: the engine proposes, the user approves/rejects and logs a one-line "why". Review scores both the engine's signals and the user's instincts, so the system (and user) improve from real feedback. |
| 12 | Paper portfolio starts at Rs. 1,00,000 | Realistic personal-portfolio size; big enough for several positions. Safety rails: whole shares only, never overspend cash, max 25% of portfolio in one stock. |
| 13 | Phase 4 replanned around a user-provided "Plan and scope" doc, not the original `aditrader-phase4-master-handoff-prd.md` 4A/4B/4C breakdown | That PRD only covered structured journaling, news sentiment, and a win-rate auto-tuner — it didn't include forecasting, full trade plans (entry/stop/target/invalidation), or automatic plan-outcome tracking, all of which the scope doc requires. See `PLAN.md` for the new 4A-4F order. |
| 14 | Markets: NSE/BSE equities AND options, options deferred to Phase 5 | Options are real scope, not dropped, but yfinance's NSE options data isn't reliable enough to build on yet — needs its own data-source decision first. |
| 15 | Holding period: swing (multi-day) first, intraday explicitly later | Matches the existing daily 50/200-day SMA + RSI cadence; intraday needs real-time data and a faster loop, which is a separate rebuild. |
| 16 | News sourced via free RSS/Google News, LLM-summarized to a tiny sentiment JSON | No subscription cost; keeps core trading scripts isolated from raw article text (only ever reads `data/news_sentiment.json`). |
| 17 | No dedicated in-tool dialogue/chat feature for v1 | The user already discusses theses with Claude directly in sessions with full journal context; Phase 4A's structured levers capture the outcome of that conversation instead of rebuilding chat inside the app. |

## Still open
- Where it runs always-on (hosting) — to be decided after the logic is solid.
- NSE options data source (Phase 5) — not yet investigated.

## Resolved gotchas worth remembering
- Gemini API keys created via AI Studio's "create in new project" button get zero free-tier quota (HTTP 429, limit:0). Create keys against the existing billed `alpha-trading-app-2026` GCP project instead.
- Pin Gemini model names loosely: `gemini-2.0-flash` was deprecated (HTTP 404) within months. `src/news_processor.py` now uses the `gemini-flash-lite-latest` alias, which auto-tracks Google's current model.

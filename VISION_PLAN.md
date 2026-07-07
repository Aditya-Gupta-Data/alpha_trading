# 🚀 ADiTrader Phase 5+: The AI Hedge Fund & Brain Map

(User-shared master blueprint, saved 2026-07-05. This is the source plan for
Phases 5-8. Same working rule as Phase 4: confirm before each step, one file
at a time. See HANDOVER.md for current build status.)

## 1. System Vision & Constraints

We are evolving ADiTrader from a static daily rule-checker into a continuous,
event-driven AI Analyst. It will ping the user's phone, discuss setups via
chat, propose trades based on complex historical event patterns, and learn
from a time-travel backtesting simulator.

### 🛑 CRITICAL STRICT GUARDRAILS (Read First)

1. **STRICTLY PAPER TRADING:** For at least the next 30 days, **ZERO real
   broker execution**. The final output of an approved trade must *always*
   route to `data/portfolio.json`. Do not import or write any code that
   attempts to place real orders.
2. **No Token Bloat:** The system remains heavily modular. The chatbot
   interface, data scrapers, and execution engine must remain separate files
   that communicate via local databases/JSON.
3. **No Heavy Databases:** Do not suggest Postgres or MongoDB. The "Brain
   Map" will be built using Python's native `sqlite3` to keep the project
   portable and free.

---

## 2. Phase Breakdown & Execution Plan

### 📱 Phase 5: The Discord Analyst Interface (Mobile/Web Ping)

**Objective:** Move the interactive `trade.py` session off the Mac terminal
and onto the user's phone using Discord. Discord acts as the front-end app
(mobile, web, and desktop), supporting interactive buttons (`[Approve]`,
`[Paper Trade]`, `[Discuss]`) and chat threads.

* **External User Action Required (Do this before coding):**
  1. Go to the Discord Developer Portal: https://discord.com/developers/applications
  2. Click "New Application" -> Name it "ADiTrader Analyst".
  3. Go to the "Bot" tab -> Click "Reset Token" -> Copy this
     `DISCORD_BOT_TOKEN`.
  4. Scroll down and enable **Message Content Intent** (required to read
     your chat replies).
  5. Go to "OAuth2" -> "URL Generator" -> Select `bot` -> Select
     permissions: `Send Messages`, `Read Messages/View Channels`. Use the
     URL to invite the bot to a private Discord server you created.
  6. Add `DISCORD_BOT_TOKEN="your-token"` to your local `.env` file.

* **Claude Code Prompt (Step 1):**
  > "Read Phase 5 of VISION_PLAN.md. Create a new standalone file
  > `src/discord_bot.py`. Use the `discord.py` library. The bot should
  > connect using `DISCORD_BOT_TOKEN` from `.env`. Create a slash command
  > `/analyze` that runs our existing `src.forecast` logic and posts the
  > result in the Discord channel. Also, create a listener so if the user
  > replies to the bot, it sends the user's text to the Gemini API to
  > generate a conversational response. Do not wire this into `trade.py`
  > execution yet. Test the bot connection."

---

### 🧠 Phase 6: The Brain Map (SQLite Event Memory)

**Objective:** Upgrade `data/brain_weights.json` to an SQLite database
(`data/brain_map.db`). We need to log complex patterns (e.g., "Reporter X +
Block Deal + Friday = Bullish") rather than just numerical weights.

* **How it works:** When a forecast is generated, the AI queries the Brain
  Map: *"Has this specific cluster of events (e.g. Earnings Miss + Insider
  Buy) happened before, and did it make money?"*
* **External User Action:** None. Python handles SQLite natively.
* **Claude Code Prompt (Step 2):**
  > "Read Phase 6 of VISION_PLAN.md. Create a new file `src/brain_map.py`.
  > Use Python's native `sqlite3` to create a local database
  > `data/brain_map.db`. Create tables to store: 1) Historical Events
  > (date, ticker, event_type, entities_involved, sentiment) and 2) Trade
  > Outcomes (linked to events, showing R-multiple and win/loss). Write
  > helper functions to insert a new event, and a function
  > `query_similar_events(event_tags)` that returns the historical success
  > rate of a specific pattern. Ensure backward compatibility with our
  > current `journal.jsonl`."

---

### ⏳ Phase 7: The Time-Travel Simulator (Historical Training)

**Objective:** Train the Brain Map without waiting months in real-time. We
will build a simulator that pretends it is a past date (e.g., Jan 1, 2021).
It will step forward one day at a time, look at the "news" and "prices" for
that day, make paper trades, peek at the actual future to resolve the
trades, and log the wins/losses into the Brain Map.

* **How it works:** We create `src/simulator.py`. It overrides the
  `datetime.now()` function across the app. It loops from `start_date` to
  `end_date`, rapidly executing the Phase 4 logic and populating the Brain
  Map with years of experience in minutes.
* **External User Action:** None.
* **Claude Code Prompt (Step 3):**
  > "Read Phase 7 of VISION_PLAN.md. Create `src/simulator.py`. This script
  > takes a `--start-date` (e.g., 2021-01-01) and an `--end-date`. It must
  > run a loop simulating the passage of time day-by-day. For each day, it
  > fetches historical OHLC data from yfinance up to that simulated date.
  > It generates a trade plan using our existing Phase 4 logic,
  > automatically 'approves' it for paper trading, and uses the subsequent
  > historical data to resolve the trade (Win/Loss). Finally, it logs the
  > outcome into `src/brain_map.py`. Crucially, ensure the simulator NEVER
  > looks at data past the 'current' simulated day when generating a
  > forecast."

---

### 📈 Phase 8: Advanced Data Ingestion (Block Deals & Options Prep)

**Objective:** Expand the watchlist data beyond Yahoo Finance. We need to
scrape or fetch NSE Block Deals, Corporate Actions, and Options Chain data.
*Note: We are only ingesting this data to feed the Brain Map and Discord
Analyst. We are still exclusively paper trading.*

* **External User Action Required:** You will need a reliable, free data
  source for NSE events. As a non-technical user, the easiest path is
  opening a free API account with an Indian broker (like Dhan or Upstox)
  *strictly for data access*.
  1. Create an API app on DhanHQ or Upstox Developer portal.
  2. Generate the API Data Tokens.
  3. Add them to `.env`.

* **Claude Code Prompt (Step 4):**
  > "Read Phase 8 of VISION_PLAN.md. We need to feed advanced events into
  > the Brain Map. Create `src/event_scraper.py`. Write a script that
  > fetches recent Block Deals, Bulk Deals, and Earnings announcements for
  > our watchlist tickers. (For now, use public NSE website scraping
  > techniques or standard Python financial libraries if API keys aren't
  > provided yet). Pass these extracted events through Gemini to extract
  > 'entities' (e.g., Buyer Name, Reporter Name) and save them into
  > `brain_map.db`. Do not alter the paper trading execution logic."

---

## 🛠️ How to use this document with Claude Code

1. Save this text as `VISION_PLAN.md` in your project root. (Done 2026-07-05.)
2. Perform the **External User Action** for Phase 5 (setting up the Discord
   Bot).
3. Open Claude Code in your terminal.
4. Copy and paste the exact prompt located under **Claude Code Prompt
   (Step 1)**.
5. Test the Discord bot on your phone. Chat with it. Make sure it responds.
6. Only once Phase 5 is working perfectly, move to Step 2.

---

## ⚠️ 2026-07-06 renumbering note

Phases 5-8 above are the **original** blueprint (saved 2026-07-05) and are
largely **done**: Discord Analyst (Phase 5 above) is live, the Brain Map
(Phase 6 above, all steps) is complete and wired into `forecast.py`, and
data ingestion moved to the DhanHQ Data API instead of scraping (superseding
Phase 8 above's NSE-scraping approach — see `DECISIONS.md` decision #22).
Phase 7 above (the Time-Travel Simulator) — the last piece of this original
blueprint — was **BUILT 2026-07-07** as `src/simulator.py`, with two
deliberate departures from the prompt above: DhanHQ history instead of the
stale yfinance instruction, and as-of-date parameter injection instead of
overriding `datetime.now()` (decision #36 — the monkeypatch approach was
flagged as a risk when this plan was first reviewed). **The original
blueprint is now fully realized end to end.**

The **current, active roadmap lives in one place: `HANDOVER.md` → "🚀 The
Master Execution Plan", "📋 Pending Phases", and "🔮 The Long-Term Vision
(Phases 9–13)"**. That is the single authoritative copy carrying live
build-status/checkmarks — it is deliberately NOT duplicated here, because a
second copy drifts out of sync (it already had, on the Sleep Phase and
Phase 10B status). This file (`VISION_PLAN.md`) keeps the *narrative* vision —
the original blueprint above and the "why" behind each phase — while
`HANDOVER.md` owns the checklist and what is / isn't built.

**Newer phases added since the original blueprint** (full status in HANDOVER):
- **Phase 6C — Knowledge Graph Reasoning Layer** (2026-07-07): a read-only
  `networkx` reasoning layer (`src/graph_engine.py`) over an additive
  `graph_edges` table in the same `data/brain_map.db`. It walks 2-hop causal
  links and surfaces historically-linked patterns as advisory context in the
  Discord trade proposal — never a rule change (decision #33).
- **Phase 6D — Causal Triple Writer** (2026-07-07): the Sleep Phase
  (`src/sleep_phase.py` Task D) mines `(subject)-[predicate]->(object)`
  causal triples from reviewed trade outcomes + post-mortems ONLY — never raw
  news sentiment (decision #34) — and writes them into `graph_edges` for 6C to
  read. Populates once trades resolve and a Sleep Phase runs with Ollama up.

**Do not execute any roadmap item until explicitly prompted by the user.**

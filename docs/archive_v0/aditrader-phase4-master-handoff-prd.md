# 🚀 ADiTrader Phase 4: Master Handoff & PRD

> **How to use this document:** Copy relevant sections and paste them straight into **Claude Code** or your **Gemini CLI** context window. It contains explicit instructions telling the AI where to look, what to avoid, and which model is optimized for each task to keep your API tokens and wallet happy.

## 1. System Overview & Current State

ADiTrader is a modular, personal paper-trading system for Indian stocks (NSE/BSE). It is strictly rule-based, operates with zero real-broker connectivity, and acts as an insulated playground for testing market strategies.

### Current File Architecture

- `alert_manager.py` (Cloud - VM): Watches watchlist, fires emails at 3:35 PM IST if price limits or bands (default 3%) breach.
- `suggestion_engine.py` (Cloud - VM): Generates daily digest at 8:00 AM IST. Calculates 50/200 Day Moving Average (Trend) and 14-day RSI (Momentum).
- `strategy.py` (Local): Generates BUY signals on fresh Golden Crosses or Uptrend + Oversold RSI. Generates SELL signals on Death Crosses.
- `journal.py` (Local): Tracks interactive user approvals/rejections and free-text reasons. Appends to `data/journal.jsonl`.
- `review.py` (Local): Runs weekly scorecard analysis. Marks outcomes as `WIN`, `LOSS`, `GOOD_SKIP`, or `MISSED_GAIN`.

### Active Sandbox State

- `data/portfolio.json`: Fake ₹1,00,000 baseline portfolio. Currently holds ₹75,091 cash and 106 shares of `ONGC.NS` (Executed: 2026-07-03 at ₹234.99).
- `data/journal.jsonl`: Actively logging entries. First scorecard run anchor point: **2026-07-10**.

## 2. Core Constraints & Token Guardrails

To prevent code generation loops from burning through context window budgets, the AI must strictly adhere to these architectural rules:

1. **The State Isolation Principle:** Core execution scripts must never calculate text tokens or read web scrapers directly. All input data must arrive via lightweight, intermediary JSON files.
2. **No Refactoring Spree:** Do not rewrite existing functional tracking loops inside `review.py` or asset calculations inside `portfolio.json` unless explicitly asked.
3. **Targeted Scope:** Always read configuration variables from an externalized configuration file instead of hardcoding multipliers inside the Python files.

## 3. Step-by-Step Feature Implementation Specification

### 📋 Phase 4A: Structured Rationales & Risk Levers

- **Objective:** Transform free-form journal entries into data points that can be mathematically evaluated.
- **What it does:** Prompts the user for numerical levers (Stop-Loss %, Target Investment size) and a list of specific pattern identifiers during the daily interactive review session.
- **File Changes:** Update `journal.py` to append structured fields to `data/journal.jsonl`. Update core proposal functions to handle missing keys gracefully on older log entries.

```
[Old Entry] ---> "Why: Felt like it was oversold"
[New Entry] ---> { "why": "Oversold bounce", "risk_levers": {"sl_pct": 3.0, "size": 10000}, "pattern_tags": ["RSI_Oversold", "Support_Touch"] }
```

#### 🧠 Model Recommendation & Prompt Instruction

> **Best Model:** `Claude Code` / `Claude 3.5 Sonnet`
>
> **Why:** This involves modifying existing operational terminal loops and interactive input controls where precision structural refactoring is critical.

**Claude Instruction Code:**

```
Examine 'journal.py'. Modify the interactive user confirmation prompt. Add input variables allowing the user to select their desired investment capital and a protective stop-loss percentage. Add a prompt requesting comma-separated text tags detailing the visual chart pattern they see (e.g. Support, Resistance, Breakout). Ensure these save cleanly into 'data/journal.jsonl' without deleting previous data layout keys.
```

### 📰 Phase 4B: Isolated News & Sentiment Extractor

- **Objective:** Ingest market news data without feeding full-text articles into the core trading scripts.
- **What it does:** A separate utility script fetches financial RSS feeds or news strings, uses an LLM to evaluate them, and dumps a minified index integer into a text state file.
- **File Changes:** Create a completely new, detached file named `news_processor.py` that writes directly to `data/news_sentiment.json`.

| Field Name | Type | Purpose |
| --- | --- | --- |
| `sentiment_score` | Integer | Scale from -5 (Bearish) to +5 (Bullish) |
| `headline_focus` | String | Max 3-word summary of the primary news driver |
| `last_updated` | Timestamp | Prevent stale sentiment indicators from causing drift |

#### 🧠 Model Recommendation & Prompt Instruction

> **Best Model:** `Gemini 1.5 Flash` or `Gemini 2.0 Flash` via API execution
>
> **Why:** Flash models feature massive context windows with extremely low cost per token. They excel at processing massive volumes of raw text articles and summarizing them down to basic JSON metrics.

**Claude Instruction Code:**

```
Create a completely isolated script called 'news_processor.py'. This script must connect to a public news RSS feed or Google News search string for specified tickers. Write a routine utilizing the Gemini API to scan the text headlines and return a highly compressed JSON payload to 'data/news_sentiment.json'. The payload must contain a sentiment_score from -5 to +5 and a short headline summary string. Do not import any core trading dependencies here.
```

### 🎛️ Phase 4C: The Closed-Loop Auto-Tuner Engine

- **Objective:** Allow the software to adjust its baseline configurations automatically based on historical performance.
- **What it does:** Reads `data/journal.jsonl` immediately after `review.py` runs. Compiles a scorecard of wins/losses against the pattern tags generated in Phase 4A. Writes tuning adjustments to a weight configuration file.
- **File Changes:** Create `tuner.py` to write weight updates to `data/brain_weights.json`. Modify `strategy.py` to use these multipliers when generating trade scores.

#### 🧠 Model Recommendation & Prompt Instruction

> **Best Model:** `Claude Code` / `Claude 3.5 Sonnet`
>
> **Why:** Logic and mathematical parsing of file structures require high reasoning density.

**Claude Instruction Code:**

```
Build a standalone pipeline utility called 'tuner.py'. It should read through 'data/journal.jsonl' and locate records where 'verdict' equals WIN or LOSS. Group these outcomes by their historical 'pattern_tags'. If a specific pattern tag exhibits a win rate lower than 45% over a minimum sample size, generate a penalty multiplier and save it to 'data/brain_weights.json'. Modify 'strategy.py' to read this JSON file and deflate proposal confidence rankings for failing tags.
```

## 4. Operational Maintenance Guide (For Non-Technical Control)

To ensure you maintain perfect alignment with the code base without breaking your configurations, follow this weekly checklist.

### Verification Protocol

1. **Check JSON Integrity:** If the trading engine acts unexpectedly, manually check your data store files (`config.json` and `data/brain_weights.json`). Ensure there are no broken brackets or raw texts.

2. **Verify Environment Keys:** The `news_processor.py` requires your active Gemini API key. Make sure your shell contains the variable before initiating:

   ```bash
   export GEMINI_API_KEY="your-api-key-here"
   ```

3. **Run Isolated Tests:** Always test individual functional components rather than running the whole layout loop at once:

   ```bash
   python3 news_processor.py
   cat data/news_sentiment.json
   ```

# ARCHITECTURE.md

## 1. Executive Summary & Vision
* **Project Name:** Alpha
* **Target:** Personal algorithmic trading tool for Indian stocks (NSE/BSE).
* **Owner Profile:** Single-user (Aditya Gupta), non-technical PM. Code is written entirely by AI tools (Claude Code / Gemini CLI).
* **Core Philosophy:** Safety-first, strict human-in-the-loop validation, leaning on public open-source libraries without adopting opaque pre-built trading bots.

## 2. Locked Architecture Rules (Non-Negotiable)
* **Zero Auto-Execution:** The application only SUGGESTS or ALERTS. Action space is strictly limited to Approve/Dismiss. No automated order placement without manual confirmation.
* **Data Layer:** Initial phase relies on `yfinance` (~15-minute delay). Transition to Zerodha Kite Connect API happens strictly at the active trading phase using a Broker Abstraction layer (`PaperBroker` vs `KiteBroker`).
* **Alert Infrastructure:** Notifications route through a dedicated Discord Webhook. No WhatsApp or Telegram APIs.
* **Mobile Delivery:** Implemented as a responsive Web URL optimized as a Progressive Web App (PWA) to allow 'Add to Home Screen' functionality. No native iOS/Android builds.

## 3. System Architecture & Component Mapping
* **Current Stack:** FastAPI backend (`src/web/api.py`), Vanilla JS/Tailwind CSS dark-themed UI dashboard (`src/web/static/index.html`), configuration handled via `config/watchlist.yaml`.
* **State Management:** Currently file-based (`watchlist.yaml`). Must implement safe file-locking protocols during concurrent read/write loops.

## 4. Immediate Development Milestones
* **Milestone 1: Alert Rules Engine & Schema Expansion:** Refactor `watchlist.yaml` to hold nested alert rules per ticker (e.g., `price_above`, `price_below`) with a `triggered_at` cooldown state. Implement a front-end modal popup for rule management.
* **Milestone 2: Discord Rich Embed Integration:** Utilize `httpx` to send structured, color-coded cryptographic data payloads directly to Discord webhooks without heavy external bot wrappers.
* **Milestone 3: Market-Hours Scheduler:** Implement `APScheduler` pinned strictly to `Asia/Kolkata` timezone, operating a 5-minute loop Monday-Friday, 9:15 AM - 3:30 PM IST.

## 5. Future Scale Targets (24/7 Production Grade)
* **Hosting Pipeline:** Deploy via Oracle Cloud Infrastructure (OCI) Always Free Tier (Arm-based Ampere instance) inside a Docker container for continuous 24/7 operations.
* **Telemetry & Feedback Loop:** Transition state from YAML to an optimized relational database (SQLite/PostgreSQL) tracking a `signals_log`, `trade_ledger`, and `performance_metrics` table to feed token-efficient analytics back into LLMs (Claude/Gemini) for automated strategy optimization.
* **Pre-Trade Risk Management (PTRM):** Build a hard-coded system-level protection layer governing Max Daily Loss Limits, Max Allocation per Ticker, and Idempotency Keys to shield execution from logic bugs.

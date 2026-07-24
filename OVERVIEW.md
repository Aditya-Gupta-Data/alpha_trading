# OVERVIEW.md — Project Vision & Non-Negotiables

Read this first. It answers "what is this project and what must never change."
For system flow see `ARCHITECTURE.md`; for the component index see
`MODULES.md`; for why specific calls were made see `DECISIONS.md`; for
credentials/boot commands see `HANDOVER.md`; for where to record new work
without creating drift see `docs/DOC_GUIDE.md`.

## What this is

**ADiTrader** — a personal, AI-built autonomous trading-signal engine for
Indian equities (NSE/BSE), owned and used by one non-technical user. Claude
Code writes and maintains all code; the user directs scope and approves every
trade. The long-term shape is an "Autonomous Options Engine": news + macro
events + technicals feed a forecast layer, which proposes full trade plans
(entry/stop/target/rationale), which the user approves or dismisses from a
Discord bot or a local web dashboard, which get tracked automatically against
real market data and fed back into a learning loop.

## Non-negotiables (do not violate these without the user explicitly saying so)

1. **Paper-trading only.** There is no broker order-execution code anywhere in
   this project, and there must not be until the user explicitly lifts this.
   Approved trades write to `data/portfolio.json` (fake cash, real prices)
   and `data/journal.jsonl` — never to a real brokerage account. `/api/decision`
   in `src/api.py` REFUSES an `APPROVE_REAL` decision (403) by design.
2. **Human-in-the-loop.** The engine proposes; a human (or, later, a
   structured review step) approves or dismisses. No fully autonomous
   execution loop exists or is planned while paper-only holds.
3. **24/7 cloud execution for the passive parts.** Alerting and suggestions
   are meant to run unattended on a cloud VM (currently GCP, see
   `ARCHITECTURE.md`) on a market-hours cron schedule — not depend on the
   user's laptop being open. Interactive parts (paper trade approval,
   Discord chat) can run locally or on the VM.
4. **Dhan-native market data.** As of 2026-07-06 the engine's sole market-data
   source is the DhanHQ Data API (`src/dhan_client.py`), not yfinance. See
   `DECISIONS.md` for why. Any new data need should extend `dhan_client.py`,
   not reintroduce a second data source without a decision logged.
5. **Local, file-based state — no cloud database.** Portfolio, journal, news
   sentiment, forecast weights: all local JSON/JSONL under `data/` (git-
   ignored, personal). No Supabase/Postgres/cloud DB for engine state. See
   `DECISIONS.md`.
6. **Decoupled frontend.** The Python engine (this repo, `main` branch) and
   the Lovable-built React UI (`lovable-frontend/`, gitignored on `main`,
   version-controlled only on `lovable-ui`) are strictly separated. Backend
   code stays framework-free. See `ARCHITECTURE.md` and `DATA_CONTRACT.md`.
7. **One file at a time, confirm before each build step.** The user's
   standing rule: don't build a new phase/step unversioned or unconfirmed.
   These 5 docs are updated only at milestones, not on every commit (see
   "When to update" below) — but code changes still get confirmed per the
   user's usual working style.

## Current scope (2026-07-06)

- **Done:** alerting, suggestions, paper trading with human-in-the-loop
  journaling, full trade plans (entry/stop/target/rationale), automatic plan
  outcome tracking, news sentiment (Gemini-summarized), a rule-based forecast
  layer, a learning-loop tuner, a Discord analyst bot, a unified local REST
  API, a React dashboard (Supabase-free, talks to the local API), and a full
  migration off yfinance onto DhanHQ for market data.
- **Deferred:** options trading (Phase 5, needs a reliable NSE options data
  source — Dhan's option-chain API is now available and unblocks this),
  intraday holding period (needs real-time streaming, not the current daily
  cron cadence), a SQLite "Brain Map" for macro/event pattern memory (Phase
  6, designed but not built), a historical backtest simulator (Phase 7).
- **Explicitly out of scope:** real money execution, multi-user support,
  any broker order-placement code.

## When to update these 5 docs

Only at a **milestone state** — e.g. after a cloud deploy, after a new major
component ships (a new bot, a new data provider, a schema change), or when a
locked decision changes. Do NOT rewrite these for routine commits, bug fixes,
or small feature additions; those stay in git history / commit messages. If
you're unsure whether something is a milestone, ask the user rather than
silently updating (or silently skipping).

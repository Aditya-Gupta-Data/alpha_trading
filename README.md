# ADiTrader

An autonomous, AI-built trading-**signal** engine for Indian markets (NSE/BSE
equities and options), owned and directed by one user. It ingests market data,
news, deals and flows; proposes full trade plans (entry / stop / target /
rationale); a human approves or dismisses each from Discord; and every outcome is
tracked and fed back into a learning loop (the "Brain Map"). **Paper-trading only
— there is no broker order-execution code in this repo**, a hard non-negotiable
until the owner explicitly lifts it.

## Start here — the docs, in reading order

| Doc | What it answers |
|---|---|
| `OVERVIEW.md` | What this is, and the non-negotiables that never change. |
| `ARCHITECTURE.md` | How it runs — the 8 departments and the one seam per department. |
| `MODULES.md` | The component index — every file that matters + its specifics. |
| `DECISIONS.md` | Why each call was made (numbered, one row per decision). |
| `HANDOVER.md` | Current build status + how to pick the project up cold. |
| `CRON_SETUP.md` | Every scheduled job, on the VM and the Mac. |
| `DATA_CONTRACT.md` | The JSON shapes the frontend reads. |
| `docs/DOC_GUIDE.md` | Where to record new work without creating drift. |

## How it runs, in one line

The engine runs 24/7 on a GCP VM — DhanHQ-native market data, ~20 IST cron jobs,
a Cloudflare-tunnelled API gateway plus a Discord bot. The Mac handles
development, local-LLM work, and the NSE-crawling analysis desk. All state is
local and file-based under `data/` (git-ignored, no cloud DB). Full picture:
`ARCHITECTURE.md` + `CRON_SETUP.md`.

Repo: https://github.com/Aditya-Gupta-Data/alpha_trading

> **History:** this project began as a Phase-1 email alerter over yfinance. That
> origin lives in `git log` and `DECISIONS.md` #1–#22; the system has since moved
> to DhanHQ data, defined-risk options spreads, a SQLite Brain Map learning loop,
> and cloud execution. Trust the docs above over any older snapshot of this file.

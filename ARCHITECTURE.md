# ARCHITECTURE.md — The Department Map

**Read this first, before any file.** The system is organized as **7
departments**. Each department has ONE **Manager** — the single file/seam you
approach to change how that department behaves. You should never have to dig
through 50 files: find the department, go to its manager.

- **Why** behind each choice → `DECISIONS.md` (numbered).
- **Per-file** one-liners → `MODULES.md` (grouped by these same departments).
- **The rules** the code may never break → `OVERVIEW.md`.

Written for the strategic brain, not the compiler: every department below says,
in plain English, what it does, what goes in, what comes out, and the ONE place
to change it. Current as of `dbd531f` (2026-07-15), suite 1006 green.

---

## The whole system in one breath

```
   ┌── 1. DATA ─────────┐   market quotes, chains, news, deals, flows
   │  come IN here      │   → cleaned, archived
   └─────────┬──────────┘
             ▼
   ┌── 2. DECISION ─────┐   "should we open a spread, and which one?"
   │  the live engine   │   → a PENDING proposal
   └─────────┬──────────┘
             ▼
   ┌── 3. RISK & CAPITAL┐   "are we allowed? size it. when do we exit?"
   │  the gatekeeper    │   → approved / blocked; exits & settlement
   └─────────┬──────────┘
             ▼
   ┌── 4. MEMORY ───────┐   every trade + everything learned is recorded
   │  the ledger+brain  │   → journal, knowledge graph, tuned weights
   └─────────┬──────────┘
             ▼
   ┌── 5. VALIDATION ───┐   "does this pattern REALLY have an edge?"
   │  the proving court │   → patterns earn (or lose) authority
   └─────────┬──────────┘
             ▼
   ┌── 6. REPORTING ────┐   tells the human what happened & what's at risk
   │  the announcer     │   → Discord cards, CLIs, weekly digests
   └─────────┬──────────┘
             ▼
   ┌── 7. INTERFACES ───┐   how a human (you) sees it and taps approve/reject
   │  the front doors   │   → dashboard, Discord buttons, gateway
   └────────────────────┘
```

Rule of the whole design (**decision #63, the composition law**): only ONE
department (Decision) proposes, and only Risk can block. Every other layer may
only *annotate* — state facts, never silently change a trade. Authority is
*earned* through Validation, never hand-wired.

---

## Department 1 — DATA (market data in)

**Manager:** `src/dhan_guard.py` (`SafeDhanClient`) — the one hardened door to
all market data. Token lives behind `src/token_provider.py` (the single token
seam). Nothing else constructs a Dhan client.

**What it does (plain English):** brings the outside market into the system —
live prices, daily history, and option chains (with per-strike Greeks) — and
does it *safely*: it classifies failures (auth vs data outage), retries once on
a rate-limit, and voids stale quotes. Separately, the `ingestion/` clerks
capture end-of-day data that can never be re-bought later (option chains, bulk
deals, FII/DII flows, earnings dates, macro, news) into a `lake/` archive.

**Inputs:** DhanHQ Data API (read-only), NSE end-of-day reports, Google-News RSS.
**Outputs:** clean quotes/chains on demand; `data/market_snapshot.json` (the
engine's published marks — everyone else READS this so the live loop stays the
*single* Dhan consumer); dated archives under `data/lake/`.

**To change data handling, go to:** `dhan_guard` for live fetches;
`market_snapshot` for the shared marks; the specific `ingestion/<x>.py` clerk
for an archive feed.

---

## Department 2 — DECISION (the live trading engine)

**Manager:** `src/options_proposer.py` (`run_headless`) — the one place a spread
proposal is born, enriched, and journaled. Composed each day by
`src/master_scheduler.py`; driven cycle-to-cycle by `src/market_loop.py`.

**What it does (plain English):** during market hours it reads the trend (SMA
cross + RSI) and India VIX, decides a bullish / bearish / neutral view, and
builds the matching defined-risk spread (bull-call / bear-put / iron-condor) via
`src/strategy.py`. It then attaches *context annotations* to the proposal —
Book Context (#73: what we already hold and why), Memory (linked past patterns),
Skeptic, Alignment — and writes the proposal to the journal as PENDING. **The
one decision seam is `decide_pending()`** — terminal, Discord button, and
auto-approve all converge there; nothing approves a trade any other way.

**Inputs:** trend/VIX read, an option chain, the current book.
**Outputs:** a PENDING_APPROVAL journal entry + a rich Discord proposal card.

**To change what/how we propose, go to:** `options_proposer` (pipeline +
`decide_pending`), `strategy` (the leg math), `trade_planner` (view→structure
routing). Annotations are their own modules but only *decorate* here.

---

## Department 3 — RISK & CAPITAL (the gatekeeper)

**Manager (entry side):** `src/portfolio_manager.py` — the capital pool, margin
locks, and the 10% drawdown halt. **Manager (exit side):**
`src/plan_tracker.py` — THE one settlement path (no other code closes a trade).

**What it does (plain English):** stands between a proposal and the book. On the
way IN: `exposure_gate` (#68) blocks a duplicate (one spread per
underlying+direction), then the margin gate checks the capital pool can afford
it. While positions are OPEN: `portfolio_greeks` (#71) watches the *whole book's*
net Vega/Delta against equity budgets and warns if ten "neutral" condors have
quietly become one big volatility bet. On the way OUT: `plan_tracker` resolves
every trade against real prices — take profit at 65%, never hold into the last
2 days to expiry, close as one atomic basket, settle the cash once.

**Inputs:** a proposal (entry); open positions + live/EOD prices (exit).
**Outputs:** allowed/blocked verdict + margin lock; resolved outcomes with P&L;
book-level Greek advisories.

**To change risk behavior, go to:** `exposure_gate` (duplicates),
`portfolio_manager` (margin/drawdown), `portfolio_greeks` (book budgets),
`plan_tracker` (exits/settlement). Exits are advisory-to-the-human by rule
(#41/#11) except the one sanctioned intraday square-off (#69).

---

## Department 4 — MEMORY & LEARNING (the ledger + brain)

**Manager (truth):** `src/journal.py` (`data/journal.jsonl` — the source of
truth for every decision). **Manager (learning):** `src/brain_map.py`
(`data/brain_map.db` — everything the system has learned). **Nightly
orchestrator:** `src/sleep_phase.py`.

**What it does (plain English):** remembers. The journal is the immutable record
of every trade and why it was taken. The brain map is the knowledge store —
events, outcomes, a causal knowledge graph, regime tags, the daily market
frame, and evidence snapshots — with one iron rule: **losses are never deleted**
(deleting losers would fake every win-rate). Each night the sleep phase
consolidates memory, applies decay to stale links (losses exempt), and folds the
day's context. `src/tuner.py` is the *only* sanctioned way learned weights
change; `src/book_context.py` reads the journal to answer "what do we hold and
why" at any hour.

**Inputs:** resolved outcomes, daily market context, deal/flow history.
**Outputs:** the trade ledger; queryable pattern memory; tuned weights fed back
into the forecast.

**To change memory/learning, go to:** `journal` (the record), `brain_map`
(the store + queries), `sleep_phase` (nightly tasks), `tuner` (weight learning).

---

## Department 5 — VALIDATION HARNESS (the proving court)

**Manager:** `src/validation/registry.py` — the pattern lifecycle
(CANDIDATE → TRIAL → VALIDATED → LIVE_ADVISORY → QUARANTINED/DEAD). **The one
statistics rulebook:** `src/validation/stat_gates.py`.

**What it does (plain English):** this is what keeps the system honest and stops
it fooling itself. Every mined pattern is registered with a frozen definition,
then must *earn* its way up: tested out-of-sample on data it never saw
(`trial`, walk-forward with an embargo), watched for decay after going live
(`monitor`, auto-quarantines a bleeding pattern), and measured against
information-free decoys (`placebo`, the false-discovery meter). The
`discovery/` miners propose candidates; `shadow_runner` fires them on live
trades without touching real money; `digest` reports the weekly state. Nothing
gets authority to influence a real decision without passing through here.

**Inputs:** resolved outcomes + daily context (to mine and test on).
**Outputs:** patterns with a governed status; the honest win-rates and
false-discovery rate; `auto:` tags the miners then exclude.

**To change validation, go to:** `registry` (lifecycle/authority),
`stat_gates` (the thresholds — no miner defines its own), `trial`/`monitor`/
`placebo` (the proving mechanics), `discovery/` (the miners).

---

## Department 6 — REPORTING & ADVISORY (the announcer)

**Manager:** `src/notifier.py` (`fire_broadcast`) — the ONE door to Discord.
Every card, from every department, leaves through here.

**What it does (plain English):** tells you what's happening without you reading
code or logs. Scheduled read-only cards: `portfolio_report` (every 2h during
market hours), `eod_summary` (end of day), `performance` (#72, weekly
Sharpe/Sortino/drawdown over the real track record), `ops_monitor` (nightly
health + job heartbeats), `validation/digest` (weekly harness state). On-demand
CLIs: `explain <id>` (reconstruct any one trade end-to-end), `book_context`
(the whole book with reasons), `view_positions`, `graph_viz`.

**Inputs:** the journal, brain map, and live marks (read-only).
**Outputs:** Discord cards + terminal reports. Never places or changes a trade.

**To change reporting, go to:** `notifier` (the delivery mechanism), or the
specific report module for its content.

---

## Department 7 — INTERFACES (the front doors)

**Manager:** `src/api_server.py` — the strict fail-closed gateway (every request
needs the API key). It mounts the unified `src/api.py` app.

**What it does (plain English):** how a human sees the system and taps a
decision. The dashboard (served through a Cloudflare Tunnel) and the Discord bot
buttons both come in through the gateway, which forwards approve/reject to the
same `decide_pending()` seam Department 2 owns — so the button and the terminal
can never disagree. `APPROVE_REAL` is refused at the API layer (403): paper
only, structurally.

**Inputs:** authenticated HTTP (dashboard, Discord actions).
**Outputs:** read views of the engine; paper approve/reject decisions routed to
the one decision seam.

**To change interfaces, go to:** `api_server` (auth/gateway), `api` (endpoints),
`discord_bot` (bot commands). None of them duplicate engine logic — they call it.

---

## How a single trade flows through all 7 (the canonical path)

1. **DATA** serves a fresh option chain + trend read.
2. **DECISION** builds a bear-put spread, annotates it with book context, writes
   it PENDING, fires the proposal card.
3. **RISK** checks the exposure gate + margin, approves it (auto or human tap),
   locks margin; later `plan_tracker` takes profit at 65% and settles the cash.
4. **MEMORY** records the outcome + a post-mortem; the tuner learns from it.
5. **VALIDATION** lets any registered pattern that fired shadow-score this
   outcome, out-of-sample.
6. **REPORTING** shows it on the 2h card + the weekly performance/digest.
7. **INTERFACES** is where you saw the proposal and tapped approve.

---

## Infrastructure (where the departments physically run)

- **The VM (`alpha-trading-vm`, GCP) is the sole live engine.** It runs the
  decision loop, risk gates, reporting cards, and the gateway via `systemd`
  services + a cron block (`scripts/setup_cron.sh` — 15 jobs; the token renews
  once at 07:00 IST). It mints its Dhan token from GCP Secret Manager. See
  `docs/gcp_vm_deployment` context / `HANDOVER.md` for the deploy checklist.
- **The Mac** runs only what needs a local Ollama or interactive state: the
  evolution agent and edge miner. It is NOT the engine; closing it doesn't stop
  trading.
- **State** is file-based under `data/` (git-ignored): `journal.jsonl`,
  `brain_map.db`, `market_snapshot.json`, `brain_weights.json`, plus the
  `lake/`. Config (versioned, non-secret) in `config.json` + `config/`. Secrets
  in `.env` (git-ignored). The `lovable-frontend/` UI lives only on the
  `lovable-ui` branch, never on `main`.

## Structural non-negotiables (enforced by this architecture)

- No broker/order-placement import exists anywhere in `src/` — paper only.
- `dhan_client` calls Dhan's *data* endpoints only, never order/fund endpoints.
- One decision seam (`decide_pending`), one settlement path (`plan_tracker`),
  one Discord door (`fire_broadcast`), one market-data door (`dhan_guard`).
- Losses are append-only in the ledger; only Validation grants authority;
  every non-Decision layer is annotate-only until it earns more (#63).

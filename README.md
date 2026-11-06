# alpha_trading — a proprietary systematic trading desk

An autonomous, **paper-money** systematic trading research desk for Indian
markets (NSE). It runs itself on a cloud VM: ingests market and macro data,
proposes trades, gates them through risk, journals every outcome, and grades
its own predictions against what actually happened.

**Nothing here touches real money.** There is no broker order path in this
repository — only a market-data connection. Every "trade" is paper.

---

## Read this in order

| If you want to… | Read |
|---|---|
| Understand the system to change it | **[ARCHITECTURE.md](ARCHITECTURE.md)** — the 8-department map + data flow. **Start here.** |
| Know what a specific file does | [MODULES.md](MODULES.md) — one line per file, grouped by department |
| Know *why* something is the way it is | [DECISIONS.md](DECISIONS.md) — 183 numbered decisions (2026-11-06), append-only |
| Know the paper accounts and what each tests | [docs/ACCOUNTS.md](docs/ACCOUNTS.md) — the seven books side by side |
| Know what is on branches but **not in `main` yet** | [docs/BRANCHES.md](docs/BRANCHES.md) — regenerated at every session wrap |
| Find any doc in `docs/` and whether it is still true | [docs/README.md](docs/README.md) — the index, with a status per file |
| Pick up cold / know what's broken | [HANDOVER.md](HANDOVER.md) — current state + the PENDING ISSUES backlog; older blocks in [docs/handover_archive.md](docs/handover_archive.md) |
| Know what runs when, and where | [CRON_SETUP.md](CRON_SETUP.md) — the VM jobs, the Mini PC blocks, the Mac |
| See how the project evolved | [PROJECT_TIMELINE.md](PROJECT_TIMELINE.md) — day by day, from git |
| Know what is coming next | [docs/ARCHITECTURE_ROADMAP.md](docs/ARCHITECTURE_ROADMAP.md) — the owner-fixed build order (10-28); [ROADMAP.md](ROADMAP.md) is the older index (09-12) |
| See the whole desk on one page, and its weak links | [docs/SYSTEM_BLUEPRINT.md](docs/SYSTEM_BLUEPRINT.md) — the 2026-09-12 CTO blueprint (dated: before the Mini PC, the learner, GEX and events; ARCHITECTURE.md's "Current state" section covers those) |
| Know the rules code may not break | [CLAUDE.md](CLAUDE.md) §7 — house conventions (`OVERVIEW.md` retired to [docs/archive_v0/](docs/archive_v0/) 2026-08-11) |
| **Work on this repo as an AI agent** | **[CLAUDE.md](CLAUDE.md)** — standing rules, loaded automatically |

## What it actually does

Two desks trade paper capital across seven paper accounts, and two research
engines (the Macro Regime Engine and the knowledge graph) study the market
they trade in. Neither research engine can open a position.

**The options desk** proposes NSE index and stock option spreads during market
hours, sized and gated by margin, exposure, earnings and volatility-stress
rules. Every entry and exit is an order ticket that a paper venue fills with
slippage. The same signal is judged separately by several accounts, each built
to test one question: small size, capital rotation, live bid/ask pricing,
dealer-gamma steering, and "take every setup" (the Shadow Learner). A separate
event engine trades long options on policy news in its own book. See
[docs/ACCOUNTS.md](docs/ACCOUNTS.md).

**The equity desk** runs a "darling" book: a quality screen over NSE cash
equities, graded on a 7-tier lifecycle, funded out of a shared firm treasury
that routes capital between the two desks based on measured performance.

**The Macro Regime Engine** (Department 8) is the newest and largest research
layer. It asks: *what kind of market are we in, and what has historically
worked in this kind of market?* It answers by fingerprinting the current macro
environment against a catalog of historical episodes using dynamic time
warping, then measuring what each declared playbook actually returned —
forward, out of sample, on an immutable ledger.

Critically, **the Macro Engine has zero execution authority.** It cannot open a
position. It writes opinions to an append-only ledger and waits to be proven
right or wrong over a 60-session clock. That gate is deliberate, and it is
enforced by Department 5 (Validation) rather than by convention.

## The core discipline: honest measurement

Most of this codebase exists to stop us fooling ourselves. The load-bearing
ideas:

- **Shadow before capital.** Every new signal runs as a paper shadow and must
  clear statistical gates before it can size a position.
- **Forward, not backward.** A backtest is a hypothesis. The engine grades its
  *declarations* against what happened after they were made
  (`logs/macro_regime_declarations.jsonl` → `logs/macro_strategy_scores.jsonl`),
  which is why declarations are immutable and timestamped.
- **Placebos run alongside.** Deliberately meaningless control strategies are
  scored on the same rulebook. If the placebos rank alongside the real signals,
  we have no edge — and the system says so out loud.
- **Losses are permanent.** The outcomes ledger is append-only. A losing result
  can never be deleted or overwritten (survivorship-bias guard).
- **Abstention is a valid answer.** Missing data yields `None`, never a
  fabricated number. "We don't know" appears all over this codebase.
- **Fail open, never crash the clock.** Every nightly stage is caught
  independently. A dead data source produces an honest gap, not a dead cron.

## Where it runs

- **The VM** (`alpha-trading-vm`, GCP, Debian, IST clock) is the only
  execution node (decision #146): cron jobs 1–38 in `scripts/setup_cron.sh`
  plus the systemd services. It holds only a short-lived market-data token,
  never the account credentials that could mint one.
- **The Mini PC** (home, `minipc1`) is the analytics hub on its own database:
  research schedules, the LLM jobs, the knowledge graph, the alt-data scrapers
  and the event engine's card builder. It refuses to run the live loop.
- **The Oracle dashboard box** serves the read-only React desk and Streamlit
  behind a Cloudflare tunnel, fed by the VM's mirror push every 15 minutes.
- **The Mac** is for development and the chat agent. Trading never needs it.

Both schedules, and how to reinstall them, are in [CRON_SETUP.md](CRON_SETUP.md).

## Running it locally

```bash
pip install -r requirements.txt
```

The full test suite is the fastest way to confirm a working checkout:

```bash
python3 -m pytest -q
```

**3,515 tests (2026-11-06) in about a minute, fully hermetic** — no network, no live token, no
production data. If a test you write is slow, it is almost certainly reaching a
real external system; see the testing philosophy in
[ARCHITECTURE.md](ARCHITECTURE.md).

Useful entry points:

```bash
python3 -m src.analysis.macro_nightly     # the nightly macro clock tick
python3 -m src.suggest                    # daily suggestions digest
python3 -m src.ops_monitor                # health sweep -> Discord card
python3 -m src.bug_ledger --report        # what the machine thinks is broken
```

## Ending a session

```bash
bash scripts/wrap_session.sh
```

Appends the day's commits to [PROJECT_TIMELINE.md](PROJECT_TIMELINE.md),
regenerates [docs/BRANCHES.md](docs/BRANCHES.md), runs the suite as a gate,
and commits those two files. Documentation here is
maintained continuously rather than in retroactive sweeps — see the Continuous
Context protocol at the top of [HANDOVER.md](HANDOVER.md).

## Repository layout

```
src/                 215 modules (not every one is on a cron; see MODULES.md)
  analysis/          Dept 8 — macro regime, valuation, darling research
  ingestion/         Dept 1 — the data clerks (lake writers)
  validation/        Dept 5 — the proving court (gates, trials, placebos)
  discovery/         pattern miners (propose candidates only)
  knowledge_graph/   GraphRAG, spreading activation, the placebo court
  execution/         the OMS, paper venue and live pricer
  event_engine/      policy events → cards → the PAPER_EVENTS book
tests/               263 files, 3,515 tests — the live suite
research_archive/    parked code, kept for reuse, on NO execution path
docs/                46 specs, plans and audits — indexed in docs/README.md
data/                the lake + artifacts (mostly gitignored)
logs/                append-only ledgers + job logs
```

`research_archive/` is deliberately not imported by anything. It exists so we
never rewrite logic from scratch — see its own README for the manifest.

## Status

- **Live** on the VM since 2026-07-08; autonomous paper trading since 07-21.
- **Capital (paper):** seven accounts: PAPER_10L (₹10L contributed), four ₹2L
  experiment books, the ₹2L Shadow Learner and the ₹10L PAPER_EVENTS book.
  Each risks 2% of its own equity per trade (#106).
- **The forward clock:** the Macro Engine needs a 60-session forward-scored
  track record before anything it says earns authority (target moved from
  Oct 1 to about Oct 13 by #86). The current verdict is in HANDOVER.md, not here.
- **Current state, open bugs and next steps:** the top of [HANDOVER.md](HANDOVER.md).
- **Posture:** proprietary desk, stealth.

One caveat worth stating plainly: the options simulator's historical P&L is
synthetic-chain inflated and is **not** an expected return. Treat it as proof
that the machinery works, never as a forecast.

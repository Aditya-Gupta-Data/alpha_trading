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
| Know *why* something is the way it is | [DECISIONS.md](DECISIONS.md) — 85 numbered decisions, append-only |
| Pick up cold / know what's broken | [HANDOVER.md](HANDOVER.md) — current state + the PENDING ISSUES backlog; older blocks in [docs/handover_archive.md](docs/handover_archive.md) |
| Know what runs when, and where | [CRON_SETUP.md](CRON_SETUP.md) — all 24 VM jobs + 3 Mac jobs |
| See how the project evolved | [PROJECT_TIMELINE.md](PROJECT_TIMELINE.md) — day by day, from git |
| Know what is coming next | [ROADMAP.md](ROADMAP.md) — the single index of future work |
| Know the rules code may not break | [CLAUDE.md](CLAUDE.md) §7 — house conventions (`OVERVIEW.md` retired to [docs/archive_v0/](docs/archive_v0/) 2026-08-11) |
| **Work on this repo as an AI agent** | **[CLAUDE.md](CLAUDE.md)** — standing rules, loaded automatically |

## What it actually does

Two desks trade paper capital, and one research engine studies the market
regime they trade in.

**The options desk** proposes NSE index option spreads during market hours,
sized and gated by margin, exposure and volatility-stress rules, then tracks
each position through to settlement.

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

- **The VM** (`alpha-trading-vm`, GCP, Debian, IST clock) is the engine: 24 cron
  jobs plus three systemd services. It holds only a short-lived market-data
  token — never the account credentials that could mint one.
- **The Mac** is analysis-only. It builds the heavy artifacts (bhavcopy lake,
  valuation, macro templates) and ships them to the VM. It also runs the
  NSE-crawling jobs, which must never run from the VM's IP.

Both schedules, and how to reinstall them, are in [CRON_SETUP.md](CRON_SETUP.md).

## Running it locally

```bash
pip install -r requirements.txt
```

The full test suite is the fastest way to confirm a working checkout:

```bash
python3 -m pytest -q
```

**1,589 tests in ~83 seconds, fully hermetic** — no network, no live token, no
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

Appends the day's commits to [PROJECT_TIMELINE.md](PROJECT_TIMELINE.md), runs
the suite as a gate, and commits that one file. Documentation here is
maintained continuously rather than in retroactive sweeps — see the Continuous
Context protocol at the top of [HANDOVER.md](HANDOVER.md).

## Repository layout

```
src/                 141 modules, all on a live execution path
  analysis/          Dept 8 — macro regime, valuation, darling research
  ingestion/         Dept 1 — the data clerks (lake writers)
  validation/        Dept 5 — the proving court (gates, trials, placebos)
  discovery/         pattern miners (propose candidates only)
  knowledge_graph/   entity affinity + causal edges
tests/               132 files, 1,589 tests — the live suite
research_archive/    parked code, kept for reuse, on NO execution path
docs/                23 specs and plans (the "why" behind the big builds)
data/                the lake + artifacts (mostly gitignored)
logs/                append-only ledgers + job logs
```

`research_archive/` is deliberately not imported by anything. It exists so we
never rewrite logic from scratch — see its own README for the manifest.

## Status

- **Live** on the VM since 2026-07-08; autonomous paper trading since 07-21.
- **Capital:** ₹2,00,000 clean-sheet paper pool, ₹10,000 hard risk cap per trade.
- **The October clock:** the Macro Engine needs a 60-session forward-scored
  track record before anything it says earns authority. Evaluated Oct 1.
- **Posture:** proprietary desk, stealth. No public surface.

One caveat worth stating plainly: the options simulator's historical P&L is
synthetic-chain inflated and is **not** an expected return. Treat it as proof
that the machinery works, never as a forecast.

# PROJECT_TIMELINE.md — how this system was built

**284 commits, 22 active days, 2026-06-24 → 2026-07-25.**

Every entry below is derived from git history — commit subjects, bodies and
dates. Where the record is ambiguous, this file says so rather than guessing.
This is the *narrative* record; the *reasoning* record is
[DECISIONS.md](DECISIONS.md) (numbered, append-only), and the *incident* record
is [docs/observation_week_ledger.md](docs/observation_week_ledger.md).

A note on dates: commit author-date and merge order do not always agree.
Several 07-10 commits landed on main on 07-11 via PRs #1/#2, and the DH-905
throttle fix (authored 07-22) only reached main on 07-25. Counts below use
author date.

---

## Act I — a personal alerting tool (June)

### 2026-06-24 · 10 commits · *Project birth*
Initial scaffold, a design doc, and a read-only web dashboard shipped as the
"Phase 1 milestone". A duplicate web app was retired the same day and a master
roadmap seeded.
> `d08e5a5` Initial scaffold · `c0ec652` read-only web dashboard (Phase 1 milestone)

**Then nothing for ten days.** Git offers no explanation for the 06-25 → 07-04
gap, so none is claimed here.

---

## Act II — from tool to engine (early July)

### 2026-07-05 · 5 commits · *The engine appears*
"Phase 4 complete" lands the functional Python trading engine: alerting,
suggestions, paper trading, trade plans, plan tracking, news sentiment and a
rule-based forecast loop. The remote dashboard is merged in and the decoupled
branch strategy written down (main = engine, lovable-ui = frontend).
> `e88f92b` Phase 4 complete — functional Python trading engine backend

### 2026-07-06 · 20 commits · 🏁 *Real market data + the Brain Map*
Two milestones in one day. A unified local FastAPI REST layer (`src/api.py`),
and the market-data source swapped from yfinance to the **DhanHQ real-time Data
API**. Then Phase 6 "Brain Map" is built end to end: SQLite engine → memory
ingestion + journal short_ids → wired into `forecast.py` → an automated
post-mortem analyst closing the feedback loop.
> `f1243aa` migrate yfinance → DhanHQ · `61f1c3b` Brain Map feedback loop closed

### 2026-07-07 · 5 commits · *Reasoning and replay*
Graph reasoning (Phase 6C), Discord approval buttons, a Random-Forest "Skeptic
Agent", and the Time-Travel Simulator for historical replay through the real
pipeline.
> `ba1fa2d` Phase 7 Time-Travel Simulator

### 2026-07-08 · 19 commits · 🏁 **THE VM BECOMES THE ENGINE**
The heaviest subsystem-per-commit day to date: Phases 6E–6J in sequence
(temporal signal decay, execution bridge, capital/margin manager, market-hour
adapter, options planner, portfolio realism) plus the Phase 7A master
scheduler. DhanHQ V2 headless PIN+TOTP auth replaces the deprecated renewal,
with credentials moved into GCP Secret Manager. The day ends with migration
complete — **the Mac is no longer required during market hours.**
> `2e8d97f` The VM is the engine · `815db44` Phase 7A Master Scheduler

---

## Act III — observation week: it meets reality (07-09 → 07-14)

### 2026-07-09 · 21 commits · *First live-operation day*
Three production hotfixes, including two response-shape double-nesting bugs
that had been **silently blocking every single proposal**. The observation-week
ledger is started — the discipline of recording verified facts about failures
begins here. Evening brings a large planning burst (~11 doc commits).
> `5fe5647` get_expiry_list double-nesting silently blocked EVERY proposal

### 2026-07-10 · 33 commits · 🏁 *Busiest day; the weekend deploy*
Eight numbered "local scratchpad" phases built explicitly **not deployed**
during observation week (self-healing token, cooldown persistence, MFE/MAE
expectancy, auto-approve gate, threat mitigation, event-driven dashboard,
semantic resonance, single-Dhan-consumer snapshot), then a refinement pass
fixing all 10 review findings, then the deploy itself. Separately,
`HOLY_GRAIL_PLAN.md` opens a new track: data lake, deals backfill, evidence
substrate, and the loss-permanence invariant.
> `374bead` executed weekend deploy (VM on bf9dc77) · `283cde3` HOLY_GRAIL_PLAN

### 2026-07-11 · 31 commits · 🏁 **THE PROVING HARNESS**
Phases 2→5 completed and merged in a single day. Phase 2 closes a timelock
harness and **two real lookahead holes**. Phase 3 lands the composition law.
Phase 4 — the proving harness — completes: statistical gates, pattern registry,
walk-forward trials, drift monitor, **placebo false-discovery meter**, weekly
digest. Phase 5 ships the pattern miners. A 3-year NSE deals backfill completes
(75,600 deals).
> `9429f4b` placebo FDR meter + weekly digest — Phase 4 COMPLETE

### 2026-07-12 · 2 commits · *Ops*
NSE 403s fixed (each fetcher needs its own endpoint-owning Referer), and a
by-design VM false alarm silenced.

### 2026-07-13 · 3 commits · *Risk containment after an incident*
A 9-spread correlated pileup was observed live. An exposure gate (one open
spread per underlying+direction, enforced *before* the margin gate) and a
trend-flip exit advisory land the same day.
> `2823367` exposure gate + trend-flip exit advisory (decision #68)

### 2026-07-14 · 4 commits · *Exit mechanics*
Intraday profit-take square-off priced on real option-chain quotes, with a
model-vs-market divergence guard, plus a Discord `/pnl` command.

---

## Act IV — realism, research, and a second desk (07-15 → 07-20)

### 2026-07-15 · 20 commits · *Realism and the department map*
Paper fills now cross the bid-ask (no more free entries). Stale NSE lot sizes
corrected to the Jan-2026 SEBI revision. Portfolio-level Greeks advisory,
risk-adjusted track-record metrics, and an official-RSS ingestion pipeline.
**ARCHITECTURE.md is rewritten as the 7-Department Manager map** — the
organizing idea the codebase still uses.
> `412e57e` honest paper fills · `cfdeeaf` ARCHITECTURE as the Department map

### 2026-07-16 · 15 commits · *Anti-false-discovery hardening*
A noise-injection regression suite and block-permuted "Noise v2" run through
the real simulator — the end-to-end false-discovery check. Phase-5 miners go
on a gated nightly cron. Universe expands to 18 verified NIFTY-50 cash
equities.
> `8e4857a` noise-injection suite — the end-to-end false-discovery regression

### 2026-07-17 · 7 commits · 🏁 *Department 8 is created*
A master deployment of the accumulated smart-money and regime-filter work
(suite 1,088 green). A Shadow Equity Engine telemetry frame and the Daily CEO
Brief land. Review #2 rulings create **Department 8 (Analysis)**.
> `6d89eb4` Master deployment: Smart Money radar + regime filters

### 2026-07-18 · 14 commits · *The research department*
An annual-report forensic pipeline is built, hardened against benchmarks, and
given a Gemini synthesis layer. A citation-grading bug (Issue 17) is fixed and
its blast radius honestly logged as Issues 18–20. On the risk side, entry-time
VIX-stress margin and a composed halt list.

### 2026-07-19 · 8 commits · 🏁 *The Darling Pipeline*
Equity research is built in two phases the same day: NSE results clerk + quant
screen + queue, then a dynamic pricer and equity halt stack — joined by a
bhavcopy clerk for daily EOD bars and a 1–100 Valuation Normalization Engine.
> `7f9c7f5` Darling Pipeline Phase 1 · `68709f6` Valuation Normalization Engine

### 2026-07-20 · 17 commits · 🏁 **CAPITAL ARCHITECTURE**
The largest single-day escalation in the repo. RIPE grading is scrapped for a
7-tier lifecycle. Then, in sequence on the same night: **the equity desk goes
live on paper capital** (#79), **a firm treasury routes capital dynamically
between the two desks** (#80), and **adaptive sizing** driven by trade
autopsies (#81).
> `e3196e4` equity desk live · `1bd8f58` firm treasury · `2a69f75` adaptive sizing

---

## Act V — autonomy and the macro brain (07-21 → 07-24)

### 2026-07-21 · 5 commits · 🏁 **THE AUTONOMOUS RUN IS ARMED**
The equity desk goes VM-native in one firm database: equity notional locks pass
through the same door as options margin, one atomically-updated treasury row,
and the Mac is reduced to analysis-only artifact shipping. A clean-sheet
₹2,00,000 pool, a ₹10,000 per-trade hard cap, a crash pager, and a 5-per-day
Discord budget. Suite 1,460 green. An internal bug ledger and the "Thursday
Protocol" land the same day.
> `0c1cb56` the autonomous run (#83/#84) · `6a6da53` bug ledger + Thursday Protocol

### 2026-07-22 · 5 commits · *Triage, then a pivot*
Thursday-Protocol triage of the autonomous run: 55 logged items, **one real
code bug**. A host-wide Dhan throttle is written to kill DH-905 rate-limit
bursts (it would not reach main until 07-25). Then the strategic turn: a
Brain-Map MCP server prototype, the cycle-hunter plan, the Speed & Scale
workflow protocol, and a planning-only Macro Regime Engine blueprint.

### 2026-07-23 · 22 commits · 🏁 **THE MACRO REGIME ENGINE**
Built essentially end to end in one multi-agent sprint. M1 macro lake (FRED
globals + NSE indices), the one featurizer and episode catalog, **M2 banded
multivariate DTW fingerprinting**, then core-channel clustering with slow-burn
horizons, M3 playbooks and the M4 declaration tracker. On top: `macro_nightly`
as the VM heartbeat **starting the 60-session scoring clock**, the AD-1→AD-4
unsupervised auto-discovery and significance layers, and a fingerprint cache
that took `declare()` from 1,840s to light enough for an e2-micro.

The result the team did not script: with the catalog at 20 shocks and 7
slow-burns, **archetypes grouped by data rather than by human labels** —
Ukraine clustered with the taper tantrum. Docs record the "Stealth Mode" pivot
the same day.
> `b6932ed` M2 fingerprint engine · `0cc9c9f` macro_nightly starts the clock

### 2026-07-24 · 14 commits · 🏁 **STAGE B — THE FORWARD CLOCK**
Two workstreams. *Stage A* hardens pre-2019 sector history with an
out-of-sample tracking-error validation protocol, and falls back to local CSVs
after Tata Motors proved unfetchable. *Stage B* builds forward scoring: the
SB-1 scorer core, SB-2 wired into `macro_nightly` as a fail-open fourth stage,
and SB-3/SB-4 the forward scoreboard and graduation rules — the machinery that
decides whether an in-sample "PREFER" actually confirmed live.
> `b641f92` SB-1 scorer core · `1c7ae0c` SB-2 wired into macro_nightly

---

## Act VI — hygiene (07-25)

### 2026-07-25 · 5 commits · *The CTO session*
A four-phase hygiene sweep with no new trading logic.

**Phase 1 — the Great Purge.** An AST dependency trace from every real
entrypoint (24 VM crons, 3 Mac crons, 2 LaunchAgents, systemd services,
`.mcp.json`) classified all 152 `src/` files. 12 dead modules moved to
`research_archive/` with their tests. Two findings the trace surfaced: the
`macro_nightly` cron existed on the VM but was **missing from the installer**
(it would have vanished on the next re-run), and `decay_engine.apply_decay_sweep`
is scheduled by nothing — graph-edge decay has never run in production.

**Phase 2 — the heartbeat.** `macro_nightly` now fires one Discord health card
per run: `[🟢 FRED: OK | 🟢 Indices: OK | 🟢 Declare: OK | 🟢 Scorer: OK]`.

**Phase 3 — the speedup.** The suite went from **14m09s to 1m23s** with all
1,589 tests still passing. Three files were reaching real external systems: one
fired 84 live quote calls per test against the production watchlist, one slept
on the real rate-limit throttle, and one ran real Ollama inference. All three
were correctness bugs as much as speed bugs — what CI ran depended on the host
it ran on.

**Phase 4 — this documentation.**
> `1003611` the Great Purge · `331b42c` heartbeat card · `48e15a8` 10x speedup

---

---

## Daily log (auto-generated)

Entries below are written by `scripts/wrap_session.sh` at the end of each
working session — the raw record. The hand-written Acts above are the
narrative, and the script never touches them: it splits this file at the
marker and only ever edits what is below it. Re-running on the same day
regenerates that day's entry rather than duplicating it.

<!-- WRAP_SESSION:INSERT_BELOW -->

### 2026-07-30 · 10 commits · 23 files touched

- `d8592e0` docs(decisions): #86 Stage-B timeline — the standard does not slip, the calendar does
- `ccf1b92` feat(ops): Edge-to-Cloud handover queue + Stage-B clock tracker
- `c72ee21` fix(graph): structural affinity decays on a 1-year clock, not 14 days
- `cc67590` docs: 07-30 close — Task J/K verified live, edge-decay lambda question logged
- `426fc34` feat(sleep_phase): Task K — wire knowledge-graph edge decay (owner-approved)
- `b356b8d` fix(validation): H4 shadow must date fires by the BAR, not wall-clock today
- `2962b7f` feat(validation): H4 pyramid-continuation shadow — lb-10 graduated, owner ruled shadow-only
- `72f9673` docs: HANDOVER — 07-30 deploy of the suggest DH-905 fix to the VM
- `8547473` fix(suggest): recover early-run DH-905 skips with an end-of-run retry pass
- `b4930c6` docs: 07-30 status check — push-state correction, intraday-capture resolution, zombie VM removal


### 2026-07-27 · 10 commits · 36 files touched

- `a4575a5` docs: session close 2026-07-27 — HANDOVER, ledger, trade-book export
- `7e0d635` fix(validation): H4 loud-abort on data failure + spread-tuner design + floor 5->10
- `9d8c13d` feat(validation): H4 simulator experiment harness (pyramid vs one-and-done)
- `614bcf8` docs: HANDOVER — Intelligence & Autonomy trio complete (Directives 1-3)
- `b4e0437` feat(risk): Opportunity Cost Tracking — blocked trades into the shadow ledger (Directive 1)
- `66c60a3` feat(reporting): CEO-View Discord — plain English + Morning Brief (Directive 2)
- `f15182e` feat(risk): Walkaway Protocol — 🔴 SYSTEM PAUSED layer for the risk-of-ruin halt (Directive 3)
- `d890129` docs: Intelligence & Autonomy strategic backlog — 3 owner directives (no new engines)
- `abcfda2` docs: 07-27 session wrap — deploy-gap close, two reporting fixes logged
- `170aa21` fix(reporting): two honesty fixes from the 07-27 CEO brief


### 2026-07-25 · 10 commits · 55 files touched

- `34d08bb` feat(docs): Continuous Context protocol — wrap_session.sh + CLAUDE.md agent rules
- `90339d0` chore: drop the back-dated wrap_session test entry
- `d687dce` docs: restore the wrap_session insertion marker
- `82b83a4` Revert "chore: EOD doc sync — 2026-07-25 (6 commits)"
- `e9897dd` docs: Phase-4 documentation sync — the source of truth, LLM-optimized
- `c271068` docs: log Phase-3 test streamlining in the observation ledger
- `48e15a8` perf(tests): Phase-3 streamlining — 14m09s suite to 1m23s (10x)
- `331b42c` feat(observability): Phase-2 nightly macro heartbeat card
- `1003611` chore(hygiene): Phase-1 Great Purge — research_archive/, cron drift closed, DH-905 landed
- `ad9d586` fix(dhan): host-wide throttle to kill DH-905 rate-limit bursts


---

## What the shape of this history says

Three patterns are visible in the record and worth preserving:

1. **Incidents become invariants.** The 07-13 spread pileup became the exposure
   gate. The 07-09 double-nesting bugs became response-shape tests. The 07-22
   rate-limit bursts became a host-wide throttle. Nothing is fixed twice.
2. **The safety machinery was built before the capital.** The proving harness
   and placebo meter (07-11) predate the equity desk going live (07-20) by nine
   days. That ordering was deliberate.
3. **The system is designed to disappoint us honestly.** The first real Strategy
   Registry build returned "no edge at 6–8 analogs yet", with placebos ranking
   alongside the seeds — and that verdict was shipped rather than tuned away.

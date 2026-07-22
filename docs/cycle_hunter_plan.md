# Cycle Hunter — teaching the brain map long-term cyclical patterns

> **Status: PLAN ONLY (written 2026-07-22, during the observation-week
> code freeze). No code exists for this yet. Build order begins ONLY
> after the Thursday Protocol bug-report review clears (owner directive
> 07-21).** Owner directive 07-22: "play out more trades in shadow and
> feed our brain map — I want our brain map identifying long term
> cyclical patterns," with up to ₹1L available for infra.

## Why this plan exists (the forward-only trap)

The brain map's event memory starts 2023-01-02 (1,242 events, 259
distinct dates). `candidate_patterns` and `pattern_audit` are EMPTY —
the pattern court built in Phase 5 is starving, not broken. Forward
shadow trading adds ~250 trading days of experience a year; a long-term
cycle (sector rotation, rate cycle, budget/festive seasonality,
election years) needs 10–20 years to show itself even three times.
Conclusion: **replay history backward to discover; shadow forward to
validate.** Neither arm alone is enough.

## What already exists (build FROM these seams, don't invent)

- `src/regime.py` — the as-of-date historical backfill pattern
  (bars-cache-driven, idempotent, never guesses). The Time Machine
  extends this idea to events/outcomes; it does not need new
  architecture.
- `src/sleep_phase.py` + `src/edge_miner.py` — causal-link writing over
  brain history (Mac-side Ollama, free).
- `src/discovery/nightly.py` — THE cron entry for the Phase-5 miners,
  triple-gated; feeds `candidate_patterns`.
- `src/evolution.py` — loss-cluster mining + double-backtest court;
  human gatekeeping.
- `docs/self_evolving_brain_map.md` — the locked DTW pattern-family
  spec, explicitly deferred until after observation week. This
  directive is its green light, AFTER Thursday.
- `validation/registry` + `stat_gates` — the one rulebook that decides
  whether a discovered "cycle" is real or overfitting. Nothing skips it.

## Data assets on hand

| Asset | Span | Note |
|---|---|---|
| lake/bhavcopy | 220 files (~1 trading year) | full-market NSE EOD |
| archive/ Kaggle NIFTY-50 | 2000 → 2021-04 | survivor-biased, research-only |
| brain_map events | 2023 → today | deep-deal backfill vintage |
| lake/financial_results | June-2026 quarters | XBRL, TCS-validated |
| lake/deals_census + raw_backfill | multi-year | big-money footprints |
| **GAP: 2021-04 → 2023-01** | — | fillable free from NSE archives |

## OPTION B ACTIVE (owner directive 2026-07-23): the Macro Regime Engine (docs/macro_regime_engine_spec.md) is prioritized OVER the landing page for the remaining Max window. M1→M4 build now; the landing page (G2 leg 2) slips behind them. Execution runs in Multi-Agent mode: PM/architect session (wiring, schemas, docs, integration, commits) + Opus worker (math/normalization) + Fable worker (fetchers/boilerplate/tests) — workers build isolated new files only, never commit; the PM session integrates behind the full gate.

## The build window (HARD constraint: Claude Max lapses Aug 8)

Owner 07-22: Max subscription ends **2026-08-08**, then **downgrades to
Pro (never zero)**. So Aug 8 is the end of HEAVY-build capacity, not of
dev capacity: Pro comfortably covers hotfixes, small modules paced one
per session, and ops questions — it does not cover multi-day agentic
builds. Rank of work: big multi-module builds land inside the Max
window; Pro-sized work can safely slip past it.

- **Thu Jul 24** — Thursday Protocol: bug ledger review + fixes (blocking).
- **Fri Jul 25–Sun 27** — Time Machine backfill built AND launched
  (continues unattended after); darlings re-screen fired.
- **Mon Jul 28–Wed 30** — miners pointed at deep history + cron-ified;
  equity shadow proposer built (shadow book widens on its own after).
- **Thu Jul 31–Sun Aug 3** — MCP server prototype (localhost, snapshot
  DB) + free-hosted landing page with waitlist.
- **Mon Aug 4–Wed 6** — hardening: suite green, MODULES.md/docs
  current, everything committed + deployed to VM.
- **Thu Aug 7–Fri 8** — buffer + **post-Claude runbook**: what runs on
  which cron, how to read the scoreboard, what never to touch.

After Aug 8 with zero renewal, the machine still: trades both desks,
backfills, mines nightly, accumulates FII/DII + shadow trades + the
waitlist, and reports on Discord. Does NOT fit by Aug 8 (needs a
future coder burst): Razorpay/payments productization, B2B onboarding,
the DTW self-evolving brain v2.

## The phases (strictly after Thursday's bug review)

1. **Phase A — Time Machine backfill.** Pull historical bhavcopy from
   NSE's free archives (target 2016 → today first; deepen later).
   Replay derived events/outcomes into `brain_map.db` with as-of-date
   honesty (the `regime.py` discipline: never guess, tag unknowable
   rows 'unknown'). Idempotent; runs off-hours on the VM; never touches
   the live loop (one-data-door rule holds).
2. **Phase B — feed the courts.** Point the existing miners at the
   deepened history. Success metric: `candidate_patterns` gets rows and
   `stat_gates` starts issuing verdicts. Cycle vocabularies to encode
   as event tags: budget day, festive window, expiry week, monsoon,
   election window, rate-decision window, FII flow streaks (the War
   Playbook already proves the crisis→sector encoding works).
3. **Phase C — widen the shadow book.** Build the equity shadow
   proposer (the parked F&O step 4 — owner un-parked it 07-22).
   Forward shadow trades become the out-of-sample validator for
   Phase B's discovered cycles.
4. **Phase D — productize.** Confirmed cycle tables become premium
   endpoints in the brain-MCP data product (facts and scores only,
   never buy/sell verbs — the SEBI posture).

## Budget (owner cap ₹1,00,000) — the PROOF-GATE rule (owner directive 07-22)

**Build aggressively at ₹0 until a spend gate opens. A gate opens only
when the stated proof exists and the value of the spend can be named in
numbers. No proof → no spend, no exceptions.** The entire free runway —
backfill, replay, miners, shadow proposer, darlings re-screen, MCP
prototype on localhost, landing page on free hosting — needs no money.

| Gate | Spend | Proof required BEFORE spending | Value it buys |
|---|---|---|---|
| G1 paid history (TrueData/GDF class) | ₹10–30k once | Free NSE archives fully ingested AND ≥1 named candidate pattern whose stat_gates verdict is blocked specifically by the 2021–2023 gap or F&O depth | Converts named blocked patterns into validated cycles = premium MCP endpoints + better desk advisories |
| G2 MCP hosting + domain + Razorpay | ~₹5k + ~₹1k/mo | Working localhost MCP server (≥8 tools over a snapshot DB) demoed end-to-end in Claude/ChatGPT AND ≥10 outsiders on a waitlist / saying they'd pay | First revenue; ~20 users × ₹299/mo covers ALL infra forever |
| G3 bigger/second VM | ₹2–5k/mo | Measured bottleneck in job logs: mining overruns its off-hours window, or MCP serving threatens live-loop latency | Named: N extra mining hours/night or user-facing latency fix |
| G4 securities-lawyer wording review | ₹10–20k once | First real B2B lead asking for terms, or first retail user about to pay | Legally clean revenue; one B2B client (₹3–5L/yr) dwarfs the fee |
| G5 re-upgrade Pro→Max (burst month) | ~₹7–16k/mo INCREMENTAL over Pro | **Decision Aug 5 or any month after**, on the scoreboard: candidate_patterns > 0 with ≥1 pattern at/near a stat_gates verdict, OR waitlist ≥ 10 — something Max-sized exists to build (payments, B2B onboarding, brain v2) | A one-month heavy-build burst. Otherwise stay on Pro: hotfixes + small paced modules continue, data compounds free, and G5 can open any later month |

**Kill criterion (the honesty clause):** if the full free build ends
with the miners producing zero patterns that survive stat_gates, we do
NOT buy data hoping more history fixes it — we stop and reassess the
approach. Reserve stays ≥₹60k until G2 revenue exists.

**Weekly scoreboard (owner-checkable, plain numbers):** years of
history in the brain · candidate_patterns rows · patterns past
stat_gates · shadow trades on the book · MCP tools working · waitlist
count. Spend proposals must cite the scoreboard.

Paper profits are not spendable cash; the business plans against the
₹1L only.

## Standing cautions

- Kaggle archive is survivor-biased — research/context only, never a
  backtest P&L source (sim-realism caveat stands).
- Backfilled events must be flagged `source='backfill'` so learned
  weights can distinguish lived experience from replayed history.
- Every discovered cycle goes through `validation/stat_gates` before it
  may advise anything — the halt-stack and risk-reducing-authority
  rules apply to cycle advisories exactly as to Dept 8.

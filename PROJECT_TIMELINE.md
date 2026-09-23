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

### 2026-09-23 · 17 commits · 59 files touched

- `da54895` docs: HANDOVER 09-23 22:30 — decision #109 deployed; live guard state, HV ranks, first hypothesis scoring
- `38dad4d` feat(risk): volatility-edge gate (HV rank ≥ 50 for short-vega structures) + correlation guard (max 2 open directional spreads per thesis, CORRELATION_GUARD_HIT); Proving Court hypothesis queue: ToD entry window + Mansfield RS, scored nightly, never live (decision #109)
- `eed66ee` audit(G3, decision #108): three archetypes already present — regime_read journaled on every proposal; end-to-end archetype tests per regime (read → structure → R:R floor → V1.1 sizing → OMS ticket → no stop)
- `b7244e6` docs: HANDOVER 09-23 night — decision #107 deployed, metals ids rolled, trail preview
- `f7d3aff` fix(cross_asset): roll COPPER/ALUMINIUM/ZINC front-month ids (all expired 2026-08-31 — caught by the #107 expiry guard's first live run); verified against the 09-23 scrip master
- `82d1460` feat(v1.2): equity-desk ATR trailing stops (one-way ratchet, floor = hard stop) replace the static target; equity EXIT tickets through the OMS, settled at the venue fill; macro instrument expiry guard on the CEO brief (decision #107)
- `5e2e74b` docs: HANDOVER 09-23 late — decision #106 deployed (fractional sizing, MCX pipelines); test: archiver pacing test points the MCX seam at nothing
- `ac69d48` docs: decision #106 (fractional sizing per account, MCX pipelines), ARCHITECTURE Dept 3 sizing rule, MODULES rows
- `d62b762` feat(v1.1+v1.3): fixed-fractional position sizing per account replaces the Rs.10k cap (decision #106); MCX commodity data pipelines (GOLD/SILVER/CRUDEOIL) — scrip master, darling_ids commodities block, chain archiver MCX extension, cross_asset SILVER + rolled CRUDE id
- `628ee02` docs: Issue 31 restore executed by the owner 20:31 IST — verified state + tracker preview (HANDOVER, ledger)
- `e896e94` docs: HANDOVER 09-23 (later) — #105 deployed, restore tool dry-run verified, owner runs the mutating step
- `bef66bc` docs: decision #105 (no mid-trade spread stop, #103 reversed, #104 withdrawn, restore tool), ARCHITECTURE Dept 3 standing rule, MODULES rows, ledger Issue 31 ruling
- `d6fd519` revert(#103→#105): no mid-trade stop on a defined-risk spread — premium stop reversed, the #104 thesis stop withdrawn; spreads held to target or expiry; scripts/restore_issue31_trades.py (MANUAL OFFLINE TOOL) re-opens the trades the stop cut
- `9ef6011` docs: DECISIONS — #104 row in newest-first order
- `98b7878` docs: decision #104 (thesis-invalidation exits, forward-looking; settled_at cards; Net Equity) + #94 recon engine built — MODULES rows, HANDOVER 09-23
- `2360a67` fix(recon): load .env via dhan_client before reading DHAN_CLIENT_ID
- `65a68bc` feat(v1.2): thesis-invalidation exits replace the vetoed 50% premium stop (decision #104, forward-looking from 2026-09-23); cards key on settled_at + Net Equity line; read-only recon engine (decision #94)


### 2026-09-21 · 5 commits · 13 files touched

- `ddcb1a5` docs: HANDOVER 09-21 night + ledger Issue 31 (retroactive #103 stop-outs invisible on the cards) + MODULES rows for the embed fitter and named unmarked positions
- `5515fc3` fix(firm_mtm): two unmarked positions on one underlying read 'NIFTY 50 ×2' so names add up to the count
- `826b1be` fix(telemetry): Discord embed limits enforced at the one door + partial MTM names its unmarked positions
- `5782ef4` docs: HANDOVER 09-21 — #102 + #103 merged and deployed to the VM (c3e73cc)
- `c3e73cc` fix(tests): pin the #103 options stop OFF in the post-expiry backstop test (the crash bars stop the spread out first; the backstop is the point)


### 2026-09-19 · 19 commits · 46 files touched

- `b7dccbd` feat(ops): scripts/audit_logs.py — the 15-day forensic log sweep (API frictions, stale data, memory runs, missed exits → logs/audit_report_15day.md, MANUAL OFFLINE TOOL) + decision #103 docs (MODULES, DECISIONS, HANDOVER)
- `b0b2335` chore: micro-commit — exits through the OMS: EXIT tickets (trade_tickets.kind, flipped legs, one atomic basket) issued and venue-filled inside the tracker's own settlement (EOD + intraday), venue exit slippage booked as a cost, primary + shadow accounts (decision #103)
- `737b701` chore: micro-commit — per-trade options stop: OPTION_STOP_LOSS_FRACTION (0.5, 0=off), one predicate spread_stop_hit in both EOD resolvers + the live bridge's stop_loss signal, verdict text, tests (decision #103)
- `b7ffb42` docs: decision #102 — dual paper treasury (MODULES, DECISIONS, ARCHITECTURE Dept 3, HANDOVER 09-19 night)
- `0ff709e` chore: micro-commit — proposer judges the 2L shadow account at both gate points and issues its venue ticket; LIVE_TRADE_BOOK renders both accounts side by side; 16 dual-treasury tests (decision #102)
- `c055eea` chore: micro-commit — OMS tickets carry account_id (additive column), router builds shadow-account tickets, venue journals shadow fills to the shadow ledger (decision #102)
- `de9fc1b` chore: micro-commit — dual paper treasury: PAPER_2L shadow account tables, gate, scaled release, config flags (decision #102)
- `24be2d8` docs: HANDOVER — Issue 30 fix deployed to the VM (0f39c04)
- `0f39c04` fix(equity_desk): settle exits the block-shadow leg logged first (Issue 30 root cause)
- `6e285ec` docs: Issue 30 resolved — eqd:3fedfeeb settled on the VM (−₹6,471 net, ₹98,377 released)
- `59f64b3` docs: HANDOVER 09-19 evening — live trade book live, Issue 30 (orphan eqd lock) logged
- `61ce6a7` feat(reporting): trade book surfaces orphan margin locks (found eqd:3fedfeeb on the first VM render)
- `b1dd11c` feat(reporting): LIVE_TRADE_BOOK.md markdown ledger — cron #32 (16:35 IST) + VM→Mac pull lane
- `b95394d` docs: HANDOVER 09-19 session close
- `33eea76` docs: HANDOVER 09-19 — paper venue deploy state
- `14595a6` feat(m2): paper venue fills Order Tickets with tier slippage; approved options entries go through the OMS (entry only, flag-gated, fail-open to legacy); tier-1 names join the scrip-master id universe — all 25 resolved (decision #101)
- `5f856bf` docs: HANDOVER 09-19 deploy state
- `07cca58` docs: decision #100 record — MODULES rows for oms/strategy_router/archiver extension, HANDOVER 09-19, blueprint GAP 3 status
- `321d574` feat(m2): tier-1 chain archiver extension by scrip-master id (unblocks the court); OMS schema trade_tickets/trade_legs/leg_events with five order states; one strategy_router issuing multi-leg Order Tickets — plumbing only, no order path (decision #100)


### 2026-09-17 · 2 commits · 2 files touched

- `66bd199` docs: HANDOVER 09-17 session close — Mini PC status, owner steps outstanding
- `f7409b7` docs: home node must never sleep — mask suspend targets, logind IdleAction, BIOS power-loss setting (CRON_SETUP)


### 2026-09-16 · 4 commits · 30 files touched

- `535bce7` fix(tests): RULE 6 — muzzle the Dhan market-data door, the Gemini post-mortem door and h4_shadow's live bars default under pytest (ledger Issue 29); suite 24 min → 57 s
- `5550555` feat(ops): home node — port the Mac lane to the Linux Mini PC: node_env.sh interpreter resolver, setup_mininode_cron.sh, portable gcloud/ollama resolution (decision #99)
- `ede0aec` docs+test: decision #98 record (MODULES, DECISIONS, HANDOVER); pin the live VIX read in the unknown-VIX proposer test (RULE 6 leak seen on the VM)
- `5496220` feat(risk): reward-to-risk guardrail — refuse any spread under 1.5 (directional) or 0.35 (iron condor/butterfly) before sizing; REJECTED_POOR_RR ledger fate; same floor in both shadow strategies (decision #98)


### 2026-09-15 · 2 commits · 13 files touched

- `587d777` docs: HANDOVER 09-15 — court deployed, first sitting numbers
- `f03d4b3` feat(validation): the Proving Court sits nightly — run_proving_court (age→TRIAL, weekly placebos, bhavcopy feed priced off the archived chain, shadow fires/grades, Wilson scorecards, FDR), ⚖️ field on the CEO brief, cron 21:00 (decision #97, closes GAP 1); SUPREMEIND post-mortem


### 2026-09-12 · 1 commits · 4 files touched

- `8beb71a` docs: SYSTEM_BLUEPRINT.md — CTO structural review, bird's-eye map + 3-gap analysis (court never tried a case; single-token/box/model-mark blindness; no order lifecycle or leg schema)


### 2026-09-11 · 6 commits · 17 files touched

- `8b7add9` docs: HANDOVER 09-11 night block — M4A shadow deploy state
- `f4aff47` feat(m4a): Glassbreaking Profits shadow — falling-knife + early-breakout primitives routed only to debit spreads, tranche/trail prep in plan_tracker, Proving Court enrolment (decision #96)
- `44e3ed9` docs: HANDOVER 09-11 evening block — deploy state for the Phase 1 hotfixes
- `69b1a87` fix(ops): health verdict counts auth codes since the last sweep offset, not the raw tail (false RED on the VM after the plan renewal)
- `dbb23a1` fix(ops): wall-clock expiry backstop for spreads + RED health alarms (auth / zero-capture / low memory) — Issues 26-28, decision #95
- `be000b9` docs: Dhan Orders API scoped as read-only broker-book sync — spec v1.0, decision #94, HANDOVER 09-11 (plan only, no code)


### 2026-09-10 · 1 commits · 2 files touched

- `a57c5cf` docs: ledger Issues 26-28 + Stage-B observation; HANDOVER 09-10 ops block (data-plan lapse, VM reset, swap)


### 2026-08-17 · 4 commits · 18 files touched

- `20883b9` feat(dept3): the risk-of-ruin halt LATCHES — capital cannot buy a resume (decision #92)
- `d3f3303` fix(dept3): capital moves translate peak_equity, never ratchet it — drawdown tracks trading, not deposits
- `17abc3b` feat(seq3): 🤖 Pattern Miner field on the CEO brief + liquidity-tier slippage on paper fills (#91)
- `6df1d82` feat(dept1): corporate-action adjuster — split/bonus-adjusted history in bars_for, latest bar always raw (#90); 49-row config from F&O scan


### 2026-08-16 · 12 commits · 24 files touched

- `ffbc914` feat(discovery): miner depth gate 60 -> 50 frames (owner decision #87); stage Next-Version + weekly release cadence in docs
- `0054289` test: stub the #68 exposure gate in the headless margin-gate test — it read the real journal
- `10ff440` feat(research): earnings reaction study — YoY quartiles vs forward returns (sandbox, not a finding)
- `e9ab7c3` docs: V2 sandbox state dump for session handoff
- `1628bb7` feat(research): deep history, steel proxies, sourced elections, plant-location extraction
- `93d21a9` feat(research): metals into cross_asset, ONI + election clocks, geo extraction from filings
- `9643faf` feat(research): run the event study on six years, and fix what running it exposed
- `d792820` feat(research): V2 sandbox — geo exposure, credit monitor, event-study backtester
- `4aceaaf` wip: readable credit render
- `1449abb` wip: lake-root fix
- `3f03a18` wip: vocabulary rewritten from live lake
- `b0fc89c` wip: v2 research sandbox


### 2026-08-08 · 1 commits · 1 files touched

- `e79dbe1` docs(handover): Friday session-close health check — clean session, router not yet exercised


### 2026-08-05 · 16 commits · 51 files touched

- `b2e6ef1` docs(handover): 2026-08-05 final — V1 desk complete, Sunday code freeze in effect
- `89d3dad` feat(dept2): the 3-D options desk — multi-index, macro routing, time horizons
- `a22bef8` feat(dept2): equity options behind a physical-settlement gate; fix a test-isolation leak
- `edd1290` fix(dept2): unblock G3 — range before direction, graded trend, butterfly wired
- `84f55ff` feat(level-1): ATR trailing stops, dual-horizon sentiment storage, cross-asset tap
- `44e64cc` feat(dept6): Sequence 1 — put the context back next to the numbers
- `f6f7f38` fix(dept6): age the bug ledger — a fixed bug must stop looking like a live one
- `8934884` feat(sequence-2): sanitize and re-wire the hanging orphans
- `17af0f6` docs(handover): 08-05 — edge_miner transport hardened, Mac brain_map fresh again
- `6a5ba3f` fix(infra): harden edge_miner's SSH transport — retries, keep-alives, no escaping timeout
- `c2dcca2` docs(handover): 08-05 night — Dept 5 is waiting, not broken; the wait is now measured
- `d1cab11` fix(dept5): the depth gate MEASURES the wait — countdown vs corpse
- `a53d41f` docs(handover): 08-05 late — ship resurrected, sector producer built, veto re-armed
- `03a42f9` fix(infra): resurrect the Mac->VM ship, and give sector_index_bars a producer
- `6d5f29d` docs: SYSTEM_XRAY + PROP_ROADMAP, and the 08-05 handover block
- `31ee6d2` fix(dept1): staleness guard — disarm the stale sector veto, scream on the ops card


### 2026-08-04 · 4 commits · 12 files touched

- `b778e10` docs(handover): 08-04 VM lane — desk price capture, darling day-tap, bhavcopy migration
- `dcf604d` docs(handover+ledger): Ollama is on-demand only — background agent must stay disabled
- `ca6558f` chore(cron): migrate bhavcopy to the VM; stagger the one real collision
- `d7d888f` feat(ingestion): capture desk + darling prices; name live_quote failures


### 2026-08-01 · 4 commits · 6 files touched

- `0ec1a8b` docs(handover): 08-01 final — AD instrument validated, 0 admissions, 2026-03-27 logged as watch item
- `afe7ad1` docs(ledger): AD-2 definitive 500-surrogate verdict — 0 admitted, COVID splits the nulls by 0.008
- `ec57a56` feat(discovery): co_stress redefinition + the three AD-2 structural fixes
- `327dc89` docs(discovery): AD-2 fails in the wild — splice-artifact null + ragged missingness


### 2026-07-31 · 1 commits · 3 files touched

- `c342c71` fix(ops): office_close skipped Chrome/Ollama — pipefail + grep -q lied


### 2026-07-30 · 11 commits · 24 files touched

- `2673367` fix(ops): office_close never slept the Mac; merge the two close-down tools
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

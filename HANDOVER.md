# HANDOVER.md — Cold-Start Brief

## ✅ THURSDAY PROTOCOL — CLEARED 2026-07-22 (owner returned early, ordered "start building")

The 2026-07-21 blocking directive (bug-ledger report → analyze → fix,
before anything else) was executed 2026-07-22 night. Verdict: 55 items,
ONE real code bug (intraday_tracker rate-limit bursts → in-sweep retry,
`8e70e97`) + one suite flake (journal-drift test isolation, same
commit); MACPOWER budget-refusal and the corporate_events arg error were
non-bugs. Full triage record: `docs/observation_week_ledger.md`. The
protocol machinery (`python3 -m src.bug_ledger --report` on the VM)
remains THE first read after any future autonomous stretch.

## 📌 2026-07-22 MILESTONES — the build sprint opened (code freeze lifted by owner)

- **Brain-MCP server** (`src/brain_mcp.py`, `7b7faee`): the data
  product's first door — 9 read-only tools over the brain, zero new
  deps, SEBI posture (data-not-advice) enforced BY TEST; repo
  `.mcp.json` = zero-step Claude Code demo. Localhost-only until gate G2.
- **Time Machine backfill**: NSE free archive floor probed = ~Oct 2019;
  `bhavcopy_clerk --backfill 2500` running on the Mac toward ~6.8 years
  of full-market daily bars. ₹0 spent.
- **Master plan + budget**: `docs/cycle_hunter_plan.md` — replay history
  backward / validate forward, proof-gated spend (G1–G5, ₹1L cap),
  Aug-8 Max-window schedule. THE living plan; PLAN.md is history.
- **Workflow**: `docs/dev_workflow.md` — the Speed & Scale protocol
  (scoped tests while iterating, full suite ONLY as the pre-deploy
  gate, zero-tech-debt rule, parallel-lane rules). Binding on every
  session.

---

Read this to pick up the project cold in a new agent session. For vision see
`OVERVIEW.md`, for system flow see `ARCHITECTURE.md`, for the file index see
`MODULES.md`, for why past calls were made see `DECISIONS.md`. **This file is
updated only at milestone states, not on every commit** — check `git log`
for anything more recent than what's written here.

## ⚠️ COVERAGE GAP IN THIS FILE, READ FIRST (noted 2026-07-20)

**This brief jumps from 2026-07-20 (the section directly below) back to
2026-07-11.** The nine days between — honest paper fills #70 + the stale
LOT_SIZES fix, portfolio Greeks #71, performance #72, book_context #73,
text-intelligence #74, RSS ingestion #75, gated nightly discovery #76, the
unified-main deploy, the whole Dept-8 Analysis department, the F&O intake
tranche, the Issue-21 XBRL clerk, and dual-horizon news sentiment — are
**recorded in `DECISIONS.md` (#70–#76), `MODULES.md`, and
`docs/observation_week_ledger.md`, but were never folded into this file.**
For anything in that window, trust `git log --oneline` + those three files
over this brief's silence. Not reconstructed here rather than risk a
plausible-sounding but unverified summary.

## 🟢 THE AUTONOMOUS RUN — ₹2L clean sheet, ₹10k/trade hard cap, set-and-forget (decision #84, 2026-07-21, owner final override)

**The owner stepped away. The firm reboots at Rs.2,00,000** (account
reset: realized 0, peak 2L; the 10L era is archived in the pre-migration
DB backup; open options spreads CARRY and settle into the new base).
Treasury pool is now DERIVED from the account (never a constant);
granularity rescaled (deadband ₹10k / step ₹25k / round ₹5k); equity
budget seeds ₹60k. **Hard cap `max_risk_per_trade_rs`=₹10,000 on BOTH
desks** applied after percentage sizing: equity risk budget min-capped;
options lots capped by max_loss, and a structure whose max_loss/lot
alone exceeds ₹10k is refused. 100% utilization allowed (no idle
buffers; the one cash door is the only brake). Equity sizing: 5% risk /
25% notional per name. Firm halts auto-rescale: daily 3% = ₹6k, ruin
10% = ₹20k trailing. **Set-and-forget:** an unhandled master_scheduler
crash fires a real-time 🚨 page (traceback tail, then re-raises).
**Directive 4 — 5 Discord messages/day (`notifier.budget_gate` at the
one door):** crash ALWAYS pages; scheduled digests (EOD/CEO/tiers +
Saturday cards) spend the budget; the 2-hourly snapshot DROPS; every
other card (trades, rotations, 🧠 sizing, review flags) SPOOLS to
`logs/discord_digest_queue.jsonl` and lands in the next digest's
"📦 Batched signals" field. This supersedes the 07-16 real-time
review-flag rule for the autonomous run. Kill switch
`discord_budget_enabled`.

## 🟢 THE VM-SHIFT — equity desk is VM-NATIVE, one database, LIVE trading (decision #83, 2026-07-21, owner override)

**Owner formally overruled the observe-first hold, accepted wiping the 5
day-old paper positions, and ordered the shift the same day.** The desk
now lives in the VM's ONE firm database: equity notional locks through
the same `pm.request_entry` door as options margin (`eqd:` prefix = the
desk's identity; deployed/realized are views over tagged rows), the
treasury is ONE atomically-updated row (`treasury_state.equity_budget_rs`
— v1's two-phase/reconcile/SSH machinery deleted; VM cron #21 19:50),
and `run_darling_live_cycle` rides the market loop beside the block-leg
shadow: LIVE exits (stop/target/time at real quotes), Strong-Sell
force-exits, mid-session settlements, LIVE entries when a Buy-tier
name's quote sits INSIDE the strict buy zone (`fill_basis:"live"`).
Quote ids: `data/darling_ids.json`, built weekly ON THE MAC from Dhan's
public scrip master (exact-match only, #78), shipped nightly. **The Mac
is analysis-only now** — its 19:15 chain ends by shipping tiers + levels
+ ids; the VM freshness-gates all three (stale tiers = no new entries,
exits always run; stale ids = unmarked, never guessed). Migration:
Mac desk DB + ledger archived (.bak), resolved autopsies MERGED into the
VM ledger (learning survives), the 5 open positions wiped per the owner
("the system will simply re-enter them"), the old `equity_desk_allocation`
reservation released, budget seeded at the routed ₹4,00,000. Report
cards (#82 surfaces) now render the desk LIVE from local state. First
live session: next market open 09:15.

## 🟢 ONE FIRM VIEW — every report card now shows BOTH desks (decision #82, 2026-07-21, first freeze exception)

**Owner ruling ("lift freeze and fix it — one ledger"):** the 12:00 card
showed only options; the equity desk's 5 funded positions were invisible
outside the Mac's 💼 card. Fixed at the VIEW layer — the physical stores
stay separate BY DESIGN (two machines, two write owners; equity rows in
the options journal would break plan_tracker's sweep). After every Mac
19:15 chain: `equity_desk.publish_snapshot()` → scp to the VM
(`firm_treasury.vm_push_file`) → the 2h Portfolio Report Card (full
section + table), the 15:45 EOD summary (💼 field), and the 16:30 CEO
brief (headline line) all render the equity book beside options. Labeled
"EOD marks" always (Mac holds no token); >30h old = "STALE" on the card;
missing = "no snapshot yet"; every seam fail-open. **Freeze resumed
after this deploy.**

## 🟢 THE FIRM TREASURY — the 7L/3L split is now DYNAMIC (decision #80, 2026-07-20 night, owner Directive 1)

**#79's static split lasted about two hours** — the owner ruled it
capital-inefficient (correct: options' stress-adjusted peak margin use is
~₹1.9L) and green-lit dynamic routing with three pre-agreed pushbacks
(nightly cadence not intraday; evidence-bar learning deferred to
Session 2 `adaptive_sizing`; gap-shock down-weighting). `src/firm_treasury.py`:
mechanical regime router (base 30% equity share; tilts for NIFTY trend,
Buy-tier depth, deep value, high VIX, options margin demand; clamp
15–60%, ₹50k deadband, ₹1L/night max step), runs inside the 19:15 EOD
chain between tier grading and the shadow leg. Capital moves =
subscribe/redeem on the desk's `starting_capital` (peak shifts with base
— the ruin halt stays rupee-honest; NOT the originally-planned 10L
re-init, which would have diluted the desk's 10% halt to ~0.3% —
pushback #4, applied during build) mirrored by the VM's
`equity_desk_allocation` lock under the RAISE-FIRST invariant
**E_vm ≥ E_mac**: any mid-move crash idles capital for a night, never
double-spends it; next run reconciles E_vm := E_mac. Unreachable VM =
frozen split, 3rd consecutive night = one warning card. Ledger
`logs/treasury_ledger.jsonl`; kill switch `treasury_enabled`.
**Session 2 ✅ BUILT (decision #81): `adaptive_sizing.py`** — the
autopsy-driven sizing feedback loop is LIVE on both desks (equity
fund_entry risk-budget multiplier; options lots penalty/veto after
size_lots). Break-even-centered priors = 1.0x until each key's own
record earns otherwise; penalties fast (≥4n, floor 0.25x), vetoes
earned (≥8n, Wilson UPPER bound under break-even, telemetry row kept),
boosts slow (≥10n, LOWER bound clear, cap 1.5x inside existing caps),
gap-shocks half-weight, ticker veto overlay (≥5n). Ledger
`logs/sizing_adjustments.jsonl`, one card per key state-change; kill
switch `adaptive_sizing_enabled`; CLI `python3 -m src.adaptive_sizing`.
Also tonight: the desk's FIRST LIVE FUNDED RUN — 5 darling entries,
₹1,77,540 locked at the 19:15 chain.

## 🟢 THE EQUITY DESK — the darling book now trades PAPER CAPITAL (decision #79, 2026-07-20 night)

**Owner ruling ("10,00,000 of paper money only buddy — let's see how
efficiently our system runs the 10 lakhs"), issued AFTER the recorded
pushback; supersedes #77's zero-capital clause for the darling leg only.**
`src/equity_desk.py` (Dept 3): a Rs.3,00,000 slice of the firm's 10L funds
darling Buy-tier entries — 1% risk / 15% notional cap, whole shares,
delivery-friction-net settlement — through portfolio_manager reused
conn-generic against `data/equity_desk.db` (Mac). Same halts (10% ruin,
daily 3%), same silent exhaustion, zero re-implemented risk rules. The VM's
options account carries the matching standing lock `equity_desk_allocation`
(**run once on the VM:** `python3 -m src.equity_desk --reserve-firm-slice`)
so the firm total stays one honest 10L. Funded entries stamp
`mode="PAPER_CAPITAL"`; funding failures keep the telemetry row with the
reason (the learning ledger never loses a line); the proposer's Dept-3
import ban still holds — seams injected only at `patience_basket.eod_chain`.
One Discord card per EOD run, only when money moved. Kill switch
`equity_desk_enabled` (code default OFF). Desk summary:
`python3 -m src.equity_desk`; crash reconciler: `--sweep`. The block-VWAP
leg stays pure telemetry. The desk's equity curve starts at zero history —
judge it like performance.py judges everything: no verdicts on thin samples.

## 🟢 THE DARLING LIFECYCLE IS LIVE — 7-tier grading + the two-clock architecture; both Mac crons INSTALLED (updated 2026-07-20 evening)

**Decision #77, commits `5c326a3` + `1629bc8`, pushed; suite 1373 green.**
The binary RIPE/waiting basket is SCRAPPED. Dept 8 now runs a lifecycle
system: every darling is graded EVERY EOD into one of seven tiers
(`strong_buy` / `weak_buy` / `strong_hold` / `weak_hold` / `weak_sell` /
`strong_sell` / `watch`) plus an honest Tier-0 `ungraded` for names whose
data can't support a grade. A name is never "done" after entry — the same
table that says BUY also says HOLD and SELL for what the paper book holds.

**The two clocks (do not collapse them into one):**
- **DAILY** — `patience_basket --eod`, **Mac cron 19:15 Mon–Fri**: bhavcopy
  → F&O bundle → pricer → valuation → tier grading → shadow leg. Re-grades
  on PRICE, because prices move daily. This half already existed and was
  already dynamic; a weekly-only recalibration would have made it WORSE.
- **WEEKLY** — `weekly_recalibration`, **Mac cron 10:00 Saturday**: refresh
  quarterly filings → re-screen → No-Orphan pins → rebuild → one card.
  Re-judges FUNDAMENTALS, which only change when filings arrive, and
  OVERRIDES the daily grade through pins.

**Mechanical definitions (never re-derive these by feel):** near-zone =
within 5% above the buy-zone ceiling · momentum = close > 50-DMA AND 50-DMA
> 200-DMA · losing volume = 20-day avg turnover < 60-day avg · near-stop =
within 1 ATR of the stop reference (trailing pivot floor first, else the
hard stop).

**The No-Orphan rule:** a held name failing the weekly screen is never
"orphaned" — it is PINNED (`data/darling_pins.json`) into the tier table
until its paper position closes, then drops entirely. A REJECTED name pins
`strong_sell`; a name the screen merely LOST THE DATA to judge pins
`ungraded` — a sell verdict is never manufactured from absence.

**Shadow book wiring:** entries from `strong_buy` + **in-zone** `weak_buy`
only (near-zone names are watched, never chased). `strong_sell` FORCE-EXITS
an open shadow (`fundamental_break` when pinned, `strong_sell_tier` when
valuation-driven); `weak_sell` does NOT — the position's own stop is
already the thesis-break detector. Still zero-capital PAPER_TELEMETRY,
still advisory-only (Law #63).

**Cards:** family transitions ONLY (buy/hold/sell/watch). Intrafamily moves
(strong_buy → weak_buy) are visible in the table but silent — a valuation
wobbling 25→26→25 would otherwise fire three cards in three days. First
grading fires ONE distribution summary.

**First live grading, 105 darlings:** 0 strong_buy (nothing is
simultaneously in-zone AND ≤25 — an honest empty bucket, not a bug) · 15
weak_buy, 10 of them in-zone and entry-eligible (the old RIPE trio
HEROMOTOCO 30 / ESCORTS 34 / TCS 35 all landed here) · 17 strong_hold · 17
weak_hold · 17 weak_sell · 12 strong_sell (9 below their hard stop) · 17
watch · 10 ungraded.

**Where the VM stands:** two commits behind, DELIBERATELY. Everything in
this work is Mac-only by the boundary doctrine (bhavcopy lake, pricer,
valuation all live on the Mac; the crons are NSE-crawling and must never
run from the VM's IP). No VM pull or restart is needed; it syncs at the
next regular deploy.

**Open / next:** `business_metrics`, `liquidity_rank` and `ticker_dossier`
(landed in `1629bc8`) still have NO dedicated test files — Dept-8 test
debt. First cron-fired EOD run is 19:15 on 2026-07-20; first weekly
recalibration is Saturday 2026-07-25 (its filing-refresh stage takes
15–30 min, which is normal).

## 🟢 HOLY-GRAIL PHASES 4 & 5 COMPLETE, MERGED & DEPLOYED — the discovery brain now RUNS; it is DATA-STARVED by design, not broken (updated 2026-07-11 evening)

**The single most important fact for a cold session: the build has reached
its designed resting point.** Phases 0–3 (substrate + confluence + macro
alignment) were already in; this evening Phase 4 (the proving harness) and
Phase 5's entire offline-buildable surface (the miners + the strategy
evidence view) landed on `main` via PR #4 (`41dcc72`) and PR #5
(`3dc7d21`), and both are pulled to the VM. Suite **909 green**, all
offline. **Nothing more should be BUILT until real data accumulates** —
the remaining Phase 5 pieces are data/human-gated (see "next" below) and
building them now would rediscover the entry gates on an empty corpus
(#50's exact failure). The system's job now is to RUN and accumulate.

**What Phase 4 added — the proving harness (`src/validation/`):** every
pattern the brain ever surfaces must first survive this. `stat_gates.py`
(the shared anti-hallucination toolkit — Wilson lower bounds, exact
binomial, structural breakeven nulls, Benjamini-Hochberg, block-permutation
nulls, split-window stability, `promotable`: sim supports but never solely
justifies; BALANCED floors, all config-tunable via `harness_*` keys),
`registry.py` (pattern lifecycle CANDIDATE→TRIAL→VALIDATED→LIVE_ADVISORY /
QUARANTINED / INSUFFICIENT_N / DEAD, frozen-definition idempotency, audited
soft-only transitions), `trial.py` (walk-forward split + 5-day embargo +
`shadow_trades`, never `journal.jsonl`), `monitor.py` (validation-is-a-lease:
CUSUM + Wilson-crossing auto-quarantine, adaptive lease expiry — wired as
Sleep-Phase Task H), `placebo.py` (seeded information-free hypotheses →
realized false-discovery meter), `digest.py` (the owner's weekly Discord
window — **cron #13, Saturday 10:00 IST**; first fire 2026-07-18).

**What Phase 5 added — the discovery brain (`src/discovery/`):** the brain
starts finding its OWN hypotheses instead of only checking hand-coded ones.
Miners ENUMERATE + REGISTER candidates; they never surface anything (the
harness above is the only path to a card). `cooccurrence_miner.py` (Apriori
over resolved-outcome transactions = event tags ∪ market-day `ctx:` tags,
stratified base rates so it can't rediscover the pipeline's gates),
`sequence_miner.py` (the same core on a time axis — `lag{k}:` antecedents
k trading-days BEFORE entry, the "early tell precedes the move" shape H1/H2
turn on; no look-ahead by construction, timelock-proven), `run_miners.py`
(the manual orchestrator — honest combined report), `strategy_evidence.py`
(the "check WHICH structure" view — per-structure Wilson-bounded win-rates,
real/sim never pooled, ≥5-real render floor, descriptive PREFER/ABSTAIN on
the honest lower bound; the read-only substrate the future duel consumes).

**CRITICAL — the miners are MANUAL-ONLY, deliberately not cron-wired.** On
today's empty corpus `./venv/bin/python -m src.discovery.run_miners` reports
`0 survivors — CORRECT` and says why (support floor not met). That is the
designed output, not a failure. Wiring them into the nightly sleep phase
waits until `daily_context` has enough history for an itemset to plausibly
clear the floor — that wiring gets its own DECISIONS row when the data
justifies it.

**Next per HOLY_GRAIL_PLAN §8.6-8.7 (ALL gated — do NOT pre-build):**
counterfactual structure pricing + the champion/challenger **duel** (needs
a VALIDATED pattern + a ≥30-day disagreement floor + the human dethroning
ritual #49 — never auto-applied), and **skeptic v3** (realized-vs-implied
vol / distance-to-support / days_to_results features; the ablation study is
deferred until the layers reach ≥50% non-NULL coverage, else it would
"prove" the new layers earn zero — an artifact the owner would misread).
The trigger to resume building is DATA: the first pattern reaching
VALIDATED, or layer coverage crossing the ablation gate. Until then, watch
the weekly digest and let the substrate fill.

## 🟢 HOLY-GRAIL PHASES 0–1 DONE + BACKFILL RUN; Sat 07:00 renewal VERIFIED; Monday 07-13 09:10 IST is the first live session (updated 2026-07-11 midday)

**Sat 2026-07-11 session, on top of the overnight builds:** the two
overnight PRs are merged and ON THE VM (PR #1 `84fe9c8`: data lake +
Phase 0 capture jobs + Phase 1 backfill CLI/flows/earnings + Phase 2
Evidence Snapshot substrate + provenance firewall; PR #2 `9e85b87`:
knowledge-graph visualizer + Phase-3 macro→sector prior config). VM
crontab now has 12 jobs — the 5 new capture crons (earnings 19:20,
deals 19:30, flows 19:35, perishables 19:45 daily; chains 15:40
Mon–Fri) fire for the FIRST time tonight — check their logs and
`data/lake/` on the VM after 20:00 IST. Suite 805 green (one
weekend-only test bug fixed: heartbeat test pinned to a fixed Monday).

**The Phase 1 moat is REAL now:** the 3-year NSE bulk/block backfill
ran clean from the Mac after fixing three NSE breakages found live
(retired endpoints → `historicalOR`; JSON API silently truncates to
~70 rows → must use `&csv=true`; homepage 403 would have aborted the
daily pull's warm-up — all fixed in `4aac239`, regression-tested,
ledger Issue 11). Result: **75,600 deals / 742 trading days
(2023-07-11 → 2026-07-10, no gap before tonight's first daily pull),
0 failed windows**, raw CSVs archived to the Mac's lake, JSONL shipped
to the VM, and the VM's entity-affinity ingest folded all of it: 16
`concentrates_in` edges across 6 promoter groups, each with its TRUE
historical `valid_from` (as-of projection verified). Sleep-Phase Task
F continues folding daily increments from here.

**Sat 07:00 renewal (Issue 10 watch item (a)): VERIFIED WORKING** —
cron fired, first attempt "Invalid TOTP", retry waited one TOTP window,
minted clean (expiry 07-12T07:00). Remaining watch items: tonight's
first capture-cron firings, Monday's first live session (clean
afternoon past 12:00, auto-approve keeping `/pending` empty).

**Sat afternoon update — PHASE 2 IS COMPLETE, PHASE 3 LANDED TOO (two
sessions in parallel; watch for concurrent agents in this repo!).** A
mobile session's PR #3 (merged 11:28 IST) built §5.1-wiring, §5.2
daily_context, §5.3 receipts + `python3 -m src.explain`, a timelock
harness (`src/validation/timelock.py` + `tests/test_no_lookahead.py` —
THE canonical one; register new discovery functions there), all of
Phase 3 (descriptive alignment line, composition law = decision #63 +
`tests/test_composition_law.py`, engagement tripwire) and Phase 4's
`stat_gates` start. This session then closed the read-side timelock
hole PR #3 missed (concentration from history-as-of-T, decision #64,
proven by a fails-on-old-code test) and built §5.5: the T+1-open
execution-timing contract (`src/execution_timing.py` +
`run_simulation(eod_signal_days=…)`, decision #65 — EOD signals decide
on T, fill at T+1's true open, rows carry signal_day/signal_age_hours/
entry_basis; refusal-never-interpolation). Suite 860 green, VM synced
and services verified after each push.

**Next per HOLY_GRAIL_PLAN §12:** Phase 4 remainder (pattern registry +
lifecycle, walk-forward trial, stability battery, noise-injection
suite — `stat_gates` P4-1 exists), plus the small §5.6 leftovers
(llm_mined confidence cap, nightly no-LLM audit of outcome_derived
edges). Phase 1's gated sibling streams (delivery %, insider/SAST,
shareholding) wait for flows to run clean 2+ weeks. §4.6 gap-playbook
ids (US/GIFT via verified Dhan ids) also still open.

## 🟢 SCRATCHPAD PHASES 1–8 + REFINEMENT — DEPLOYED TO THE VM Fri 2026-07-10 ~21:45 IST AND SMOKE-TESTED; Monday 07-13 09:10 IST is the first live session (updated 2026-07-10 late night)

**The single most important facts for a cold session: the deploy is
DONE — the VM runs `bf9dc77` (everything below), pushed and pulled Fri
2026-07-10 night, markets closed. The full checklist executed and
verified: `PAPER_AUTO_APPROVE=1` live in the VM `.env`, `setup_cron.sh`
re-ran clean (7-job block, 07:00 renewal restored, 2h report card added),
root's interim `30 6,18` renewal crontab REMOVED ("no crontab for root" —
the single-07:00 cadence of `docs/token_renewal_cadence.md` is now
reality), all 3 services active, regime backfill 366/366. Smoke-tested
live: a manual retry-hardened `renew_token` run minted a real token
(expiry 07-11T21:47), `get_live_price` works on it, the gateway kept
serving keyed requests after the mint with no restart, `/dashboard` is
200 through the tunnel from outside (401 without key). Ledger Issue 10
carries the full verified record. Remaining watch items: Sat 07:00 first
cron-fired renewal on new code, Monday's first live session (clean
afternoon past 12:00). NOT yet done (optional, Mac-side): evolution
LaunchAgent install (`bash scripts/install_evolution_agent.sh`) and the
`pull_snapshot_from_vm.sh` sync. Never restart VM services mid-session
(09:15–15:30 IST) still stands.** Suite went
486 → **710 green**, all offline; the full diff passed an 8-angle
multi-agent review (27 candidates → 10 verified findings → all fixed,
commit `1794ef4`). What landed, by phase:

1. **Self-healing token + Dhan hardening** — `src/token_provider.py` (live
   .env re-read; Issue 5 fix) wired into `dhan_client._get_client`;
   `renew_token` retries "Invalid TOTP" in the next TOTP window (Issue 10);
   `setup_cron.sh` refuses non-IST hosts (Issue 1) and warns on duplicate
   renewal crons; `src/dhan_guard.py` `SafeDhanClient` (classified DH-9xx
   errors, audit trail); in-place double-nest fixes for
   `get_daily_ohlc`/`get_quote`; single-renewal-cadence decision doc at
   `docs/token_renewal_cadence.md` (root cron removal = deploy-day step).
2. **Visibility + cooldown persistence** — `src/positions.py` +
   `python3 -m src.view_positions` (read-only open-positions table);
   gateway `GET /api/discord/positions` + bot `/positions` embed; journal
   entries stamp `created_at` (IST) and `CooldownRegistry.seed_from_journal`
   rebuilds cooldowns across restarts (Issue 8 fix); `/analyze`'s lying
   "Yahoo Finance" string fixed (Issue 7); Ollama-offline logged once
   quietly (Issue 4); edge miner `extractor_ready()` end-to-end probe —
   no more "ok" from a dead extractor (Issue 9).
3. **MFE/MAE expectancy surface** — `src/calibration/mfe_mae_analyzer.py`
   (spec §3.1/§3.2): journal + simulated_trades sources (read-only
   `mode=ro`), one bar-fetch per ticker via SafeDhanClient, winner-based
   Apex TP/SL suggestion with a 20-trade abstention floor; advisory only.
   First real run needs a valid token (VM, post-deploy).
4. **Auto-approve gate + report card** — `PAPER_AUTO_APPROVE` env switch
   (**default OFF**; when on, headless proposals approve through the same
   `decide_pending` path a human tap takes — decision #53);
   `src/portfolio_report.py` 2-hourly read-only Discord book snapshot
   (cron `0 */2`, self-gates to market hours).
5. **Threat mitigation** — `dhan_guard` freshness guard (`StaleDataError`
   when a 200-OK quote/chain is >60s old mid-session; off-hours and
   untimestamped payloads pass); evolution anti-overfitting guards
   (30-trade corpus floor + split-window stability → new verdict
   `unstable_out_of_sample`); evolution scheduling moved OFF the VM cron
   to a Mac LaunchAgent (`scripts/com.alphatrading.evolution.plist` +
   `install_evolution_agent.sh`, Sat 02:00, pinned interpreter) — **not
   yet loaded into launchd**, run the installer to activate.
6. **Event-driven web dashboard** — `src/web/static/dashboard.html`
   (single-file, SSE-driven, deliberately no polling) + `GET /dashboard`,
   `GET /api/web/positions`, `GET /api/web/events` on `src/api.py`.
   Behind the gateway it authenticates via `?api_key=` on the page URL
   (EventSource can't send headers — refinement fix #1).
7. **Semantic resonance & macro horizon matrix** (`d4df8cc`) —
   `src/ingestion/macro_tracker.py` (Crude/Gold-India/Gold-World/USDINR →
   SHORT/MEDIUM/LONG matrix; verified-ids-only Dhan path, fail-open to
   `data/macro_snapshot.json`, index-impact weights), `src/ingestion/
   news_parser.py` (local-Ollama headline → strict 5-key signal frame),
   `src/knowledge_graph/resonance.py` (CONFLICT/RESONANCE/NEUTRAL
   advisories vs open positions, strike/expiry-roll suggestions,
   brain_map strictly mode=ro). All advisory, zero writes to live state.
8. **Engine-published market snapshot** (`0ebd736`) — the live loop
   publishes spots + every position's mark to `data/market_snapshot.json`
   each cycle (`src/market_snapshot.py`); `portfolio_report.
   get_live_marks()` is THE shared mark ladder (snapshot first — zero
   Dhan calls — direct fetch only for uncovered positions), consumed by
   the dashboard AND the 2h report card. Makes the engine the single Dhan
   quote consumer (decision #48 architecture); `scripts/
   pull_snapshot_from_vm.sh` syncs it to the Mac post-deploy.
9. **Refinement pass** (`1794ef4`, Fri evening) — all 10 verified review
   findings fixed: dashboard gateway auth, equity-mark starvation,
   freshness guard scoped to indexes (+ implausible-age escape), honest
   `release_entry` after commit, ragged-payload tolerance, single-sourced
   open-position predicates, auto-approve never on injected books, shared
   mark ladder, one `unwrap_payload`, resonance graph-query memoization.
   Also that afternoon (VM config hotfix, ledger Issue 10 UPDATE): root's
   renewal cron rescheduled to 06:30/18:30 IST and the 07:00 user renewal
   DISABLED — a 12:00 IST mint had blinded the live loop all afternoon
   (stale in-memory token; the deployed code can't re-read `.env`).

**Weekend deploy checklist (user-approved timeline, target Sun 07-12,
live Mon 07-13 09:10 IST):** push the 12 commits → VM `git pull` +
`pip install -r requirements.txt` + restart services (markets closed all
weekend, restart freely) → **token endgame, order matters** (per the
INTERIM STATE note in `docs/token_renewal_cadence.md`): the retry-hardened
`renew_token` is now deployed, so re-enable the 07:00 user renewal
(uncomment the crontab line tagged `#DISABLED-2026-07-10-hotfix`) and
THEN remove root's interim `30 6,18` cron (backups:
`~/root_crontab.bak-20260710-152339`, `~/user_crontab.bak-20260710-152339`)
→ re-run `scripts/setup_cron.sh` (adds the report card; asserts IST) →
restart the Discord bot (`/positions` registers) → verify the dashboard
through the gateway at `/dashboard?api_key=<API_KEY>` (query-param auth is
how the SSE stream authenticates) → optionally `bash
scripts/install_evolution_agent.sh` on the Mac and set up
`scripts/pull_snapshot_from_vm.sh` for Mac-side live marks → watch
Sunday 18:30 (or next) renewal run on new code + Monday's first session,
especially past 12:00 (the old blinding hour — closes ledger Issue 10).
**USER DECISION 2026-07-10: set `PAPER_AUTO_APPROVE=1` in the VM's `.env`
at deploy** (the switch means nothing on the Mac — the VM is the engine,
decision #47). Consequence to expect: proposals auto-journal as APPROVED
and the `/pending` queue stays empty by design; the human role shifts
from Approve/Reject to monitoring, and the margin gate + persisted
cooldown (Phase 1/2) become the only brakes. **UPDATE 2026-07-13: the
flagged concentration/duplicate-exposure gap is CLOSED** — decision #68's
`src/exposure_gate.py` now blocks a second open spread on the same
underlying+direction at proposal time (before margin lock), after the
book was observed carrying NINE near-identical bear put spreads; a
trend-flip exit advisory rides the live loop alongside it. Flip it back off by deleting the line and
restarting `alpha-trading` — it is re-read per call, no code change.

### Merge Protocol — "Unified Main" (standing Saturday-deploy rule, added 2026-07-16)

No fragmented branches during a production push. Before the Fable
Pre-Review and before any VM deployment:

1. **Unified Main.** Every open **backend** side branch — including the
   `market_loop` test-fix branch — is merged into `main` first, so the
   thing that gets reviewed and deployed is one consolidated tree.
   **Carve-out (non-negotiable, per [[project_branch_strategy]] and
   [[project_lovable_terminal_ui]]):** the UI branches — `lovable-ui`
   and the `Trading Terminal/` frontend — are NOT "side branches" for
   this purpose and are NEVER merged into `main`. `main` stays the
   framework-free Python backend; the React UI keeps its own branch.
2. **Unified Test Run.** Post-merge, run the FULL suite locally against
   the consolidated `main` (`python3 -m pytest`). Deploy only on 100%
   green — a red or skipped test on unified `main` blocks the push.
3. **VM Deployment from unified `main` only.** The live-VM deploy
   (`git pull` + restart, per the checklist above) happens strictly from
   this merged, fully tested `main` — never from a side branch and never
   with a fix branch still outstanding.

Order for a Saturday deploy: **merge backend branches → unified test run
(100% green) → Fable Pre-Review → VM deploy.**

## ✅ Regime-Aware Memory — BUILT AND TESTED; skeptic hypothesis honestly NOT confirmed (2026-07-09)

Roadmap item #4. Every trade the learning stack remembers now carries the
market conditions it was born under — `src/regime.py` is the vocabulary
(trend = the proposer's own market_view read; vix_band = low <13 / mid
13–16 / high >16, now the SINGLE source the planner's IV matrix and the
evolution miner share):

- **Capture:** `to_journal_entry` and the simulator's `_entry_for` attach
  `entry["regime"]` at creation (additive key; old entries tolerate).
- **Storage:** `outcomes.regime_trend/regime_vix` (in-place ALTER on
  connect, post_mortem pattern) + the same columns on `simulated_trades`
  (idempotent ALTER in ensure_schema). NULL on pre-feature rows — never
  guessed.
- **Backfill:** `python3 -m src.regime backfill --db <path>` recomputes
  trend AS-OF each historical trade's proposal date from the bars cache
  (the simulator's own no-future-data discipline); vix_band from the
  row's stored vix.
- **Query:** `brain_map.query_similar_events(tags, regime=...)` adds an
  `in_regime` stats block (count/win_rate/avg_r + tag) alongside the
  untouched overall stats — fully backward compatible.
- **Skeptic contract v2:** FEATURE_NAMES += regime_trend/regime_vix_band
  (contract change = retrain by design, decision #44; no model had ever
  shipped, so nothing was invalidated).

**The experiment (the reason this was prioritized):** backfilled all
1,008 scratch trades (2015–2026, zero unknown trends) and retrained.
Result: 5-fold balanced accuracy **0.578 vs 0.594 pre-regime — no
improvement, within noise**. Why, per feature importances: raw `vix`
(0.26) already contains the band (a coarsening of it, 0.027), and the
simulator proposes structures MATCHED to the trend, so trend is nearly
constant within a strategy (0.027). The "regime tags will ship the
skeptic" hypothesis is NOT confirmed for these coarse tags. Gate stays
closed; skeptic keeps abstaining. Next candidates for the 0.60 gate:
features orthogonal to the entry gates (realized vol vs implied,
distance-to-support, day-of-week/expiry-proximity) rather than
re-encodings of inputs the pipeline already filters on.

NOT deployed to the VM yet (observation week): migrations are additive
and auto-apply on the next `git pull` + restart; the production DB's 366
rows backfill with the same CLI when that happens. Tests:
`tests/test_regime.py` (11, offline); suite 521 green.

## ✅ Procedural Evolution — BUILT AND TESTED; NOT YET SCHEDULED (2026-07-09)

`src/evolution.py` closes roadmap item #5: the system studies its own loss
clusters and proposes rule mutations for HUMAN review — it can never apply
anything itself. Pipeline per cluster: mine losses by (underlying ×
strategy × VIX band) with journal_ref provenance → deterministic HER-style
hindsight buckets (bad_risk_parameters / bad_timing / ambiguous) →
counterfactual contrast against the same setup's wins → an Analyst→Critic
→resolution dialectic on LOCAL Ollama (every reply strict-JSON-gated;
unresolved critic BLOCK kills the candidate) → the proposal must come from
the whitelisted `EVOLVABLE_PARAMETERS` registry (VIX gate, risk %, OTM %,
profit-take fraction, pre-expiry days; bounds-checked — the 3B model never
writes code; diffs are generated deterministically) → double backtest via
the Phase 7 simulator (baseline vs `override_parameters`, in-memory DBs,
cached bars) with **RevertOnRegression**: a cluster-fix that degrades
global Sharpe/max-drawdown is discarded. Survivors:
`candidates/evolution_<ts>.md` (4 sections: cluster, dialectic summary,
simulator proof table, unified diff) + a version-tree entry in
`data/evolution_lineage.json` (v1→v2 per parameter; failed attempts are
remembered so future runs know what was tried).

Runs Mac-side only (Ollama; zero API spend — user rule). Backtest bars
come from `data/bars_cache.json`, refreshed THROUGH the VM
(`python3 -m src.evolution --refresh-bars-cache`) since the Mac holds no
live token (decision #48). Wired as sleep-phase Task E with the standard
graceful skip (the VM skips it silently). **Deliberately NOT on any
schedule until the observation-week triage clears it** — run manually.

First live run (2026-07-09): mined 10 real clusters; the worst (13
Bank-Nifty condor losses in the mid-VIX band, Rs.-8.1L) produced an
Analyst proposal that the Critic BLOCKED at the consensus gate — the
adversarial design doing its job. Bug found & fixed during the build:
multi-line python shipped via ssh `--command` gets newline-mangled — both
evolution's bars dump AND the edge miner's apply step now travel as scp'd
FILES (the miner's flaw had never fired: its only prior run had 0 new
edges). Also fixed: a queue-built notifier test hardcoding "today" broke
the suite at midnight. Tests: `tests/test_evolution.py` (14, offline,
scripted fake LLM); suite 500 green.

## ⚠️ Correction (2026-07-09, just after midnight): Mac renew/push crons REMOVED — they raced the VM's token

Discovered by accident: DhanHQ allows only ONE active access token per
client ID — minting a new one silently invalidates the previous token,
even one whose own expiry claim is hours from now. The Mac's 07:00
renewal + 07:10 push (added a few hours earlier the same night as
"deliberate redundancy") meant that on ANY morning where the VM's own
07:00 Secret-Manager renewal happened to land a moment before the Mac's,
the Mac's 07:10 push would overwrite the VM's fresh, valid token with
the Mac's own (now-invalidated-by-the-VM) token — breaking the live
engine's market data for the whole day. **Fixed**: both Mac cron entries
removed. The VM's Secret-Manager renewal is proven reliable on its own
(verified twice); it needs no backup, and the "backup" was actually the
risk. `scripts/push_token_to_vm.sh` stays in the repo as a manual/dev
tool only — never on an automatic schedule again. Decision #48.

## ✅ THE VM IS THE ENGINE — full migration, LIVE AND VERIFIED (2026-07-08 night)

The Mac is no longer required for anything market-hours. Topology
(decision #47):

| Concern | Where | How |
|---|---|---|
| Live session 09:15–15:30 | VM | `src.master_scheduler`, cron 09:10 Mon-Fri |
| Token renewal | VM, 07:00 | `src.renew_token` — V2 creds fetched at runtime from **GCP Secret Manager** (verified live: mints with ZERO V2 keys on VM disk) |
| Paper state (journal/portfolio/brain_map) | VM `data/` | Mac's live state migrated 2026-07-08; VM authoritative |
| Alerts 15:35 / suggestions 08:00 / sleep-phase decay 20:00 / ops sweep 20:30 | VM cron | `scripts/setup_cron.sh` (6 jobs, CRON_TZ=Asia/Kolkata) |
| API gateway + Discord bot + tunnel | VM (unchanged) | systemd, all `Restart=always` |
| Causal edge mining (Ollama, no API spend) | **Mac, opportunistic** | `src/edge_miner.py` via LaunchAgent (login + 21:00): pull VM brain_map → mine locally → apply idempotent edges back → refresh Mac's read copies |
| chat_agent, development | Mac | reads the miner-refreshed local copies |

Key facts for a cold pickup:
- The VM's OAuth **scopes** were upgraded to `cloud-platform` (required a
  stop/start 2026-07-08) — without that, Secret Manager answers 403 even
  with correct IAM. Secrets `dhan-pin`/`dhan-totp-secret`/`dhan-api-key`/
  `dhan-api-secret` live in Secret Manager, granted per-secret to the VM's
  default service account.
- The old `alpha-market-loop.service` is **disabled** (stale pre-6E code);
  the scheduler cron replaced it. Do not re-enable.
- The Mac's crontab retains renew_token 07:00 + push_token_to_vm 07:10 as
  DELIBERATE redundancy: when the Mac is awake it refreshes the VM's token
  too (harmless either order); when asleep, the VM self-renews. Remove any
  time with `crontab -e` if unwanted.
- The Mac's pre-migration state is archived at `data/mac-archive-pre-vm/`
  (created by the miner's first run) and the VM had NO prior data (its
  market loop never journaled — dead token since creation).
- If the Mac stays closed for a week: everything runs except NEW causal
  edges (graph still decays nightly on the VM). Nothing breaks.

## ✅ Phase 7A: Master Scheduler & Live Execution Loop — BUILT AND TESTED (2026-07-08)

`src/master_scheduler.py` (`python3 -m src.master_scheduler`) is the
one-command entry point for a fully automated live paper-trading day.
**Deliberately NOT `src/main.py`** — that name is the Phase 1 alert job the
VM cron runs at 15:35 IST; clobbering it would have silently killed the
alert pipeline.

`run_trading_session()` runs strictly Mon-Fri 09:15–15:30 IST: launched
early it sleeps until the open; launched after the close it exits
immediately (cron-misfire safe); at 15:30 it shuts itself down. During the
window it supervises the two existing live loops as asyncio tasks — ENTRY
(`market_loop.run_market_loop` fed by the Phase 6H live adapter → margin-
gated, PENDING_APPROVAL proposals; decision #11's human-in-the-loop stands,
nothing is auto-approved) and EXIT (`live_bridge.run_live_loop` advisory
profit-take/pre-expiry alerts). Session bookends go to Discord: the 🟢 OPEN
card carries the Phase 6G account snapshot + the Phase 6I planner's
advisory playbook per underlying; the 🔴 CLOSE card the end-of-day account.
Graceful shutdown: SIGINT/SIGTERM set an asyncio.Event, both loops are
cancelled and awaited; state cannot corrupt because every httpx client and
SQLite touch in this codebase is per-call scoped (open-commit-close) — no
long-lived handles exist to strand mid-write. A dying loop brings the
session down safely (never a zombie). `CRON_SETUP.md` (project root)
documents the exact Mac crontab line (09:10 Mon-Fri + Full-Disk-Access and
wake-schedule caveats). Tests: `tests/test_master_scheduler.py` (8 offline
tests with a hand-wound IST clock; suite 463 green). Decision #45.

## 🟡 Phase 7b: Skeptic Trainer — BUILT AND TESTED; MODEL DELIBERATELY NOT SHIPPED (2026-07-08)

`src/train_skeptic.py` (`python3 -m src.train_skeptic [--dry-run|--force]`)
fits the Phase 11 skeptic's Random Forest on `simulated_trades` in the
frozen `FEATURE_NAMES` order (graph slots honestly zero for simulated rows
— the simulator never consults the graph, so backfilling them would be
look-ahead leakage), evaluates on a stratified 25% holdout, and persists
`data/skeptic_model.pkl` + `skeptic_model_meta.json` ONLY above a
`MIN_BALANCED_ACCURACY = 0.60` ship gate (decision #44).

**The honest outcome so far**: the training corpus was grown from 82
VIX-less rows to **366 resolved simulated trades with true VIX** (290 wins
/ 76 losses; NIFTY 50 + NIFTY BANK, 2023-01 → 2026-06) — the simulator CLI
now fetches India VIX history natively (`_fetch_vix_series`, `--no-vix` to
skip) and the 82 legacy NULL-VIX rows were backfilled from real history.
Even so, the forest scores **~0.55 five-fold balanced accuracy — a coin
flip**: the 10 frozen features don't separate wins from losses for
structures that already passed the pipeline's own gates. So the trainer
correctly REFUSES to persist, and the skeptic keeps abstaining (its
designed no-noise behavior). To go live the model needs richer signal:
regime-aware features (pending "Regime-Aware Memory" phase), real graph
context at simulation time, or a feature-contract revision (which means
retraining by design).

## ✅ Phase 6J: Strict Portfolio Realism — BUILT AND TESTED (2026-07-08)

A four-part hardening pass tying the 6G–6I layers into enforced real-world
boundaries (committed as one unit; the user's spec called it "Phase 6H" but
that letter was already the live bridge):

1. **Test-environment webhook muzzle** (`src/notifier.py`) —
   `webhooks_muzzled()` blocks EVERY Discord webhook HTTP request (text path
   `send_discord_message` AND embed path `broadcast_alert`) when
   `IS_TEST_ENV` is truthy or a pytest run is detected
   (`PYTEST_CURRENT_TEST`); muzzled sends are logged locally and report
   False. Webhooks only fire from true live runs. Tests that exercise the
   dispatch machinery itself set `notifier.WEBHOOK_MUZZLE_OVERRIDE = False`
   (autouse fixture in `tests/test_notifier.py`). The simulator needs no
   muzzle — it is source-guarded against importing notifier at all.
2. **Margin gate at trade ACCEPTANCE** (`options_proposer.decide_pending`) —
   approving a pending entry now requests its margin
   (`spread.margin.total_margin × lots`) from the Phase 6G capital layer
   first (idempotent when the headless gate already locked it at proposal
   time). A margin-blocked approval returns a new
   `{"status": "margin_blocked"}` and leaves the entry pending — nothing
   journaled, broadcast, or settled. With the existing run_headless gate,
   every acceptance path now bounds concurrent trades by the Rs.10L pool.
3. **Theoretical plan economics** (`trade_planner.estimate_plan_economics`)
   — every tradeable plan now carries leg premiums (modeled via the
   simulator's synthetic chain — same world the tracker/replay price in),
   `net_credit`/`net_debit`, `spread_width`, and per-lot `max_profit`/
   `max_loss`, so no broadcast can ever show Rs.0 placeholders. Credit
   structures: profit = credit, loss = width − credit; debit structures
   mirror it; identities are test-asserted.
4. **Portfolio snapshot command** (`src/chat_agent.py`) — `@ADiTrader
   portfolio` (exact match after mention-strip) bypasses Ollama entirely:
   `build_portfolio_snapshot()` formats the live Phase 6G account as hard
   numbers — Starting Capital, Free Cash, Locked Margin, Active Trades
   (= active margin locks), Net PnL. Money numbers are never paraphrased
   by an LLM.

Tests: +6 muzzle tests (network-tripwired), +1 decide_pending gate test,
+6 planner economics tests, +4 chat-agent snapshot tests. Suite 443 green.
Decision #43 in `DECISIONS.md`.

## ✅ Phase 6I: Technical-to-Options Strategy Planner (trade_planner) — BUILT AND TESTED (2026-07-08)

`src/trade_planner.py` is a PURE evaluation matrix from a technical market
read to the appropriate defined-risk options structure — zero side effects
(no market data, DB, journal, or network; import-guard tested), fully
deterministic. `map_technical_to_strategy(technical_state)` ingests trend
(explicit, or classified from spot's % distance to the fast/slow SMAs — ±2%
on the slow SMA marks "strong", the fast SMA must agree in sign), IV regime
(explicit, or from VIX: <13 low, 13–16 high, >16 extreme), and optional
support/resistance boundaries. The routing matrix:

- **Range-Bound + High IV → Iron Condor** — shorts at 2% OTM (or tucked
  under support / over resistance when boundaries are supplied), wings
  `WING_STEPS × step` further out. "High" means rich-but-tradeable: above
  VIX 16 the planner returns no_trade, NEVER contradicting the existing
  `strategy.validate_regime` hard gate.
- **Strong Bullish + Low IV → Bull Call Spread** (ATM + wing; rich IV is a
  deliberate no_trade — debit structures want cheap options).
- **Bearish + High IV → Bear Call Spread** (credit sold above resistance);
  **Bearish + Low IV → Bear Put Spread** (the proposer's own structure).
- Everything else (weak bullish, unknowns, panic VIX) → no_trade with a
  rationale.

Output legs are structural specs — side, CE/PE, concrete strike AND offset
from ATM, snapped to the underlying's grid, optimized for Bank Nifty (step
100, lot 35; NIFTY 50 gets 50/75) — consistent with options_proposer's own
geometry so a planned condor is the same condor the headless pipeline
builds. Tests: `tests/test_trade_planner.py` (21 offline tests: full matrix,
classifier boundaries, strike snapping, S/R overrides, purity + import
guard; suite 426 green).

## ✅ Phase 6H: Live Market-Hour Data Adapter (live_bridge) — BUILT AND TESTED (2026-07-08)

`src/live_bridge.py` decouples the pipeline from daily-close replay during
NSE market hours (Mon-Fri 09:15-15:30 IST), via the verified DhanHQ V2
token framework. Two real-time jobs:

- **Entry** — `fetch_live_market_state(underlying)` is a drop-in for
  `market_loop.fetch_market_state` (the loop's documented `fetch_fn=`
  injection seam): it appends the live spot as today's provisional close
  before the same SMA/RSI read the simulator replays
  (`simulator.analysis_from_closes`), so the trend read reacts intraday.
  Same contract: `{"analysis", "vix"}` (+ `"vol_overrides"` from the Phase
  6F bridge), None outside market hours / dead quote / thin history.
- **Exit** — `evaluate_open_positions()` marks every ACTIVE approved open
  spread in the journal against live spots using `plan_tracker`'s own pure
  helpers (`_spread_mark`, the no-arbitrage clamp, the 65% profit take, the
  pre-expiry gamma rule) and returns advisory exit signals hours before the
  tracker's end-of-day sweep. `live_cycle()` snapshots each underlying,
  folds packets into 15-minute `CandleAggregator` OHLC buckets, and fires
  ONE de-duplicated Discord note per (position, signal) via `AlertRegistry`.

Hard sandbox rule (decision #41): the module is READ-ONLY on all trade
state — it never writes journal.jsonl, never settles cash
(`_settle_spread_cash` stays the tracker's exclusive job), never touches
portfolio.json; a live exit signal is an alert to the human, not an
execution (runtime-spy tested). Daemon: `python3 -m src.live_bridge`
(60s cycles, fail-safe — a dead quote feed or Discord outage never kills
the loop). Tests: `tests/test_live_bridge.py` (19 offline packet-playback
tests; suite 405 green).

## ✅ Phase 6G: Capital & Margin Allocation Layer — BUILT AND TESTED (2026-07-08)

`src/portfolio_manager.py` gives the automated options pipeline a dedicated
account profile: a simulated pool of Rs.10,00,000 starting capital living in
`brain_map.db` (four additive tables owned by the module: `account_state`,
`margin_locks`, `equity_curve`, `account_events` — core tables untouched,
same pattern as the simulator's `simulated_trades`). Three strict guards:

- **Margin locking** — when the headless proposer fires an entry signal, the
  structure's SPAN margin (`portfolio.calculate_span_margin` total × lots) is
  digitally locked under the entry's journal `short_id` BEFORE the proposal
  goes out. Locks release when the tracker resolves the trade (realized P&L
  settles into the account) or the human rejects it (zero P&L).
- **Margin exhaustion** — an entry needing more margin than the available
  liquid cash (equity − active locks) is SILENTLY rejected: no journal line,
  no Discord alert, just a `margin_exhaustion` row in `account_events`.
- **Risk of ruin** — the account tracks its equity curve and trailing
  drawdown from a ratcheting peak; once drawdown ≥ the hard-coded 10%
  (`MAX_DRAWDOWN_PCT`), ALL entries are blocked (`risk_of_ruin_halt` logged),
  however affordable, until equity recovers above the line.

Scope rule (decision #40): the gate applies ONLY when `run_headless` trades
the real paper book — a caller-injected `book` (the Phase 7 simulator, every
test, any what-if run) is its own capital world and neither consults nor
touches the real account. The paper cash flow itself is unchanged
(`plan_tracker._settle_spread_cash` still net-settles `portfolio.json`);
margin here is *virtually* blocked, like a real clearing house blocks SPAN.
Fail-safe at the seams: the proposer/tracker call `gate_headless_entry` /
`release_entry`, which never raise — a dead DB prints a note and fails OPEN.
Inspect the account: `python3 -m src.portfolio_manager`. Tests:
`tests/test_portfolio.py` (Phase 6G section — 16 new tests, in-memory DB,
margin boundaries, consecutive-loss drawdown scenarios, halt behavior,
`run_headless` gate integration; suite 386 green).

## ✅ Broadcast Alert Engine + EOD Summary — BUILT AND TESTED (2026-07-08)

`src/notifier.py` gains two new exports:

* **`broadcast_alert(payload: dict)` (async)** — posts a colour-coded Discord
  embed card directly to `DISCORD_WEBHOOK_URL` via httpx using Discord's
  `{"embeds": [...]}` API (not the existing `{"content": "..."}` text path).
  Colour scheme: green = opened/win, orange = closed-neutral, red = stop_loss/loss,
  blue = EOD. Fail-safe: missing webhook, any network error, or httpx absent all
  return False without raising.

* **`fire_broadcast(payload: dict)` (sync bridge)** — dispatches
  `broadcast_alert` from sync calling contexts. Detects whether an event loop is
  running (`asyncio.get_running_loop()`): if yes, schedules a fire-and-forget
  `Task`; if no, calls `asyncio.run()`. Never raises — the trade journal is never
  blocked by a Discord outage.

**Wired into the execution loop at three points:**
- `plan_tracker.run_tracker()` — embed on every equity and spread resolution
  (`"closed"` event for profit-take/pre-expiry/target/time-stop; `"stop_loss"`
  for stop_hit). All inside try/except — existing journal write never blocked.
- `options_proposer.run_session()` — embed when the user types `y` in the
  terminal session (the `"opened"` event fires after `journal.log`).
- `options_proposer.decide_pending()` — embed when the Discord/API bridge or
  `--review-pending` approves a pending entry (same `"opened"` event).

**`src/eod_summary.py`** — new standalone daily broadcaster (run at 15:30 IST /
10:00 UTC): queries `data/journal.jsonl` (today's resolved P&L, active approved
positions) and `data/brain_map.db` (outcomes win/loss count), computes
strategy-level net delta exposure across open spreads, and posts a terse embed
status card via `broadcast_alert`. Run manually: `python3 -m src.eod_summary`.

Cron schedule on VM:
```
0 10 * * 1-5  cd /home/aditya/alpha_trading && \
              ./venv/bin/python3 -m src.eod_summary
```

**Tests**: `tests/test_notifier.py` — 53 new offline tests (pytest-mock
`mocker` fixture, no network). Suite: 317 → 370 tests, all green.
`pytest-mock` added to `requirements.txt`. Decision #39 in `DECISIONS.md`.

## ✅ RESOLVED AND VERIFIED LIVE (2026-07-08): DhanHQ V2 auth refactor

**Fully closed, not just fixed-in-code — confirmed against Dhan's live
API on the Mac.** `src/renew_token.py` is V2-FIRST: with `DHAN_CLIENT_ID`
+ `DHAN_PIN` + `DHAN_TOTP_SECRET` (+ `DHAN_API_KEY`/`DHAN_API_SECRET` app
headers) in `.env`, it computes the current TOTP via `pyotp` and POSTs
`auth.dhan.co/app/generateAccessToken` — minting a **brand-new 24h token
headlessly**, even from a fully dead old token (the exact failure that
forced a manual dashboard paste on 2026-07-07). Without those keys it
falls back to the DEPRECATED legacy `/v2/RenewToken` — that path is what
broke with `DH-905` after DhanHQ's 2025-10-01 auth overhaul. Sources:
[the change notice](https://github.com/marketcalls/openalgo/issues/488),
[DhanHQ v2 auth docs](https://dhanhq.co/docs/v2/authentication/).
`pyotp` added to `requirements.txt`; offline tests in
`tests/test_renew_token.py`.

**Live verification (2026-07-08, Mac)**: after the one-time Dhan-web setup
(API key + secret via the developer console's "API Key" tab; TOTP 2FA
enabled with the plain-text secret captured during enrollment — NOT the
account's general login settings, and NOT re-viewable after the fact, so
disable/re-enable was needed once to see it) and populating `.env`,
`python3 -m src.renew_token` printed **"Token renewed successfully. New
expiry: 2026-07-09T12:24:11"** — a genuine fresh token from Dhan's live
API, headlessly, with no deprecation note. **Phase 7b is now unblocked
for real**: large simulator runs no longer risk the token dying mid-run.

**Still to do**: replicate the same four `.env` keys on the **VM**
(`git pull` + `pip install -r requirements.txt` for `pyotp`, then the
same base64 `.env` transfer trick since these values would otherwise
mangle in the browser SSH terminal) so its 07:00 IST cron renewal also
uses V2 instead of the legacy fallback.

## ✅ Phase 6F: Quantitative Execution Bridge (vol_bridge) — BUILT AND TESTED (2026-07-08)

`src/vol_bridge.py` is a stateless routing module that reads the active
`graph_edges` from `brain_map.db`, computes a signed net-weight signal
(`_net_signal` = Σ polarity × confidence_score over active edges where
polarity is −1/+1/0 from the target node's keywords), and classifies the
macro regime:

- **Expansion** (`net_signal < -0.5`): negative-node weight dominates — the
  knowledge graph's evidence tilts toward losses/bearish outcomes.
- **Contraction** (`net_signal > +0.5`): positive-node weight dominates.
- **Neutral**: neither threshold reached.

Under **Expansion** two defensive modes translate the regime to iron condor
parameters (caller selects via `mode=`):
- `"scale_risk"` (default) — `risk_pct = base × 0.70` (30 % fewer contracts,
  lower max loss per cycle)
- `"widen_wings"` — `short_strike_otm_pct = base × 1.50` (short put moves
  50 % further OTM, widening the tail-risk buffer)

Wired end-to-end:
- `market_loop.fetch_market_state` calls `compute_regime_overrides()` and
  stashes the result as `state["vol_overrides"]`.
- `options_proposer.run_headless` strips `vol_overrides` from state before
  unpacking into `build_proposal`, forwarding `risk_pct` / `short_strike_otm_pct`
  as explicit kwargs.
- `build_proposal` gained two optional kwargs (`risk_pct`, `short_strike_otm_pct`)
  that fall back to the module constants — fully backward-compatible.

Fail-safe throughout: missing DB / empty graph / any exception returns `{}`
so the proposer runs unchanged. Tests: `tests/test_vol_bridge.py` (31 tests,
offline in-memory SQLite, covering polarity classification, net-signal
arithmetic, boundary precision, macro shock scenarios, and the
`run_headless` integration). Decision #38 in `DECISIONS.md`.

## ✅ Phase 6E: Temporal Signal Decay — BUILT AND TESTED (2026-07-08)

`src/decay_engine.py` is a standalone daily sweep that applies exponential
decay to every active `graph_edges` row: `w(t) = w₀·exp(−λ·t)` where `t` is
days since the edge was last written or swept, and `λ` is the per-edge
`decay_lambda` (default 0.05 — matching the Sleep Phase's semantic-node
decay rate). When a decayed weight falls below 0.1 the edge is soft-expired
(`invalid_at` stamped) so `GraphEngine` excludes it from inference; it is
never deleted, so a re-observed pattern (same triple via `add_edge`) reactivates
it automatically (decision #37). Three additive columns were added to
`graph_edges`: `valid_from` (creation/last-sweep timestamp), `invalid_at`
(expiry marker, NULL = active), `decay_lambda` (per-edge rate). `add_edge`
now stamps `valid_from = now` and clears `invalid_at` on both first write and
reinforce. `GraphEngine.__init__` loads only `WHERE invalid_at IS NULL`.
Migration is idempotent — existing DBs are upgraded in place on next connect.
Run manually: `python3 -m src.decay_engine`. Tests: `tests/test_decay.py`
(22 tests, all offline). **No network I/O, no market data** (decision #30 holds).

## Current production state (as of 2026-07-06)

- **Phases 1-4 (alerting, suggestions, paper trading, journal/plans/tracking/
  news/forecast/tuner) are feature-complete.**
- **Phase 5 (frontend + local API) is live**: unified FastAPI backend
  (`src/api.py`), a React dashboard (`lovable-frontend/`, Supabase-free),
  direct Gemini integration (no cloud AI gateway), an hourly auto-sync loop,
  and a Discord analyst bot (`src/discord_bot.py`).
- **Market data has been fully migrated from yfinance to the DhanHQ Data
  API** (`src/dhan_client.py`). This is the single source of prices/OHLC for
  the whole engine now.
- **The backend is deployed to a fresh GCP VM (2026-07-06)** running the
  DhanHQ-backed FastAPI server continuously as a systemd service — see
  "GCP VM (cloud hosting)" below. The old cron VM is superseded.
- **Phase Operational — DONE (2026-07-06):** `scripts/setup_cron.sh` deploys
  the token-renewal (`src.renew_token`, 07:00 IST) and email-digest
  (`src.main` 15:35 IST, `src.suggest` 08:00 IST) cron schedules on the VM,
  closing the "known gap" that used to be documented here. `src/api.py`
  also now runs a `_poll_watchlist_loop` background task (60s cadence,
  `asyncio.to_thread` for the blocking DhanHQ/analysis calls) that
  deduplicates rule breaches per-day and fires `src.notifier.send_digest`
  email alerts directly from the live server, independent of the hourly
  auto-sync loop.
- **Phase 5 (Options) — COMPLETE (2026-07-06), both parts.**
  *Part A (frictions)*: `src/portfolio.py` applies the full 2026 cost
  stack per executed leg — STT 0.15% (sell side ONLY), Stamp Duty 0.003%
  (buy side only), flat ₹20 brokerage, NSE exchange charges (0.00345%),
  SEBI turnover fees (0.0001%), and 18% GST on the service charges — plus
  `calculate_span_margin()`, a SPAN simulation with hedge offsets (a
  defined-risk spread blocks only its net risk, a naked short gets the
  punitive treatment). `src/plan_tracker.py` applies dynamic bid-ask
  slippage on resolution (0.05% index; 0.1%-0.5% options by liquidity;
  0% stocks).
  *Part B (spreads)*: `strategy.StrategyConstructor` builds defined-risk
  structures ONLY (bull call / bear put verticals, iron condor / iron
  butterfly — zero naked legs by construction), gated by India VIX
  (range-bound strategies strictly blocked when VIX > 16 *or* VIX is
  unavailable) and sized by ABSOLUTE MAX LOSS, capped by SPAN margin vs
  cash. India VIX lives in `dhan_client` (`get_india_vix()`, security id
  21 verified against Dhan's scrip master). The tracker resolves spreads
  as ATOMIC BASKETS (no per-leg exit path exists — the SPAN-spike
  sequencing bug is structurally impossible) with auto-exit at 65% of max
  profit or strictly 2 days before expiry (gamma rule), modeled P&L
  clamped to the structure's defined-risk bounds, and net-of-frictions
  journaling. The proposal wiring is `src/options_proposer.py`
  (`python3 -m src.options_proposer`, terminal, human-in-the-loop):
  trend read via suggestions.analyze -> India VIX + real Dhan option
  chain -> regime-matched spread (bullish: bull call; bearish: bear put;
  neutral: iron condor, VIX-gated) -> sized by the dedicated
  `options_risk_per_trade_pct` budget (config.json, 10% — decision #28)
  -> approve/reject + why -> journal entry the tracker resolves.
  **Discord-surfaced (2026-07-06)**: the moment a proposal is built, a
  rich 🚨 PROPOSAL ALERT (regime/VIX, legs in a code block, economics
  incl. max loss + SPAN margin, action-required note) fires to Discord
  BEFORE the terminal pauses for y/n, and a short ✅/❌ decision
  follow-up after — both fail-safe, an unreachable Discord never blocks
  the session. Dashboard surfacing still open.
- **Discord connectivity dry run**: `python3 -m src.plan_tracker
  --mock-trade-strategy IRON_BUTTERFLY` pushes a synthetic [MOCK] Trade
  Episode through the real notifier path (nothing journaled; exit code 0
  only if Discord actually accepted it). Needs `DISCORD_WEBHOOK_URL` in
  `.env`. The options proposer also pushes a "Spread proposed" message on
  every journaled decision.
- **Phase 10B extractor BUILT (2026-07-06)**: `src/local_parser.py` —
  `LocalExtractor` (OpenAI-compat calls to local Ollama only,
  `OLLAMA_BASE_URL`/`OLLAMA_MODEL` in `.env`, defaults
  `http://localhost:11434/v1` / `llama3`), `extract_event_json()` (strict
  EEF JSON with schema coercion), and `process_unstructured_input(conn,
  text)` writing idempotently into the Brain Map `events` table
  (`brain_map.py` itself untouched and still network-free). Fully
  fail-safe; guardrail test enforces zero market-data imports (decision
  #30). **Ollama IS installed on the host with `llama3` pulled
  (confirmed 2026-07-06)** — the parser is live-capable; offline tests
  stay mocked regardless.
- **Phase 10B "Sleep Phase" BUILT (2026-07-06)** — `src/sleep_phase.py`
  (`python3 -m src.sleep_phase`, run off-market hours / cron it): three
  sequential fail-safe tasks against `data/brain_map.db`. (A) *Ingestion*:
  journal free text (signal + "why") -> EEF events via the local parser,
  hash-deduped in a new `ingest_log` table holding provenance pointers
  (journal_ref) back to the source rows; failures aren't logged so they
  retry when Ollama is back. (B) *Consolidation*: last-24h events -> ONE
  Ollama call clustering themes into `semantic_nodes` (confidence 1.0)
  with `semantic_event_link` graph edges; re-observed themes are
  reinforced (confidence reset, reactivated) instead of duplicated.
  (C) *Decay*: `score_new = score * e^(-λ·Δt)` anchored on
  last-reinforced/last-decayed so repeat runs never double-count days;
  below 0.20 the node is flagged `active=0` (never deleted). Knobs are
  optional `config.json` keys (`sleep_decay_lambda` 0.05,
  `sleep_prune_threshold` 0.20, `sleep_consolidation_hours` 24). The three
  new tables are created and owned by `sleep_phase.py` — `brain_map.py`'s
  core schema stays untouched. Decision #30 holds: no market data, no
  trading, local Ollama only. **Cron automation DONE (2026-07-06)**:
  `scripts/setup_cron.sh` entry #4 schedules it daily at 20:00 IST
  (`CRON_TZ=Asia/Kolkata` pins IST on Linux), logging to
  `logs/sleep_phase.log`. ⚠️ Placement note: the sleep phase only does
  real work on the machine holding `data/journal.jsonl`,
  `data/brain_map.db` AND Ollama (currently the Mac — the VM deploy
  excludes `data/` and can't run llama3 on an e2-micro); elsewhere it
  degrades to a harmless decay-only pass.
- **Market loop + headless proposals BUILT (2026-07-06)**:
  `src/market_loop.py` (`python3 -m src.market_loop`) is an async daemon
  that polls NIFTY 50 / NIFTY BANK every 15 min during NSE hours
  (Mon-Fri 09:15-15:30 IST; sleeps otherwise) via the abstract
  `fetch_market_state()` seam (pure-Python indicators + VIX — the exact
  injection point for the Phase 7 simulator), and on a favorable setup
  triggers `options_proposer.run_headless()`: 🚨 Discord alert + journal
  entry with decision `pending_approval`, NO terminal pause. Per-index
  2h cool-down stops Discord spam; blocked/no-signal cycles don't burn
  it. Pending entries are tracked hypothetically like rejected ones
  (user's call — see decision #31); decide them any time with
  `python3 -m src.options_proposer --review-pending` (reads the stored
  spread payload from the journal, NO market data fetched: y -> approved
  on paper, tracker takes over; n -> rejected + why; entries the tracker
  already resolved hypothetically are left alone — no hindsight
  approvals). One bad cycle never kills the loop.
- **Discord approval buttons — DONE (2026-07-07):** `/pending` in Discord
  lists every PENDING_APPROVAL proposal with tappable ✅ Approve / ❌
  Reject buttons (persistent across bot restarts — the trade_id round-trips
  through the component custom_id via `discord.ui.DynamicItem`); each tap
  opens a one-line "why" prompt, then POSTs to the gateway's
  `POST /api/discord/action` with the `x-api-key` — the bot never touches
  the journal or engine modules itself (its read-only guardrail holds; the
  gateway owns the mutation). New read side: `GET /api/discord/pending` on
  `src/api_server.py`. The bot reads `BRIDGE_BASE_URL` (default
  `http://127.0.0.1:8000` — correct when it runs on the same VM as the
  gateway, which also makes the quick-tunnel URL irrelevant for approvals).
  Tests: `tests/test_discord_buttons.py` + pending-list tests in
  `tests/test_api_server.py`.
- **Phase 11 scaffolding: Random Forest Skeptic Agent — BUILT (2026-07-07),
  model untrained by design:** `src/skeptic_agent.py` (`RandomForestAuditor`)
  merges the knowledge graph's 2-hop evidence (edge count, cumulative/avg
  confidence, Brain-Map avg R for the active tags) with the proposal's
  market numbers (VIX, signed net premium, spread width, days to expiry,
  max loss/lot, lots) into the frozen `FEATURE_NAMES` vector, and — once
  the Phase 7 simulator trains and saves `data/skeptic_model.pkl` — scores
  P(win) with a Random Forest. Wired into `options_proposer` right before
  the alert is formatted: below 0.40 a strictly formatted "⚠️ Skeptic
  Agent Warning" rides in the Discord PROPOSAL ALERT. Until a trained
  model exists it ABSTAINS silently (decision #35 — no fake warnings from
  an untrained forest), sklearn loads lazily only when a model file is
  present, and every failure abstains rather than blocking a proposal.
  Advisory only, never gates. `scikit-learn` added to `requirements.txt`.
  Tests: `tests/test_skeptic_agent.py` + proposer integration tests.
- **Phase 7 Time-Travel Simulator — BUILT AND VALIDATED END-TO-END ON REAL
  DATA (2026-07-07):** `src/simulator.py`
  (`python3 -m src.simulator --start YYYY-MM-DD --end YYYY-MM-DD`) replays
  history through the REAL pipeline: as-of-date SMA/RSI analysis (no future
  data ever enters a proposal), historical VIX, a synthetic option chain,
  the actual `build_proposal()` logic, auto-approve, then resolution via
  `plan_tracker`'s own pure helpers — 65% profit take, pre-expiry gamma
  rule, and the FULL 2026 friction stack, byte-identical to live. Results
  land idempotently (deterministic `sim:` journal_refs) in the additive
  `simulated_trades` table + standard `outcomes`/`events`/links, and
  `encode_causal_links` runs the Sleep Phase's Task D over the simulated
  window so graph_edges mint from simulated post-mortems exactly like real
  ones (decision #36). The real journal/portfolio are never touched; no
  notifier/network imports (both guard-tested).
  **Live validation run (2026-07-07, real DhanHQ history, NIFTY 50,
  2025-07-01 → 2026-06-30, 56 trading days scanned):** 56 iron-condor
  proposals, 56/56 resolved — **48 wins (avg +Rs.140,532, avg R +1.43)**,
  **8 losses (avg −Rs.76,802, avg R −0.78)**, 0 scratches; `brain_map.db`
  went from empty to 182 events / 56 outcomes / 168 links; the causal
  writer minted the graph's first two real edges,
  `iron_condor RESULTS_IN win` and `iron_condor RESULTS_IN loss` (both
  confidence 1.0) — the Phase 6C/6D memory stack now has real content for
  the first time. **Phase 7 is officially validated, not just built.**
  Also fixed in passing: spread outcomes now record their strategy as the
  Brain Map `archetype`
  ("iron_condor", not "other"), so causal summaries name the trade for
  real trades too. Tests: `tests/test_simulator.py`.
- **Full offline test suite: 244/244 passing** (`python3 -m pytest tests/`;
  the `for f in tests/test_*.py; do python3 "$f"; done` __main__ loop runs
  all 23 files clean too), including `tests/test_options_spreads.py`
  (condor max-loss math, STT sell-side-only, VIX gate, atomic tracker
  resolution), `tests/test_options_proposer.py` (regime mapping,
  strike selection off a fake chain, budget sizing, journal contract),
  `tests/test_api_server.py` (Phase 9 gateway auth + Discord bridge),
  `tests/test_graph_engine.py` (Phase 6C 2-hop BFS + confidence sorting),
  and `tests/test_causal_writer.py` (Phase 6D triple extraction + decision
  #34 sourcing).
- **Discord episodic encoder — DONE (2026-07-06):** `src/discord_client.py`
  (async `httpx` webhook client, `DISCORD_WEBHOOK_URL` in `.env`, optional
  `thread_id` grouping, fully fail-safe) + `notifier.send_discord_message()`.
  The API's poll loop pushes watchlist alerts to Discord alongside email,
  and the hourly auto-sync loop pushes a structured "Trade Episode"
  (market sentiment + prices + rule that fired) for every resolution —
  built by the pure `brain_map.build_episode_snapshot()` and handed out of
  the sync tracker via `run_tracker(on_episode=...)`, so the Brain Map
  itself still does zero network I/O (decision #25's additive rule holds).
- **Discord delivery VERIFIED LIVE end-to-end (2026-07-06)**: a real
  webhook was created on the "Alpha Trading" Discord server (#general),
  `DISCORD_WEBHOOK_URL` set in `.env` both locally and on the VM (via the
  base64-paste method below), and confirmed working by two live sends —
  a plain connectivity ping and the `--mock-trade-strategy` dry run — both
  landing in #general with `Discord delivery: OK`. The VM's systemd
  service was restarted afterward and came up clean
  (`systemctl status alpha-trading` → `active (running)`, both background
  loops armed), so live watchlist alerts and real resolved-trade episodes
  now push to Discord in production, not just locally.
- **Phase 9 Public API Gateway & Discord Bridge — DONE (2026-07-07):** `src/api_server.py` implements a strict fail-closed API-key gateway (requiring `X-API-Key` or `Authorization: Bearer` token) that wraps the `src.api` FastAPI app. It also hosts the two-way Discord bridge endpoint `POST /api/discord/action` to securely decide pending approvals directly from phone notifications/Discord webhook callbacks. Tested and verified offline via `tests/test_api_server.py`.
- **Phase 6C Knowledge Graph Reasoning Layer — DONE (reader; 2026-07-07):**
  `src/graph_engine.py` — a `GraphEngine` that loads the additive
  `graph_edges` table (`source_node, relation, target_node,
  confidence_score`) from `data/brain_map.db` into a `networkx.DiGraph`
  once at construction, then answers `get_relevant_context(node,
  max_hops=2)` — a BFS to depth 2 returning linked edges sorted by
  confidence — purely from memory. Strictly READ-ONLY, never writes during
  inference (decision #33). Wired into `src/options_proposer.py`: each
  proposal runs a fail-safe "Memory Query" on its ticker and appends a 🧠
  Memory block to the Discord PROPOSAL ALERT rationale (advisory only —
  no rule/score change, decision #26 philosophy). Additive: `brain_map.py`
  untouched; SQLite stays the only persistent store, `networkx` is just the
  in-memory reasoning layer (no new DB). Tests: `tests/test_graph_engine.py`
  (+ proposer memory-block tests). `networkx` was added to
  `requirements.txt`.
- **Phase 6D Causal Triple Writer — DONE (2026-07-07):** the Sleep Phase now
  WRITES the graph. `src/sleep_phase.py` gained Task D `write_causal_links`
  (the pass is now A→B→C→**D**): it reads reviewed trades from the
  `outcomes` table (with their `src/analyst.py` post-mortems), calls the new
  `local_parser.LocalExtractor.extract_causal_triples()` — which mines
  `(subject)-[predicate]->(object)` triples, predicate ∈ RESULTS_IN /
  PRECEDES / INDICATES / CONTRADICTS — and writes each into `graph_edges` at
  confidence 1.0, idempotently (a `UNIQUE(source, relation, target)` upsert;
  a new nullable `context` column preserves the "when VIX > 20" qualifier).
  **Sourced ONLY from reviewed outcomes, never raw news sentiment
  (decision #34)** — with no resolved trades it makes no LLM call at all.
  The proposer's Memory Query now seeds on ticker + view + **strategy**, so
  these concept-keyed causal edges actually surface in the Discord PROPOSAL
  ALERT. Tests: `tests/test_causal_writer.py`. Live effect appears once the
  first trades resolve and a Sleep Phase runs with Ollama up.

## Credentials & environment variables

All secrets live in `.env` (repo root, git-ignored — `.env.example` is the
safe versioned template). Load pattern used everywhere: a self-contained
reader in each entry point (`_load_env()`), not a shared library, by design
(modularity — see `DECISIONS.md`).

| Variable | Purpose | Notes |
|---|---|---|
| `DHAN_CLIENT_ID` | DhanHQ account id | `1109738713` as of this writing |
| `DHAN_ACCESS_TOKEN` | DhanHQ Data API token | **Short-lived (~24h)**, auto-minted daily by `python3 -m src.renew_token`. V2 flow (post Oct-2025 overhaul) needs `DHAN_PIN` + `DHAN_TOTP_SECRET` (+ `DHAN_API_KEY`/`DHAN_API_SECRET`) in `.env` — see the "✅ RESOLVED" block at the top of this file for the one-time Dhan-web setup. Without those keys it falls back to the deprecated legacy renewal (expect `DH-905` + manual pastes). |
| `DHAN_PIN` / `DHAN_TOTP_SECRET` / `DHAN_API_KEY` / `DHAN_API_SECRET` | DhanHQ V2 headless auth (daily token minting) | PIN = the Dhan login PIN. API key + secret: `developer.dhanhq.co/live-environment` → "API Key" tab (not "Access Token") → name an app, any placeholder `https://` URL works for Redirection (never actually used by our headless flow) → Generate. TOTP secret: **on that same "API Key" tab**, enable TOTP — the plain-text secret is shown only once at enrollment, so copy it immediately; if missed, Disable then re-enable to see a fresh one (confirm the re-enrollment code with `python3 -c "import pyotp; print(pyotp.TOTP('SECRET').now())"`, no phone app needed). Needed on BOTH the Mac and the VM. |
| `GEMINI_API_KEY` | Google Gemini (news sentiment + chat) | Get from Google AI Studio, create the key against the *existing billed* `alpha-trading-app-2026` GCP project (a key from AI Studio's "new project" flow gets zero free-tier quota — see `DECISIONS.md`). |
| `DISCORD_BOT_TOKEN` | Discord bot login | From the Discord Developer Portal, needs "Message Content Intent" enabled. |
| `DISCORD_WEBHOOK_URL` | Discord channel webhook (alerts + trade episodes push) | **Set and verified live 2026-07-06**, both locally and on the VM. Different thing from the bot token above — a channel gear icon → Integrations → Webhooks → New Webhook → Copy Webhook URL. Pushes to the "Alpha Trading" server's #general channel. Verify anytime with `python3 -m src.plan_tracker --mock-trade-strategy IRON_BUTTERFLY` (prints `Discord delivery: OK`/`FAILED`, journals nothing). |
| `ALERT_EMAIL_FROM` / `ALERT_EMAIL_APP_PASSWORD` / `ALERT_EMAIL_TO` | Gmail SMTP for alert/suggestion/session digests | App Password (16-char), not the normal Gmail password. |

`lovable-frontend/.env` (separate, its own git-ignore inside that folder)
needs only `VITE_API_BASE_URL="http://localhost:8000"` — no Supabase keys
(stripped 2026-07-06).

## Boot commands

```bash
# 1. Python engine dependencies (from repo root)
python3 -m pip install -r requirements.txt

# 2. The unified local API (serves the dashboard + all /api/* routes)
# Run the raw server (no key required, localhost dev):
uvicorn src.api:app --reload --port 8000
# Or run the strict API-key gateway (Phase 9 public exposure mode):
uvicorn src.api_server:app --reload --port 8000

# 3. The React dashboard (separate terminal)
cd lovable-frontend && npm install && npm run dev   # localhost:8080 (falls back :8081)

# 4. The Discord analyst bot (separate terminal, optional)
python3 -m src.discord_bot

# 5. Interactive paper-trading session (terminal, when you want to trade)
python3 -m src.trade

# 5b. Options spread proposer (terminal; needs a valid Dhan token for the
#     live chain/VIX — proposes ONE defined-risk spread, you approve/reject)
python3 -m src.options_proposer            # NIFTY 50
python3 -m src.options_proposer "NIFTY BANK"
python3 -m src.options_proposer --review-pending   # decide market-loop
                                                   # PENDING_APPROVAL entries
                                                   # (offline, no market data)

# 6. Offline test suite (no internet/API calls needed)
python3 -m pytest tests/                          # expect 244 passing

# 7. Market loop daemon (market hours only; headless proposals to Discord)
python3 -m src.market_loop

# 8. Discord connectivity check (needs DISCORD_WEBHOOK_URL set; journals nothing)
python3 -m src.plan_tracker --mock-trade-strategy IRON_BUTTERFLY

# 9. Public gateway (Phase 9 exposure mode — strict x-api-key, wraps src.api)
uvicorn src.api_server:app --host 127.0.0.1 --port 8000
```

Manual/on-demand engine scripts (not on a schedule locally — only via VM cron
or run by hand): `python3 -m src.main` (alerts), `python3 -m src.suggest`
(suggestions), `python3 -m src.news_processor` (refresh news sentiment),
`python3 -m src.forecast` (print forecasts), `python3 -m src.tuner` (refresh
learned weights), `python3 -m src.plan_tracker` (manual resolve sweep — also
runs automatically at the start of every `src.trade` session and every hour
inside `src.api`), `python3 -m src.review` (7-day scorecard for pre-plan
entries).

## GCP VM (cloud hosting)

**Rebuilt from scratch 2026-07-06.** The original cron VM (project
`alpha-trading-app-2026`) had a lost login and is abandoned; a new VM was
created and now runs the current DhanHQ FastAPI backend.

- **VM**: `alpha-trading-vm`, project `project-37632031-10d0-47dd-b6f`
  ("My First Project", org `adigupta1998-org`), zone `us-central1-a`, machine
  type `e2-micro`, Debian 13 (trixie), Python 3.13. Billing has ₹28,321
  free-trial credit expiring 2026-10-01.
- **External IP**: `35.239.254.99` — ⚠️ *ephemeral*, can change if the VM is
  stopped/started. Reserve a static IP before relying on it externally.
- **SSH**: GCP Console → Compute Engine → VM instances → **SSH** button
  (browser terminal, no key files). `gcloud compute ssh` also works if the
  gcloud CLI is configured locally, but it is not set up as of this writing.
- **Code lives at** `~/alpha_trading` on the VM, cloned from GitHub (`main`),
  with a Python venv at `~/alpha_trading/venv`.
- **Runtime**: the unified FastAPI API (`src.api:app`) runs continuously on
  port 8000 as a **systemd service** named `alpha-trading`
  (`/etc/systemd/system/alpha-trading.service`): `Restart=always`, enabled on
  boot. This includes the built-in hourly auto-sync loop. Health check:
  `http://localhost:8000/api/health` → `{"status":"ok","mode":"paper-only"}`.

  ```bash
  # deploy an update (on the VM)
  cd ~/alpha_trading && git pull && venv/bin/pip install -r requirements.txt
  sudo systemctl restart alpha-trading

  # operate
  systemctl status alpha-trading          # is it running?
  sudo journalctl -u alpha-trading -f      # live logs (Ctrl+C to exit)
  sudo systemctl restart|stop alpha-trading
  ```

- **`.env` on the VM** is NOT in git and must be transferred by hand. ⚠️
  **Do not paste the DhanHQ JWT directly into the browser SSH terminal** — a
  secret-scanner silently replaces the `eyJ...` token with bullet characters,
  causing `'latin-1' codec can't encode` errors at runtime. Working method:
  on the Mac, `base64`-encode `.env` and pipe a decode command to the
  clipboard, then paste that (the base64 blob isn't recognized as a token, so
  it survives):
  ```bash
  # on the Mac (fills clipboard with a ready-to-run command):
  printf 'echo %s | base64 -d > ~/alpha_trading/.env && echo OK\n' \
    "$(base64 < ~/Documents/Claude/alpha_trading/.env | tr -d '\n')" | pbcopy
  # then paste into the VM SSH window + Enter, then restart the service.
  ```
  Because `DHAN_ACCESS_TOKEN` is short-lived (~24h), keep it alive with the
  auto-renewal script instead of daily manual pastes: after ONE manual seed
  of a valid token, schedule `python3 -m src.renew_token` on the VM
  (`crontab -e`, e.g. `0 6 * * * cd ~/alpha_trading && venv/bin/python -m
  src.renew_token >> logs/renew_token.log 2>&1`). The manual base64 paste
  above is then only needed if a renewal window is missed and the token
  dies (script prints CRITICAL).
- **No firewall port is ever opened — inbound goes through a Cloudflare
  Tunnel only** (Phase 9, decision #32) — **LIVE end-to-end 2026-07-07**:
  port 8000 is reachable only on the VM itself, bound to `127.0.0.1`
  (`alpha-trading.service`'s `ExecStart` now runs
  `uvicorn src.api_server:app --host 127.0.0.1 --port 8000`, the strict
  gateway wrapping the full `src.api` app + the two-way Discord bridge
  `POST /api/discord/action`). `cloudflared` is installed and runs as its
  own systemd service, `cloudflared-tunnel` (`ExecStart=<cloudflared path>
  tunnel --url http://localhost:8000`, `Restart=always`, enabled on boot,
  `Requires=alpha-trading.service`), dialing OUT to Cloudflare and
  forwarding public HTTPS traffic in. The gateway is fail-closed: every
  request needs an `x-api-key` header matching `.env`'s `API_KEY` (401
  otherwise), and it refuses everything with 503 if `API_KEY` is unset —
  only `GET /api/health` stays public. Verified live from an outside
  network (not just VM loopback): `GET /api/health` → 200, and
  `POST /api/discord/action` with a real key and a bogus `trade_id` → 404
  (proving the full chain: Cloudflare edge → tunnel → gateway auth →
  `options_proposer.decide_pending` → journal lookup).
  ⚠️ **This is a "quick tunnel"** (no Cloudflare account/domain needed) —
  free and fast to stand up, but the public URL is **randomly regenerated
  on every restart** of `cloudflared-tunnel` (crash, VM reboot). Fetch the
  current one anytime with:
  `sudo journalctl -u cloudflared-tunnel --no-pager | grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' | tail -1`
  For a permanent, never-changing URL (needed before hardcoding it into a
  Discord bot integration), upgrade to a **named tunnel** — requires adding
  a domain to a Cloudflare account (`cloudflared tunnel create` +
  `tunnel route dns`). Not done — deferred until a domain is available.
- **Scheduled jobs**: `scripts/setup_cron.sh` (idempotent, safe to re-run
  after every `git pull`) installs the full cron block — `src.renew_token`
  07:00 IST daily, `src.main` 15:35 IST Mon-Fri, `src.suggest` 08:00 IST
  Mon-Fri, and `src.sleep_phase` 20:00 IST daily — each logging to
  `logs/<name>.log`, pinned to IST via `CRON_TZ=Asia/Kolkata`. Run it on
  the VM with `bash ~/alpha_trading/scripts/setup_cron.sh`; note the sleep
  phase only does real work where `data/` + Ollama live (see the Phase 10B
  bullet above).
- `data/`, `tests/`, `logs/` are not part of the deploy (paper-trading state
  stays local only; see `OVERVIEW.md`). `config.json` and `.env` are required
  — `src/config.py` fails loudly at import without `config.json`, and
  `src/dhan_client.py` needs `.env`'s Dhan keys.

## Watchlist (current)

10 tickers in `config/watchlist.yaml`, each with `percent_up`/`percent_down`
alert rules at 3%: `HDFCBANK.NS`, `ICICIBANK.NS`, `TCS.NS`, `INFY.NS`,
`RELIANCE.NS`, `ONGC.NS`, `HINDUNILVR.NS`, `ITC.NS`, `MARUTI.NS`, `TMPV.NS`.
All 10 are present in `src/dhan_client.py`'s `SECURITY_ID_MAP` — a ticker not
in that map cannot be priced by the current data layer.

## Live paper-trading data (IMPORTANT — do not reset)

`data/journal.jsonl` and `data/portfolio.json` are git-ignored and hold real
(paper) user activity: an original ONGC.NS buy (2026-07-03) plus several
2026-07-06 dashboard test trades (TCS/MARUTI/ONGC) made by clicking the
frontend's seeded demo proposal cards — kept intentionally, per the user.
Note those demo trades used bare tickers (`TCS`, not `TCS.NS`); resolving
them correctly depends on `dhan_client`'s alias resolution.
**Never reset these files.** When testing anything that writes to them, back
up first and restore after (or point at an isolated temp dataset) — this is
the working pattern used throughout this project's history.

## Next steps / roadmap

**Phase 6 (Brain Map) steps 1–2 landed 2026-07-06**: `src/brain_map.py`
(native `sqlite3` store at `data/brain_map.db` — `events`, `outcomes`,
`event_outcome_link` tables, record/link helpers, and
`query_similar_events(tags)` returning `{count, win_rate, avg_r_multiple,
examples}`) plus `tests/test_brain_map.py` (offline in-memory tests). The
design remains banked in `DECISIONS.md` → "Phase 6 — Brain Map design".

**Phase 6 steps 3–4 landed later on 2026-07-06**: new journal entries now
carry a stable `short_id` (8-char uuid hex, `src/journal.py` — older lines
without one are fine, readers fall back to a composite
`date|ticker|action|price` key via `brain_map.journal_ref_for()`), and
`ingest_existing()` in `src/brain_map.py` idempotently seeds the map from
resolved `journal.jsonl` trades and `data/news_sentiment.json`. Run it any
time with `python3 -m src.brain_map ingest` (re-running is safe and picks
up newly resolved trades). The real `data/brain_map.db` now exists,
holding 10 news events; 0 outcomes so far because no journal trade has
resolved yet. Full suite: **55/55**.

**Phase 6 step 5 (the final step) landed later on 2026-07-06 — PHASE 6 IS
COMPLETE.** `forecast.py` now queries the map: when the current setup has
active pattern tags (fresh Golden Cross → `fresh_cross`+`golden_cross`,
oversold RSI → `rsi_oversold`), the forecast payload gains `memory` stats
and a `memory_context` line ("Historical Performance for active patterns
[...]: Win Rate: X%, ...") that `describe()` prints (terminal + Discord
`/analyze`). Advisory only — zero score points (decision #26 in
`DECISIONS.md`); empty/missing DB degrades to `memory: null` with the
standard flow untouched. `tuner.py`/`brain_weights.json` were never
modified. Suite: **63/63**. Contract addition documented in
`DATA_CONTRACT.md` § 2.4.

**Phase 6 core loop also landed 2026-07-06 (after step 5)** — the
feedback loop is now fully automatic. The moment `plan_tracker` resolves
a plan it (a) captures the original thesis + realized execution metrics,
(b) asks the new post-mortem analyst (`src/analyst.py`, Gemini,
never-raises) for a structured `{variance_analysis, unexpected_variables,
future_guardrails}` JSON, and (c) writes outcome + events + post-mortem
into the Brain Map keyed by the entry's `short_id`
(`brain_map.record_resolved_entry`, shared with `ingest_existing`). The
`outcomes` table gained a `post_mortem` column (auto-migrated in place on
connect). All fail-safe: no Gemini key / locked DB just prints a note,
journal resolution is never blocked. Suite: **71/71**.

**Ongoing Brain Map operation**: nothing manual needed anymore — resolved
trades flow in live via the tracker. `python3 -m src.brain_map ingest`
remains available as a backfill/repair sweep (it won't have post-mortems,
which only generate at live resolution). `memory_context` lines appear in
forecasts once the first trades resolve.

**Phase 9 backend landed 2026-07-07, and the VM exposure is now LIVE**:
`src/api_server.py` is the strict public gateway (fail-closed API-key auth
on every route, wraps the full `src.api` app) with the two-way Discord
bridge `POST /api/discord/action` — approve/reject a `pending_approval`
journal entry by its `short_id`, exactly the `--review-pending` semantics
(`options_proposer.decide_pending`). Tests: `tests/test_api_server.py`. On
the VM: `alpha-trading.service` now runs `src.api_server:app` on
`127.0.0.1:8000`, and `cloudflared` runs as its own systemd service
(`cloudflared-tunnel`) forwarding a public quick-tunnel URL to it — see the
GCP VM section above for the exact setup and the "URL changes on restart"
caveat. Verified end-to-end from an outside network: health check and the
Discord bridge both round-trip correctly through the tunnel.

**Discord approval buttons landed later on 2026-07-07** (see the bullet in
"Current production state"): `/pending` + persistent Approve/Reject buttons
in the bot, `GET /api/discord/pending` on the gateway. For the phone flow
to be fully hands-off, the bot (`python3 -m src.discord_bot`) and the
market loop (`python3 -m src.market_loop`) need to run continuously on the
VM (systemd services, same pattern as `alpha-trading`) — note the pending
entries then live in the VM's own `data/journal.jsonl`, a separate file
from the Mac's local journal.

**Next up, in priority order**: (1) ~~the DhanHQ V2 auth refactor~~ ✅
DONE AND VERIFIED LIVE on the Mac 2026-07-08 — see the "✅ RESOLVED" block
at the top; only replicating the same `.env` keys on the **VM** remains
(so its cron renewal also uses V2); (2) training the skeptic model on
simulated trades (Phase 7b, now genuinely unblocked); (3) upgrading to a
named Cloudflare tunnel for a permanent URL (needs a domain); (4) analyst
procedural evolution (see `DECISIONS.md` → "Still open"). The VM's
scheduled jobs are handled by `scripts/setup_cron.sh`
(see the GCP VM section).

## Where to look for more detail

- **Deep phase-by-phase build history** (what was built, when, and how it
  was verified) lived in this file through 2026-07-06 and has moved to git
  history / commit messages — `git log --oneline` and the commit bodies are
  the detailed record now. This file stays a lean cold-start brief going
  forward, per the user's instruction not to bloat it on every change.
- **Phase 4's step-by-step plan** (4A-4F): `PLAN.md`.
- **The Phase 5+ vision** (Discord, Brain Map, simulator, event ingestion):
  `VISION_PLAN.md`.
- **Frontend JSON contracts**: `DATA_CONTRACT.md`.

---
## 🚀 The Master Execution Plan (Current Targets)
(Note: Do not execute these until explicitly prompted by the user)

### Phase Operational: Fix VM Gaps & Token Automation — ✅ DONE (2026-07-06)
* ~~Create `scripts/setup_cron.sh` to schedule `src.renew_token` at 07:00 AM IST.~~
* ~~Add cron schedules for `src.main` (15:35 IST) and `src.suggest` (08:00 AM IST).~~
* ~~Add a fast background asyncio loop to `src/api.py` to poll prices via DhanHQ and trigger workflows only on watchlist breaches.~~

### Phase 5: Options Trading & Frictions — ✅ DONE (2026-07-06)
* **Part A (Frictions) — ✅ DONE:** ~~Update `src/portfolio.py` with 2026 STT (0.15%), SPAN margin simulation, and bid-ask slippage.~~ Full 2026 stack (STT sell-only, Stamp Duty buy-only, brokerage, NSE exchange charges, SEBI fees, GST on service charges) + `calculate_span_margin()` hedge-offset simulation in `src/portfolio.py`; dynamic bid-ask slippage in `src/plan_tracker.py`.
* **Part B (Strategy) — ✅ DONE:** ~~Update `src/strategy.py` to propose defined-risk spreads ONLY (Bull Call/Bear Put/Iron Condors). Integrate India VIX filtering (Block Iron Condors if VIX > 16). Update tracker for early exits at 60-70% max profit to kill Gamma risk.~~ `StrategyConstructor` + VIX gate (via `dhan_client.get_india_vix()`) + max-loss sizing; tracker resolves spreads as atomic baskets with 65%-of-max-profit / 2-days-before-expiry auto-exits. Proposal wiring also DONE: `src/options_proposer.py` (`python3 -m src.options_proposer`) fetches the real chain + VIX, builds the regime-matched spread, sizes it via `options_risk_per_trade_pct` (decision #28), and journals your approve/reject. Dashboard/Discord surfacing still open.

### Phase 6 (Advanced): Memory Consolidation & Evolution
* ~~Update Brain Map schema for `confidence_score` and temporal decay.~~ ✅ DONE 2026-07-06 — landed as the `semantic_nodes` table (confidence_score, last_reinforced/last_decayed, active flag) owned by `src/sleep_phase.py`, additive to brain_map's core schema.
* ~~Create a "Sleep Phase" background task to process memory off-market hours.~~ ✅ DONE 2026-07-06 — built as a standalone cron job (`src/sleep_phase.py`, 20:00 IST via `scripts/setup_cron.sh`) rather than inside `src/api.py`, so local LLM inference never shares a process with the live server.
* Add procedural evolution to `src/analyst.py` (proposing new trading rules to a `/candidates` folder based on loss clusters). — NOT STARTED.

### Phase 6C: Knowledge Graph Reasoning Layer — 🟡 READER DONE (2026-07-07)
* ~~Build `src/graph_engine.py`: a read-only `GraphEngine` loading the additive `graph_edges` table from `data/brain_map.db` into a `networkx.DiGraph`, with `get_relevant_context(node, max_hops=2)` (2-hop BFS, confidence-sorted).~~ ✅ DONE — memory-resident, never writes during inference (decision #33); `tests/test_graph_engine.py`.
* ~~Wire the Memory Query into the proposal path so linked historical patterns ride along in the Discord PROPOSAL ALERT rationale.~~ ✅ DONE in `src/options_proposer.py` (fail-safe 🧠 Memory block; advisory only, decision #26 philosophy). Query now seeds on ticker + view + strategy so concept-keyed causal edges surface.
* ~~Teach `src/sleep_phase.py` to WRITE causal edges into `graph_edges`.~~ ✅ **Phase 6D DONE 2026-07-07** — Task D `write_causal_links` mines `(subject)-[predicate]->(object)` triples from reviewed outcomes + post-mortems only (decision #34), confidence 1.0, idempotent; `local_parser.extract_causal_triples()` + `tests/test_causal_writer.py`. `networkx` added to `requirements.txt`. Populates once trades resolve and a Sleep Phase runs with Ollama up.

### Phase 7: The Time-Travel Simulator — ✅ DONE AND VALIDATED ON REAL DATA (2026-07-07)
* ~~Build `src/simulator.py` to override `datetime.now()` and loop over historical DhanHQ data.~~ ✅ Built with **as-of-date injection instead of `datetime.now()` monkeypatching** (the safer path recorded as a caveat when this phase was planned — decision #36): per historical day it computes the same SMA/RSI analysis over only the closes known then, and drives the REAL `options_proposer.build_proposal()` (regime map, VIX gate, max-loss sizing) with historical VIX + a synthetic option chain (premiums modeled — historical chains aren't retrievable). Run: `python3 -m src.simulator --start YYYY-MM-DD --end YYYY-MM-DD [--underlying "NIFTY 50"] [--skip-causal]`.
* ~~Instantly fast-forward plans to resolution to populate the Brain Map without waiting months in real-time. Use a simulated portfolio to protect the live paper state.~~ ✅ Resolution reuses `plan_tracker`'s pure helpers, so exits + the FULL 2026 friction stack are byte-identical to live. Results land idempotently (deterministic `sim:<hash>` journal_refs) in the new `simulated_trades` table + the standard `outcomes`/`events`/links — which the Sleep Phase's causal writer (decision #34) then turns into `graph_edges`. The real journal/portfolio are never touched (runtime-spied in `tests/test_simulator.py`); the simulated book is a plain dict.
* ✅ **Validated end-to-end on real DhanHQ history same day** (NIFTY 50, 2025-07-01 → 2026-06-30): 56 iron-condor proposals resolved (48 wins / 8 losses), `brain_map.db` populated from empty to 182 events / 56 outcomes / 168 links, and the causal writer minted the graph's first two real edges (`iron_condor RESULTS_IN win` / `RESULTS_IN loss`, confidence 1.0). See the production-state bullet above for full figures. **Not just built — proven working.**
* Still open (Phase 7b): a training script that fits the Phase 11 skeptic's Random Forest on `simulated_trades` rows and saves `data/skeptic_model.pkl` (the table already stores every `FEATURE_NAMES` input + the win/loss label). **Blocked on the DhanHQ auth debt below** — Phase 7b will want to simulate a much larger date range for a meaningful training set, and the current token/renewal setup can't sustain that unattended.

---
## 📋 Pending Phases
Estimated Sequencing: **Cross-Asset Integration (Asset Expansion) ➔ Dual-Horizon Sentiment (Dual Sentiments) ➔ ATR-Based Trailing Stoplosses (Trailing Stoploss)**

These upcoming features are officially added to the roadmap:

### 1. Cross-Asset Integration (Asset Expansion)
* **Objective:** Expand the data layer and ingestion pipeline to fully support MCX Commodities (Gold, Crude Oil) and Global Indices.
* **Details:** Leverages the DhanHQ API migration to fetch real-time and historical data for these instruments, enabling diversified multi-asset paper trading without additional third-party data feeds.

### 2. Dual-Horizon Sentiment (Dual Sentiments)
* **Objective:** Upgrade `news_processor.py` to support dual-horizon JSON outputs.
* **Details:** Separates news sentiment analysis into `short_term_catalyst_score` and `long_term_macro_score`, feeding distinct granular durations into the Brain Map.

### 3. ATR-Based Trailing Stoplosses (Trailing Stoploss)
* **Objective:** Upgrade the `plan_tracker` to implement dynamic, volatility-adjusted trailing stops.
* **Details:** Replaces rigid bracket orders with dynamic, ATR-buffered trailing stops to protect capital while letting profitable swing trends run.

### 4. Regime-Aware Memory
* **Objective:** Add regime tags to the Brain Map's event-outcome links.
* **Details:** Captures and links current market regimes (e.g., trend, volatility, regime type) to trades so the learning loop can query patterns specifically under matching market conditions.

### 5. Procedural Evolution
* **Objective:** Support human-in-the-loop candidate generation for rule changes.
* **Details:** Evaluates post-mortem clusters of losses in `src/analyst.py` and proposes rule adjustments to a `/candidates` folder for user review, driving iterative rule enhancement.

---
## 🔮 The Long-Term Vision (Phases 9 - 13)
(To be executed only after Phase 7 Simulator proves statistical Alpha)

### Phase 9: Secure Web Exposure & UI Deployment
* ~~Expose GCP VM API to the internet securely via Cloudflare Tunnel with API-key middleware to connect the React dashboard and Discord bot.~~ ✅ **DONE 2026-07-07, end to end**: `src/api_server.py` (strict fail-closed `x-api-key` gateway wrapping the full `src.api` app) + two-way Discord bridge `POST /api/discord/action` (approve/reject pending journal entries by `short_id`, `--review-pending` semantics). On the VM, `alpha-trading.service` runs the gateway on `127.0.0.1:8000` and a new `cloudflared-tunnel.service` forwards a public quick-tunnel URL to it (`Restart=always`, enabled on boot). Verified live from an outside network: health check + the Discord bridge both round-trip correctly. Still open: this is a quick tunnel, so the URL changes on restart — upgrading to a named tunnel (permanent URL) needs a Cloudflare-registered domain; and the React dashboard / Discord bot aren't yet pointed at the tunnel URL (the bot in particular has no button/command calling the bridge endpoint yet).

### Phase 10: Local LLM "Maker/Checker" (Hallucination Guardrails)
* Run a local open-source model (Llama 3 / Phi-3) on the local Mac as a strict auditor.
* Validate Gemini's cloud-generated plans against raw data to catch logical contradictions before Brain Map logging.

### Phase 10B: Local LLM Episodic Event Extractor (NOT the same as Phase 10 above — FULLY BUILT + CRON'D 2026-07-06)
A separate use of a local LLM from Phase 10's "maker/checker" auditor — this one is a text-to-structured-data parser feeding the Brain Map, not a plan validator. **All four steps below are built** (`src/local_parser.py`, `src/sleep_phase.py`, tests), Ollama + `llama3` are installed on the host, and the Sleep Phase is scheduled via `scripts/setup_cron.sh` (20:00 IST daily → `logs/sleep_phase.log`).

**Architectural rule this phase is built on:** an LLM (local or cloud) must NEVER be used for continuous 24/7 market monitoring — checking whether a price crossed a level or a moving average is pure math and belongs in `src/rules.py` / `src/dhan_client.py` on the VM, exactly as today. Using an LLM for constant price polling would be a massive, pointless compute cost. A local LLM's only job here is the "light work" of turning unstructured text (news, Discord chat, journal summaries) into structured JSON for the Brain Map — never live price decisions.

Planned build (when explicitly greenlit, one file at a time, offline-first, native `sqlite3` only — same discipline as every other phase):
1. **Ollama on the Mac** — install it as a free local model server (e.g. Llama 3 8B or Phi-3). Add `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`) to the env-loading logic (`src/config.py` or equivalent), OpenAI-compatible API.
2. **`src/local_parser.py`** — an "Episodic Event Frame (EEF) Extractor": one function that takes raw text (e.g. a news headline) and returns strict JSON `{"event_type": str, "tag": str, "sentiment": int, "entities": list}` — no conversational output, a narrow structured-extraction task only.
3. **Wire into `src/brain_map.py`** — feed that JSON into the `events` table via the existing `record_event()`/`_get_or_create_event()` helpers, additive only (decision #25's rule still applies — no execution or portfolio access).
4. **Async "Sleep Phase" loop** — runs off-market hours only, so local LLM inference never competes with the live trading loop; distills the day's raw text into Brain Map events in the background.

### Phase 11: The "Skeptic Agent" (Multi-Agent Debate) — 🟡 SCAFFOLDING BUILT (2026-07-07)
* ~~Introduce a dedicated Skeptic Agent to counter the primary Analyst's long-directional bias.~~ **Quantitative half scaffolded**: `src/skeptic_agent.py` (`RandomForestAuditor`) — frozen 10-feature vector merging knowledge-graph evidence + the proposal's market numbers, wired into the proposer so a low modeled P(win) appends a "⚠️ Skeptic Agent Warning" to the Discord alert. **ABSTAINS until the Phase 7 simulator trains `data/skeptic_model.pkl`** (decision #35 — no fake warnings from an untrained forest); advisory only, never gates.
* Still open: training the model (blocked on Phase 7), and the original multi-agent structural-debate idea (an LLM skeptic arguing the counter-case) if still wanted once the numerical auditor is live.

### Phase 12: The Intraday Trading Loop
* Transition from hourly/daily OHLC swing-trading to a real-time streaming websocket architecture for rapid same-day fetch-decide-execute loops.

### Phase 13: Live Broker Execution
* Remove the strict "Paper-Trading Only" guardrail.
* Connect DhanHQ /v2/orders execution endpoints to route real capital to the NSE.

---
## 🌐 Future Frontiers
(Architecture documented ahead of the build — not started, not scheduled)

* Phase 8: Semantic News Ingestion (Spec fully defined in docs/PHASE_8_NEWS_INGESTION_SPEC.md).
---

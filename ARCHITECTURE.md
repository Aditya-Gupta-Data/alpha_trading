# ARCHITECTURE.md — The Department Map

**Read this first, before any file.** The system is organized as **8
departments**. Each department has ONE **Manager** — the single file/seam you
approach to change how that department behaves. You should never have to dig
through 50 files: find the department, go to its manager.

- **Why** behind each choice → `DECISIONS.md` (numbered).
- **Per-file** one-liners and module *specifics* (config keys, thresholds,
  verification state) → `MODULES.md` (grouped by these same departments). This
  file states each department's design role and its one seam; it does NOT
  restate those specifics — they have a single home in `MODULES.md` so they
  can't drift out of sync.
- **The rules** the code may never break → `OVERVIEW.md`.

Written for the strategic brain, not the compiler: every department below says,
in plain English, what it does, what goes in, what comes out, and the ONE place
to change it. Current as of `52003d9` (2026-07-18, review #2), suite **1174
collected**.

> **Numbering note (review #2):** Department 8 — ANALYSIS was added in this
> revision. In the *data flow* it sits between DATA (1) and DECISION (2), but
> it is numbered 8 so that every existing reference to Departments 1–7 (in the
> ledger, DECISIONS.md, and prior review notes) stays correct forever. Numbers
> are names here, not an ordering.

---

## The whole system in one breath

```
   ┌── 1. DATA ─────────┐   market quotes, chains, news, deals, flows
   │  come IN here      │   → cleaned, archived
   └─────────┬──────────┘
             ▼
   ┌── 8. ANALYSIS ─────┐   "what regime are we in? what does smart money say?"
   │  the research desk │   → one advisory VERDICT (veto/allow), fail-open
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
*earned* through Validation, never hand-wired — with exactly ONE sanctioned
carve-out, written down in Department 8: an analysis verdict may be hand-wired
*only* when honoring it can never add risk (a veto that blocks a structure, or
disables short premium). Anything that would CREATE or SIZE a trade earns its
authority in Department 5 first, no exceptions.

---

## Department 1 — DATA (market data in)

**Manager:** `src/dhan_guard.py` (`SafeDhanClient`) — the one hardened door to
all market data. Token lives behind `src/token_provider.py` (the single token
seam). Nothing else constructs a Dhan client.

**What it does (plain English):** brings the outside market into the system —
live prices, daily history, and option chains (with per-strike Greeks) — and
does it *safely*: it classifies failures (auth vs data outage), retries once on
a rate-limit, and voids stale quotes. Separately, the `ingestion/` clerks
capture end-of-day data that can never be re-bought later into a `lake/`
archive: option chains, bulk deals, FII/DII flows (`flows_tracker` forward,
`flows_backfill` for owner-supplied history), earnings dates, macro, news
(`rss_ingester`, decision #75 — publishers' OWN feeds, never scrapes),
corporate announcements (`corporate_events` — catalyst/expansion/risk
classified), full fundamental statements (`fundamental_parser`, yfinance), 15-minute
intraday snapshots (`intraday_tracker`), annual-report PDFs
(`report_downloader` — NSE's filings archive, Mac-only, throttled,
honest outage codes; feeds Department 8's forensic reader), and
exchange-filed quarterly results (`nse_results` — the Darling
Pipeline's quant feed, same safe-crawl doctrine). `config/sector_universe.json`
(7 NSE sectors → parent index + ~70 constituents) is this department's map of
the market's structure.

**The two layers of the door — a deliberate split:**
- `dhan_client` is the **wire**: mechanics only. The proactive `_throttle()`
  (1.1s minimum gap, process-wide, the DH-905 fix) lives HERE, below the guard,
  on purpose — pacing must cover *every* caller, including ingestion clerks
  that talk to the wire directly, so it sits at the lowest layer where nothing
  can route around it. Known gap: the throttle sleeps silently — throttle
  events are unobservable until it logs a countable line (Dept 1 follow-up,
  flagged on the CEO Brief).
- `dhan_guard` is the **judgment**: failure classification, retry-once,
  stale-quote voiding. Anything that *interprets* an answer changes here.

**One door, possibly two transports (review #2 ruling):** if the staged
WebSocket feed (`next_gen_engine/dhan_websocket.py`) is ever adopted, it lands
BEHIND `dhan_guard` as a second *transport* (push, beside REST pull) — decoded
ticks pass the same staleness/failure judgment and surface through
`market_snapshot`, so consumers never know a socket exists. It must NOT land as
a peer module with its own path to consumers: the non-negotiable stays "one
market-data door," counted in doors, not sockets. It already reuses
`token_provider` (verified) — never a second credential source.

**Owns (review #2's standing gap — CLOSED 2026-07-20):** scrip-master
reconciliation. `SECURITY_ID_MAP` encodes broker security IDs that silently rot
on delisting/demerger (ledger Issues 14/15: LTIM delisted, TATAMOTORS demerged —
both found by a human at deploy time). `ingestion/scrip_master.py` now diffs
every mapped id — and the `config/scrip_wanted.json` list, GOLDBEES included —
against Dhan's public scrip master weekly, firing a de-duped review card on any
mismatch. Nothing auto-corrects: a guessed replacement id is the same bug again,
so the clerk reports and a human edits the map. A fetch failure reports
`unavailable`, never a pass — an unread master must not look like a clean run.
First live run: 218,714 master rows, **88/88 ids verified**, zero drift.

**Inputs:** DhanHQ Data API (read-only), NSE end-of-day reports, Google-News
RSS, yfinance fundamentals/index bars, owner-supplied flow exports.
**Outputs:** clean quotes/chains on demand; `data/market_snapshot.json` (the
engine's published marks — everyone else READS this so the live loop stays the
*single* Dhan consumer); dated archives under `data/lake/`.

**To change data handling, go to:** `dhan_guard` for live-fetch judgment;
`dhan_client` for wire mechanics (pacing, endpoints); `market_snapshot` for the
shared marks; the specific `ingestion/<x>.py` clerk for an archive feed;
**`text_intelligence` (decision #74) to change how raw text becomes a JSON
signal** — one manager seam where the LLM backend (`ollama` local / `claude`
cloud) is a config choice, with a daily budget cap and incremental de-dup.
(News sentiment itself is `news_processor` → Gemini → `data/news_sentiment.json`,
scheduled daily; it is NOT a DB table.) The staged `wisdom_extractor` (memo →
backtestable JSON frame) deploys into this department as
`src/ingestion/wisdom_extractor.py`, a permanent `text_intelligence` *client*
— it must never grow its own model call (enforce with an import-ban test, the
`equity_shadow_proposer` precedent).

---

## Department 8 — ANALYSIS (the research desk)

**Manager:** `src/analysis/regime_filters.py` (`advise()`) — the ONE seam
through which anything this department knows reaches a live decision. The
verdict is composed once per cycle in `market_loop.fetch_market_state` and
honored by `options_proposer.build_proposal` via the additive `advisory=`
kwarg (the vol_bridge pattern). Fail-open throughout: absent data or any
exception leaves the proposer byte-for-byte unchanged.

**What it does (plain English):** turns raw archives into *market judgment* —
without ever touching a trade. Six modules: `smart_money_trend` (net
institutional accumulation/distribution + block-deal VWAP from the 12.5-yr
deals ledger), `sector_trend` (is the parent sector index above its 50/200
SMAs; stock vs sector relative strength), `macro_shocks` (the War Playbook —
known crisis windows and which sectors historically survive them),
`regime_filters` (the manager — composes those into the two live radars: the
smart-money/sector VETO on bullish index spreads, and the CRISIS regime that
disables short-premium structures), `annual_report_analyzer` (the forensic
annual-report reader: section-aware condensation → LLM extraction through
`text_intelligence` → a verbatim-quote validator that DROPS any finding whose
quote isn't on the cited page — so a weak local model yields fewer findings,
never fabricated evidence; conviction JSONs land in the lake, advisory-only),
`cohort_comparator` (the side-by-side matrix of human deep-read scores and
machine scores — two instruments, never rescaled into one number),
`fundamental_screener` (the Darling screen: mechanical pass rule over
exchange-filed numbers + the forensic trust gate; writes every passer to
`darlings_queue.json`, which the report_downloader consumes — quant finds
the darlings, the deep-read tells us if they're genuine),
and **the Darling lifecycle** — the two-clock system (decision #77) that
keeps that list from going stale: `dynamic_pricer` (per-name buy zone,
ATR stop, trim pivots, overextension state) + `valuation_scorer` (the
1–100 cheap-to-rich score vs winsorized sector-or-market μ/σ) feed
`darling_tiers`, which every EOD grades the WHOLE cohort into seven tiers
(strong/weak buy–hold–sell + watch) plus an honest `ungraded` Tier 0 —
so a name is never "done" after entry and the same table that says BUY
also says SELL for what we hold. Two clocks, deliberately different:
`patience_basket --eod` (daily 19:15) re-grades on PRICE, because prices
move daily; `weekly_recalibration` (Saturday 10:00) re-judges
FUNDAMENTALS, because those only change when filings arrive — and it
OVERRIDES the daily grade by PINNING a held name that fails its screen
(the No-Orphan rule) until its paper position closes,
plus two **research-stage orphans**:
`institutional_alpha` (VWAP-pullback entry primitives — signal source for the
Shadow Equity Engine's thesis) and `conviction` (a 0–1 multi-factor score that
*aspires* to drive position sizing).

**The department's iron rules:**
- **Read-only and point-in-time.** No module here writes trade state, and every
  signal uses only data dated strictly before the decision day. NULL-honest: a
  missing factor abstains (or scores neutral) — never a fabricated edge.
- **Risk-reducing authority only, hand-wired.** The live radars may VETO and
  DISABLE — never create, size, or approve. This is the composition law's one
  carve-out, and it is capped there: `conviction`'s sizing ambition reaches the
  book ONLY by earning authority in Department 5 and then being wired through
  Department 3's manager (`portfolio_manager`). Not from here, not directly.
- **One seam out.** New radars compose inside `advise()`; the proposer never
  imports analysis modules directly, and the deals ledger is loaded once per
  cycle in `market_loop` — never inside `build_proposal`.

**Known debts (review #2):** the package shipped with ZERO tests while its
veto logic was LIVE on the VM — closed 2026-07-19 by
`tests/test_regime_filters.py` (the manager seam + the proposer contract) and
`tests/test_analysis_signals.py` (all five signal/research modules, plus the
IST decision-day fix: the default `as_of` now comes from the shared IST clock,
never the host timezone). The veto's supporting evidence is
the simulator, whose P&L is known-inflated — acceptable for a
conservative veto, not transferable to anything that adds risk.

**Inputs:** the deals ledger, sector universe + index bars, VIX, shock windows.
**Outputs:** one advisory verdict dict per cycle; signal primitives consumed by
the Shadow Equity Engine (Dept 4's telemetry arm).

**To change analysis, go to:** `regime_filters` (the verdict + thresholds),
the specific signal module for its math. To give any of it MORE authority than
a veto, go to Department 5 first.

---

## Department 2 — DECISION (the live trading engine)

**Manager:** `src/options_proposer.py` (`run_headless`) — the one place a spread
proposal is born, enriched, and journaled. Composed each day by
`src/master_scheduler.py`; driven cycle-to-cycle by `src/market_loop.py`.

**What it does (plain English):** during market hours it reads the trend (SMA
cross + RSI) and India VIX, decides a bullish / bearish / neutral view, and
builds the matching defined-risk spread (bull-call / bear-put / iron-condor) via
`src/strategy.py` — **after honoring Department 8's advisory verdict**: a
bullish view under a smart-money/sector veto builds nothing, and a neutral view
in a crisis regime skips the short-premium condor. It then attaches *context
annotations* to the proposal — Book Context (#73), Memory, Skeptic, Alignment —
and writes it to the journal as PENDING. **The one decision seam is
`decide_pending()`** — terminal, Discord button, and auto-approve all converge
there; nothing approves a trade any other way.

**Inputs:** trend/VIX read + advisory verdict, an option chain, the current book.
**Outputs:** a PENDING_APPROVAL journal entry + a rich Discord proposal card.

**To change what/how we propose, go to:** `options_proposer` (pipeline +
`decide_pending`), `strategy` (the leg math), `trade_planner` (view→structure
routing). Annotations are their own modules but only *decorate* here.
**Staged merge target:** `execution_algo` (next_gen Phase 2) refines the
`_leg_fill` paper-fill layer (decision #70 — limit-chase, honest MISS,
protective-leg-first). It changes how a paper fill is *modeled*, nothing else;
"no broker/order-placement import in `src/`" survives the merge untouched.

---

## Department 3 — RISK & CAPITAL (the gatekeeper)

**Manager (entry side):** `src/portfolio_manager.py` — the capital pool, margin
locks, and the entry halts. **Manager (exit side):** `src/plan_tracker.py` —
THE one settlement path (no other code closes a trade).

**What it does (plain English):** stands between a proposal and the book. On the
way IN: `exposure_gate` (#68) blocks a duplicate (one spread per
underlying+direction), then the margin gate checks the capital pool can afford
it, under the entry halts (today: the 10% trailing-drawdown halt). While
positions are OPEN: `portfolio_greeks` (#71) watches the *whole book's* net
Vega/Delta against equity budgets; `live_bridge` marks open spreads against
live quotes in real time — READ-ONLY advisory exit alerts, with the ONE
sanctioned exception (#69): the config-gated intraday profit-take square-off,
which itself settles through `plan_tracker`'s path. On the way OUT:
`plan_tracker` resolves every trade against real prices — take profit at 65%,
never hold into the last 2 days to expiry, close as one atomic basket, settle
the cash once. On profitable settlement, `wealth_lock` records the advisory 50%
GOLDBEES sweep (paper ledger + card, never a cash movement), hooked from
`portfolio_manager.release_entry`.

**The halt-stack rule (review #2 ruling — REALIZED 2026-07-19):** entry halts
COMPOSE at one point. `request_entry` evaluates the single ordered
`ENTRY_HALT_CHECKS` list, each check answering (halted?, reason): (1) the 10%
trailing-drawdown risk-of-ruin halt, (2) the daily 3% realized-loss circuit
breaker (merged from staging — entries only, resets at the IST day boundary
by construction, fires one de-duped Discord review card per day when it
rejects). No halt is ever called from anywhere else; a new halt is a new
entry in the list, not a new call site.

**Entry-time VIX stress (owner decision 2026-07-19):** the margin a proposal
reserves is the SPAN total times `portfolio.span_stress_factor` at the
proposal's own VIX (1.0 calm / 1.15 at VIX≥16 / 1.30 at VIX≥25 — a simple
paper model of exchange stress hikes, to be recalibrated against NSE
circulars). In a panic the reservation grows upfront, so the existing
margin-exhaustion check naturally chokes how many trades fit — no forced
closes, no second settlement path. `margin_audit` (report-only CLI) replays
the journal under these factors and cross-checks recorded margins for drift.

**The equity desk (decision #79, owner ruling 2026-07-20):** the firm's 10L now
funds TWO desks. `src/equity_desk.py` runs the darling shadow book's capital on
the Mac-side desk ledger (`data/equity_desk.db`) with the SAME
portfolio_manager machinery — same halt list, same drawdown math, same
exhaustion doctrine. The desk funds darling entries (1% risk / 15% notional
cap, whole shares, delivery frictions on settlement) through injected seams at
the patience_basket composition root; funding fails CLOSED while the telemetry
ledger keeps every row. The Dept-5-first rule was explicitly waived by the
owner for this wiring; the desk's own equity curve is the evidence a later
Dept-5 review judges.

**The firm treasury (decision #80, owner Directive 1):** the split between the
desks is DYNAMIC. `src/firm_treasury.py` re-routes capital nightly inside the
Mac EOD chain — after tier grading (freshest demand read), before the shadow
leg spends — using a mechanical regime router (NIFTY trend, Buy-tier depth,
valuations, VIX, options-desk margin demand; bounds 15–60% equity share,
₹50k deadband, ₹1L max step). The desk's account base IS its allocation
(subscribe/redeem shift `starting_capital` + peak together, so the ruin halt
keeps measuring real rupees against current capital); the VM options account
mirrors it as the `equity_desk_allocation` reservation lock. The two-phase
RAISE-FIRST rule keeps the invariant E_vm ≥ E_mac through any mid-move crash:
a partial failure can only idle capital for a night, never double-spend it,
and the next run's reconcile (E_vm := E_mac) converges every failure mode.
P&L stays with the earning desk — winners compound their own buying power.

**Adaptive sizing (decision #81, owner Directive 2):** both desks consult
`src/adaptive_sizing.py` before sizing — the first consumer of the
four-question autopsy frame with sizing authority. A break-even-centered
Bayesian read of each setup's OWN resolved record: penalties are fast
(≥4 weighted resolutions, floor 0.25x), vetoes are earned (≥8, and only when
even the Wilson upper bound sits under break-even — the telemetry row still
logs), boosts are slow (≥10, lower bound over break-even, cap 1.5x inside
the existing caps). Gap-shock losses count half; a repeatedly-burning ticker
can be vetoed on its own. Day one it says 1.0x for everything — authority
accrues per-key as the record earns it, never before.

**Staged merge targets:** ~~`wealth_flywheel`~~ **MERGED 2026-07-20** — the
scrip clerk verified GOLDBEES as NSE_EQ id **14428** (NIP IND ETF GOLD BEES,
series EQ), the Issue-15 blocker was lifted, the id entered `SECURITY_ID_MAP`,
and `build_sweep_order` graduated into `wealth_lock.size_sweep_order` (staging
file deleted — the anti-orphan rule, second graduation after
`portfolio_risk_manager`). The sweep now earmarks 50% of a winning settlement
AND sizes whole GOLDBEES units with an honest cash residual — but only while
`wealth_lock.goldbees_verified()` confirms the id against the clerk's latest
report, re-read at call time. Mismatch, stale (>14d), `unavailable`, or missing
report ⇒ sizing BLOCKED and the sweep degrades to the pre-merge earmark-only
behavior, with the reason stored on the row and stated on the card;
`trailing_stops` → its `atr()` math goes to `indicators.py` (the shared,
department-neutral math library: SMA/RSI today), its advisory ratchet loop
goes to `live_bridge` beside the existing exit alerts. That split is clean
because it separates a *formula* from an *advisory process* — the stop is one
more advisory alert, and the actual close still lands through `plan_tracker`
like every other exit. (`portfolio_risk_manager` completed this path
2026-07-19 — merged into the halt list, staging file deleted.)

**Inputs:** a proposal (entry); open positions + live/EOD prices (exit).
**Outputs:** allowed/blocked verdict + margin lock; resolved outcomes with P&L;
book-level Greek advisories; advisory exit/stop alerts.

**To change risk behavior, go to:** `exposure_gate` (duplicates),
`portfolio_manager` (margin + the halt list), `portfolio_greeks` (book
budgets), `plan_tracker` (exits/settlement), `live_bridge` (live advisory
exits). Exits are advisory-to-the-human by rule (#41/#11) except the one
sanctioned intraday square-off (#69).

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

**The telemetry arm — a deliberately SEPARATE second store (review #2 ruling):**
the Shadow Equity Engine (`equity_shadow_proposer`) paper-tracks zero-capital
equity theses (block-VWAP pullbacks on deal-covered names, built on Dept 8's
signal primitives) purely to log the four-question learning frame — WHY
entered, HOW (context), WHAT (action), what was LEARNED (autopsy). Its events
go through `knowledge_graph_logger` to `logs/equity_shadow_journal.jsonl` —
**deliberately OUTSIDE `brain_map`**, because zero-capital telemetry mixed into
the options engine's `query_similar_events` would skew live decisions, and a
"remember the mode filter in every query" convention would fail exactly once.
The parallel store remains the right shape in the capital era (decision #79):
money math lives in the equity desk's OWN ledger (`data/equity_desk.db`), never
here — the ONLY path into `brain_map` is a future explicit, mode-tagged ingest,
never an implicit merge. The shadow engine is import-banned (test-enforced)
from journal / portfolio_manager / equity_desk / options_proposer / notifier /
brain_map. Block-leg events carry `mode="PAPER_TELEMETRY"` +
`capital_allocated=0`; a darling entry the desk funds is stamped
`mode="PAPER_CAPITAL"` with its locked notional (its exit rides the same
stamp), and a funding rejection keeps the telemetry row with the reason — the
learning ledger never loses a line to the capital layer.

**Inputs:** resolved outcomes, daily market context, deal/flow history.
**Outputs:** the trade ledger; queryable pattern memory; tuned weights; the
quarantined shadow-telemetry ledger.

**To change memory/learning, go to:** `journal` (the record), `brain_map`
(the store + queries), `sleep_phase` (nightly tasks), `tuner` (weight
learning), `knowledge_graph_logger`/`equity_shadow_proposer` (the telemetry arm).

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
trades without touching real money; `digest` reports the weekly state.

**Jurisdiction over Department 8 (review #2):** an analysis signal that wants
more than veto power — sizing, entries, anything that ADDS risk — comes through
this court like any mined pattern: frozen definition, out-of-sample trial,
placebo control. `conviction` and `institutional_alpha` are the first two in
that queue. Simulator results alone (known-inflated) are never sufficient
evidence.

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
market hours), `eod_summary` (Mon–Fri 15:45 IST, cron #20 — scheduled
2026-07-18 after its docstring claimed a slot it never had; 15:45 not 15:30
because the scheduler self-terminates AT 15:30), `ceo_brief` (Mon–Fri 16:30
IST, cron #19 — ONE cross-department card: operations heartbeats, bucketed
issues, deployed SHAs per service, risk & capital), `performance` (#72, weekly
Sharpe/Sortino/drawdown), `ops_monitor` (nightly health + job heartbeats),
`validation/digest` (weekly harness state). On-demand CLIs: `explain <id>`,
`book_context`, `view_positions`, `graph_viz`.

**Two numbers, one owner:** any figure two cards both show is COMPUTED ONCE.
`ceo_brief` reuses `eod_summary`'s journal readers rather than recomputing
today's P&L — that's the rule, not an optimization (two independent
"today's P&L" computations is how two cards start disagreeing). Pending
follow-ups from review #2: promote `eod_summary`'s underscore-private readers
to a small public read API (the reuse is currently a reach-in), and move
`ceo_brief`'s private due-hour map into `ops_monitor.EXPECTED_JOBS` — **the
schedule's one source of truth is `scripts/setup_cron.sh`**, everything else
carries a drift-guard test against it.

**Inputs:** the journal, brain map, and live marks (read-only).
**Outputs:** Discord cards + terminal reports. Never places or changes a trade.

**To change reporting, go to:** `notifier` (the delivery mechanism), or the
specific report module for its content.

---

## Department 7 — INTERFACES (the front doors)

**Manager:** `src/api_server.py` — the strict fail-closed gateway (every request
needs the API key). It mounts the unified `src/api.py` app.

**What it does (plain English):** how a human sees the system and taps a
decision. The dashboard (served through a tunnel) and the Discord bot
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

## The staging ground — `next_gen_engine/` (NOT a department)

A local build area for institutional-maturity modules, **imported by nothing in
`src/`, unscheduled, undeployed**. Its 29 hermetic tests are collected by the
main suite so it can't silently rot. The rules for LEAVING it:

1. **Every module names its canonical `src/` target** in its header and the
   folder README (the anti-orphan rule, learned from the discarded
   `pattern_registry`/`trial_runner` duplicates). At deploy the logic moves
   INTO that target; the folder must never become a second implementation.
2. **Optional deps (`redis`, `websockets`) enter `requirements.txt` in the
   adoption commit, not before.** Lazy imports keep the suite green without
   them.
3. **Phase 4 is deferred by review #2 ruling** (see non-negotiables): the
   event bus does not merge until it has its own manager and written
   subscriber rules; the WebSocket feed merges only BEHIND `dhan_guard`.

Current residents and their targets: `wealth_flywheel` → Dept 3 `wealth_lock`
(GOLDBEES id verification blocker); `trailing_stops` → `indicators` +
`live_bridge`; `execution_algo` → Dept 2 `_leg_fill`; `wisdom_extractor` →
Dept 1 ingestion (text_intelligence client); `redis_pubsub` +
`dhan_websocket` → deferred drafts. First graduation: `portfolio_risk_manager`
merged into Dept 3's halt list 2026-07-19 (file deleted, tests migrated) —
the anti-orphan rule working as written.

---

## How a single trade flows through the departments (the canonical path)

1. **DATA** serves a fresh option chain + trend read.
2. **ANALYSIS** hands the loop one advisory verdict: no smart-money veto
   today, no crisis regime.
3. **DECISION** builds a bear-put spread, annotates it with book context,
   writes it PENDING, fires the proposal card.
4. **RISK** checks the exposure gate + margin under the halt list, approves it
   (auto or human tap), locks margin; later `plan_tracker` takes profit at 65%
   and settles the cash; `wealth_lock` records the sweep.
5. **MEMORY** records the outcome + a post-mortem; the tuner learns from it.
6. **VALIDATION** lets any registered pattern that fired shadow-score this
   outcome, out-of-sample.
7. **REPORTING** shows it on the 2h card, the 15:45 EOD card, and the 16:30
   CEO Brief.
8. **INTERFACES** is where you saw the proposal and tapped approve.

---

## Infrastructure (where the departments physically run)

- **The VM (`alpha-trading-vm`, GCP) is the sole live engine.** It runs the
  decision loop, risk gates, reporting cards, and the gateway via `systemd`
  services + a cron block (`scripts/setup_cron.sh` — 20 numbered jobs; the
  token renews once at 07:00 IST). It mints its Dhan token from GCP Secret
  Manager. See `HANDOVER.md` for the deploy checklist.
- **The Mac** runs only what needs a local Ollama or interactive state: the
  evolution agent and edge miner. It is NOT the engine; closing it doesn't stop
  trading.
- **State** is file-based under `data/` (git-ignored): `journal.jsonl`,
  `brain_map.db`, `market_snapshot.json`, `brain_weights.json`, plus the
  `lake/`. The shadow-telemetry ledger lives under `logs/`
  (`equity_shadow_journal.jsonl`, git-ignored). Config (versioned, non-secret)
  in `config.json` + `config/` (including `sector_universe.json` and the
  scrip-master-sensitive `watchlist.yaml`). Secrets in `.env` (git-ignored).
  The `lovable-frontend/` UI lives only on the `lovable-ui` branch, never on
  `main`.

## Structural non-negotiables (enforced by this architecture)

- No broker/order-placement import exists anywhere in `src/` — paper only.
- `dhan_client` calls Dhan's *data* endpoints only, never order/fund endpoints.
- One decision seam (`decide_pending`), one settlement path (`plan_tracker`),
  one Discord door (`fire_broadcast`), one market-data door (`dhan_guard`) —
  **doors are counted in doors, not transports**: a push feed, if adopted,
  is a second transport behind the SAME door with the same guarantees.
- Losses are append-only in the ledger; only Validation grants authority;
  every non-Decision layer is annotate-only (#63). The one carve-out:
  Department 8 verdicts may be hand-wired ONLY while they are strictly
  risk-reducing (veto/disable). Anything that creates or sizes goes through
  Department 5.
- `PAPER_TELEMETRY` events never enter `brain_map` except through an explicit,
  mode-tagged ingest. The options engine's memory queries must never see
  zero-capital telemetry by accident.
- **The event bus is DEFERRED (review #2 ruling).** If it is ever adopted:
  it becomes its own department with its own manager — the ONLY file that
  imports `redis_pubsub`; publishers call that one seam; subscribers are
  annotate-only by written rule and may NEVER settle a trade (that is
  `plan_tracker`'s exclusivity) or send a card (that is `fire_broadcast`'s).
  Until a consumer exists that the single-box scheduler demonstrably cannot
  serve, the bus stays a draft.
- Staging code leaves `next_gen_engine/` only INTO its named canonical target.
- `MODULES.md` and this file update in the SAME commit as any module change —
  an undocumented module is a review bug (Issue: `institutional_alpha` and
  `conviction` shipped unindexed in `6d89eb4`; fixed in review #2).

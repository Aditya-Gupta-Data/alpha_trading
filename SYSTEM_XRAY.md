# SYSTEM_XRAY.md — "Where are we actually?"

**Audit date: 2026-08-05.** Every number below was READ, not remembered — from
the live VM (`alpha-trading-vm`, `project-37632031-10d0-47dd-b6f`), its
`data/brain_map.db`, its `logs/`, its `data/lake/`, and the Mac working tree.
Where a document in this repo disagrees with what I read, I say so and the
live box wins.

> **Reading note.** This is an X-ray, not a report card. It is deliberately
> unkind about things that are half-wired, write-only, or empty. Nothing here
> says the system is bad — it says exactly what is load-bearing and what is
> decoration, so you stop paying attention tax on the decoration.

---

## 0. The one-paragraph verdict

The **ingestion layer is genuinely strong** — 28 VM cron jobs, ~15 outside-world
feeds, honest failure codes, a real 38-year macro lake. The **decision layer is
narrow** — 19 resolved real trades ever, and **every single options trade in the
book's history is the same structure in the same direction** (`bear_put_spread`).
The **learning layer is empty** — `candidate_patterns` = 0 rows,
`pattern_audit` = 0 rows; Department 5, the proving court, has never tried a
single pattern, and the nightly miner has skipped **17 consecutive nights**
because it needs 60 daily-context frames and only 25 exist. So: hum data बहुत
अच्छा इकट्ठा कर रहे हैं, decisions बहुत कम ले रहे हैं, aur unse सीख अभी
zero रहे हैं. Yeh koi crisis nahi hai — yeh phase hai. Par isko naam dena zaroori
tha.

---

## 1. INPUTS — every piece of outside-world data we ingest

Fifteen distinct external feeds. Grouped by the door they come through.

| # | Input | Door (module) | Provider |
|---|---|---|---|
| 1 | Live quotes / LTP (indices + equities) | `src/dhan_guard.SafeDhanClient` → `src/dhan_client` | DhanHQ Data API |
| 2 | Daily OHLC bars | same | DhanHQ |
| 3 | Option chains + per-strike Greeks + spot | same (`get_option_chain`) | DhanHQ |
| 4 | India VIX | same (index quote) | DhanHQ |
| 5 | EOD option-chain archive (4 nearest NIFTY/BANKNIFTY expiries) | `src/ingestion/chain_archiver.py` | DhanHQ |
| 6 | 15-min intraday price snapshots (84 watchlist + open desk book) | `src/ingestion/intraday_tracker.py` | DhanHQ |
| 7 | Full equity bhavcopy incl. **delivery %** | `src/ingestion/bhavcopy_clerk.py` | NSE static archive |
| 8 | F&O bhavcopy + MWPL ban list + volatility file | `src/ingestion/fo_bhavcopy.py` | NSE static archive |
| 9 | Bulk & block deals | `src/ingestion/deals_tracker.py` | NSE (cookie handshake) |
| 10 | FII/DII daily cash provisional | `src/ingestion/flows_tracker.py` | NSE |
| 11 | Earnings / results calendar | `src/ingestion/earnings_calendar.py` | NSE |
| 12 | Quarterly filed financials (XBRL, SEBI integrated-filing) | `src/ingestion/integrated_results.py` (+ legacy `nse_results.py`) | NSE / nsearchives |
| 13 | Annual-report PDFs | `src/ingestion/report_downloader.py` | NSE corporate filings |
| 14 | Corporate announcements (order wins, M&A, SEBI actions) | `src/ingestion/corporate_events.py` | NSE |
| 15 | NSE daily all-indices close (VIX + NIFTY + 13 sector indices) | `src/ingestion/indices_lake.py` | NSE |
| 16 | Cross-asset macro (BRENT, DXY, USDINR, US10Y) | `src/ingestion/macro_lake.py` | FRED REST API |
| 17 | Pre-2019 index history | `src/ingestion/index_history.py` | **owner's manual CSV download** (NSE bot-blocks every scripted path) |
| 18 | News headlines → sentiment | `src/news_processor.py` | Google News RSS → **Gemini** |
| 19 | Publisher RSS feeds | `src/ingestion/rss_ingester.py` | Moneycontrol / ET / BS own RSS |
| 20 | Commodity/FX directional matrix | `src/ingestion/macro_tracker.py` | DhanHQ (verified ids only) |
| 21 | Dhan scrip master (~218k rows) | `src/ingestion/scrip_master.py` | Dhan public CDN CSV |
| 22 | Sector index bars (10y) | `data/sector_index_bars.json` | yfinance — **frozen since 2026-07-16, no refresher on any cron** |

**Not ingested at all** (worth knowing, because people assume we have it):
option-chain *history* older than the archiver's start, tick data, order-book
depth, corporate-action (split/bonus) adjustments, short-interest, mutual-fund
holdings, any paid/licensed dataset.

---

## 2. SOURCE × METHOD × WHY — and which inputs feed no decision

"Feeds a decision" = can change whether a trade opens, how big it is, or when
it closes. Everything else is capture or reporting.

| Input | Method | Cadence | WHY — which decision | Verdict |
|---|---|---|---|---|
| Live quotes | REST pull, host-wide 1.1s throttle (`_throttle`, `flock` on `data/.dhan_throttle`) | continuous 09:15–15:30 | entry price, exit trigger, MTM | **LOAD-BEARING** |
| Daily OHLC | REST | on demand | trend read (50/200 SMA), `plan_tracker` stop/target resolution against real high/low | **LOAD-BEARING** |
| Option chain | REST | per proposal | leg selection, real premiums, `portfolio_greeks` | **LOAD-BEARING** |
| India VIX | REST | per cycle | VIX gate (#27), `span_stress_factor` margin stress, regime band | **LOAD-BEARING** |
| Chain archive | REST, 15:40 | daily | *nothing today.* Bought for #36 ("historical chains are unbuyable") | **CAPTURE-ONLY, deliberate** |
| Intraday 15m | REST, every 15m | 25 taps/day | *nothing.* No reader exists | **ORPHAN — see §7** |
| Equity bhavcopy | NSE CSV | daily 19:15 (VM) + Mac inline | `dynamic_pricer` levels → darling tiers → equity desk entries | **LOAD-BEARING** |
| F&O bhavcopy | NSE CSV | daily | `equity_entry_checks.liquidity_filter` (tier-1 / exchange-ban veto) | **LOAD-BEARING (veto)** |
| Bulk/block deals | NSE + cookie | daily 19:30 | `regime_filters._distribution` → `block_bullish` veto; entity affinity; `daily_context` | **LOAD-BEARING (veto only, #60)** |
| FII/DII flows | NSE | daily 19:35 | `confluence/evidence` layer + `daily_context` frame | **EVIDENCE-ONLY** |
| Earnings calendar | NSE | daily 19:20 | `days_to_results` → evidence layer + morning brief | **EVIDENCE-ONLY** |
| Quarterly financials | XBRL parse | Saturday | `fundamental_screener` (darling pass/fail) + `valuation_scorer` (1–100) → tiers | **LOAD-BEARING (equity desk)** |
| Annual-report PDFs | NSE archive, 8–15s throttle | on demand | **nothing — the analyzer that read them is in `research_archive/`** | **ORPHAN — see §7** |
| Corporate announcements | NSE | manual/backfill | *nothing.* Written to `data/lake/events/`, no reader in `src/` | **ORPHAN — see §7** |
| NSE indices close | NSE CSV | daily | `macro_features` channels → regime declaration; sector playbooks | **ANALYSIS-ONLY (zero execution authority)** |
| FRED macro | REST | daily 19:50 | same as above | **ANALYSIS-ONLY** |
| Pre-2019 index history | manual CSV | one-shot | lit up the NIFTY benchmark channel back to 1995 for `auto_discovery` | **RESEARCH** |
| News sentiment | Gemini | daily 19:10 | `forecast.py` bias, `confluence/evidence` news layer, `daily_context.news_net` | **EVIDENCE-ONLY** |
| RSS feeds | stdlib urllib | daily 18:50 | *nothing.* And on the VM the classifier **silently no-ops** (backend `ollama`, VM runs no Ollama by design) — so we are storing raw unclassified headlines | **ORPHAN + HALF-WIRED — see §7** |
| Macro tracker matrix | Dhan | on demand | `daily_context` macro columns, `resonance` | **EVIDENCE-ONLY** |
| Scrip master | CDN CSV | Sat 09:30 | id-rot review card; builds `darling_ids.json` (the desk cannot quote without it) | **LOAD-BEARING (infra)** |
| Sector index bars | yfinance | **NEVER refreshed since 07-16** | `sector_trend.is_sector_bullish` → part of `regime_filters` `block_bullish` veto | **⚠️ LOAD-BEARING ON STALE DATA** |

### The three findings you should not skip

1. **`data/sector_index_bars.json` is stale by 20 days and feeds a live veto.**
   `regime_filters.advise()` composes `_sector_bearish(underlying)`, which reads
   `sector_trend`, which reads that file. Nothing on any cron refreshes it. A
   50/200-SMA read on 3-week-old bars is not wrong-loud, it is wrong-quiet.
   This is the single most dangerous stale artifact in the system.
2. **RSS is cost-safe by design and therefore useless by default.** Decision #75
   pinned `rss_backend` to inherit the global `ollama` backend so the VM spends
   nothing. Correct choice — but the consequence is that `data/rss_signals.jsonl`
   (183 KB and growing) holds headlines nobody classified and nobody reads.
3. **The annual-report chain is severed.** `report_downloader` still runs and
   still downloads PDFs; `annual_report_analyzer` — the only thing that read them
   — was archived to `research_archive/` on 2026-07-25. We are crawling NSE, at
   8–15s per ticker, to fill a folder no code opens.

---

## 3. PROCESSING — what happens to each input after it lands

| Input | Transform | Score | Store | Ignore |
|---|---|---|---|---|
| Quotes | stale-void >60s on indices mid-session; DH-9xx classification | — | `data/market_snapshot.json` (atomic, published read-model) | — |
| Bars | SMA50/200 + Wilder RSI (`src/indicators.py`) | trend/momentum verdict | `data/bars_cache.json` (stale, 07-09) | — |
| Chains | leg construction (`trade_planner` matrix) | SPAN margin, net delta/vega | `data/lake/chains/{nifty,banknifty}/` | strikes >50% from ATM rejected |
| VIX | banding (<13 / 13–16 / >16, `src/regime.vix_band`) | `span_stress_factor` 1.0/1.15/1.30 | stamped on every journal entry as `regime_vix` | — |
| Bhavcopy | NULL-honest parse (`' -'`→None), EQ/BE only | ATR, DMAs, anchored VWAP, HVN bands, pivots | `data/lake/bhavcopy/<date>.csv` + `data/darlings_levels.json` | — |
| F&O bundle | per-underlying OI + traded value | tier1 = top-25 by options traded value, minus banned | `data/fo_liquidity.json` | index F&O excluded |
| Deals | canonicalize client names, net direction (not raw count), marquee tag | `net_qty`, `net_value_rs`, affinity weights | `data/bulk_deals.json`, `data/deals_history.jsonl` (13.8 MB), `entity_affinity` table (19,824 rows) | — |
| Flows | derive net when absent | — | `data/fii_dii_flows.json` + lake `flows/` (17 days) | never guesses zeros |
| Financials | rupees→lakhs, consolidated preferred, revisions supersede | screener pass/fail; valuation z-scores → sigmoid → **1–100** | `data/lake/financial_results/<SYM>.json`, `data/darlings_valuation.json` | — |
| News | Gemini | `short_term_catalyst_score` + `long_term_macro_score` | `data/news_sentiment.json` + lake `news_daily/` (17 days) | — |
| Macro/indices | z20 per series, dxy_brent_corr60, DTW vs episode fingerprints | archetype + phase + similarity % | `data/lake/macro/<KEY>.csv` (19 series), `data/macro_regime.json`, append-only `logs/macro_regime_declarations.jsonl` (17 rows) | — |
| RSS | dedup vs `logs/rss_seen.jsonl` | **classification skipped on VM** | `data/rss_signals.jsonl` | everything downstream |
| Intraday | none | none | `data/lake/intraday_15m.jsonl` (25,782 lines, 2.8 MB, flat file) | everything |
| Corporate events | keyword classify into CATALYST / EXPANSION / STRUCTURAL_RISK / LEGAL_RISK | none | `data/lake/events/date=YYYY-MM-DD/` (**2,601 partitions**) | everything |

---

## 4. SCATTER MAP — where processed output lives, and whether anyone reads it

### 4a. `data/brain_map.db` — 22 tables (VM, live)

| Table | Rows (VM, 08-05) | Written by | Read by | Verdict |
|---|---|---|---|---|
| `events` | 1,250 | `brain_map.record_event` | `query_similar_events`, miners | READ |
| `outcomes` | 385 (**366 simulated / 19 real**) | `plan_tracker`, `simulator` | `performance`, `tuner`, `evolution`, miners | READ |
| `event_outcome_link` | 1,134 | `brain_map` | miners | READ |
| `simulated_trades` | 366 | `simulator` | `train_skeptic`, `evolution`, `mfe_mae_analyzer` | READ (**but see the 10× inflation caveat, §6**) |
| `daily_context` | **25** (07-11 → 08-04) | `daily_context.run_for_today` | `cooccurrence_miner`, `sequence_miner` — **which need 60** | READ-BLOCKED |
| `graph_edges` | 91 | `sleep_phase`, `entity_affinity` | `GraphEngine`, `vol_bridge` | READ |
| `entity_affinity` | 19,824 | `accumulate_entity_affinity` | `daily_context`, `evidence` | READ |
| `entity_affinity_ingested` | 3,111 | idempotency ledger | itself | INFRA |
| `evidence_snapshots` | **9** | `confluence/evidence.capture_for_entry` | `src/explain.py` | READ — **but only 9 snapshots for 25 journal entries** |
| `shadow_trades` | **11, all `BLOCKED_BY_RISK`** (5 resolved) | `exposure_gate` via `trial.record_block` | `opportunity_cost.collect()` | READ |
| `candidate_patterns` | **0** | `validation/registry` | `digest`, `shadow_runner`, `inspect` | **EMPTY** |
| `pattern_audit` | **0** | `validation/registry` | `digest` | **EMPTY** |
| `semantic_nodes` / `semantic_event_link` | 1 / 2 | `sleep_phase` | `sleep_phase` | VESTIGIAL |
| `account_state` | 1 row (start ₹200,000 · realized ₹39,423.99) | `portfolio_manager` | everything money-shaped | LOAD-BEARING |
| `margin_locks` | 23 (7 open) | `portfolio_manager`, `equity_desk` (`eqd:` prefix) | `positions`, `equity_desk.desk_state` | LOAD-BEARING |
| `equity_curve` | 18 | `portfolio_manager` | `brain_mcp` | READ (MCP only) |
| `account_events` | 6 | `portfolio_manager`, `firm_treasury` | `firm_mtm`, `bug_ledger` | READ |
| `treasury_state` | 1 row (equity budget ₹60,000) | `firm_treasury` | `equity_desk` | LOAD-BEARING |
| `wealth_lock_ledger` | 11 | `wealth_lock` | `api` | READ |
| `ingest_log` | 4 | `sleep_phase` | `sleep_phase` | INFRA |

### 4b. `data/` JSON artifacts

**Read again:** `market_snapshot.json`, `darling_tiers.json`, `darlings_levels.json`,
`darlings_valuation.json`, `darlings_queue.json`, `darling_ids.json`, `darling_pins.json`,
`fo_liquidity.json`, `bulk_deals.json`, `deals_history.jsonl`, `fii_dii_flows.json`,
`earnings_calendar.json`, `news_sentiment.json`, `brain_weights.json`, `macro_regime.json`,
`macro_playbooks.json` / `macro_templates.json` / `macro_strategies.json` /
`macro_fingerprints_cache.json`, `strategy_scoreboard.json`, `entity_affinity.json`,
`scrip_reconciliation.json`, `portfolio.json`, `journal.jsonl`.

**Write-only or dead:**

| Artifact | Size / rows | Status |
|---|---|---|
| `data/lake/intraday_15m.jsonl` | 25,782 rows / 2.8 MB | **no reader anywhere** |
| `data/lake/darlings_daily.jsonl` | 105 rows/day since 08-04 | **no reader** (built so entry zones are visible; nothing looks) |
| `data/lake/events/` | **2,601 day-partitions** | **no reader** |
| `data/rss_signals.jsonl` | 183 KB | **no reader**, and unclassified on the VM |
| `data/patience_basket.json` | 19 KB | **zero references in the entire tree** — superseded by `darling_tiers` |
| `data/lake/business_metrics/` | 21 dirs | orphaned with its archived module |
| `data/lake/fundamental_reports*` (4 variants incl. `_bench`, `_v11`) | 267+ dirs | orphaned with `annual_report_analyzer` |
| `data/sector_index_bars.json` | 1.2 MB, frozen 07-16 | **read, but never refreshed** |
| `data/bars_cache.json` | 351 KB, frozen 07-09 | read by `regime` backfill / `evolution` / `macro_shocks` |
| `data/macro_snapshot.json` | 929 B, 07-10 | hand-editable fallback, currently the only thing `brain_mcp` sees |
| `data/equity_desk.db.bak-*`, `equity_desk_snapshot.json.parked` | — | deliberately parked (#83), delete after clean observation |

### 4c. `logs/` ledgers (VM)

| Ledger | Size | Read by | Verdict |
|---|---|---|---|
| `problems.jsonl` | 23 KB | `ops_monitor`, `ceo_brief`, `bug_ledger` | READ |
| `autonomous_bug_report.jsonl` | **74 items, 37 KB, untriaged since 07-21** | nothing automatic | **BACKLOG** |
| `exposure_blocks.jsonl` | **645 rows, 110 KB** | nothing (`opportunity_cost` reads the DB table, not this) | WRITE-ONLY |
| `sizing_adjustments.jsonl` | 100 KB | `bug_ledger` (VETO lines only) | PARTLY READ |
| `greeks_snapshots.jsonl` | 10 KB | nothing | WRITE-ONLY |
| `affinity_advisories.jsonl` | 7 KB | nothing | WRITE-ONLY |
| `resonance_advisories.jsonl` | 9 KB, last write **2026-07-10** | nothing | DEAD |
| `equity_shadow_journal.jsonl` | 41 KB | `equity_shadow_proposer`, `export_trade_book.py` | READ |
| `macro_regime_declarations.jsonl` | 17 rows | `strategy_scorer`, `stage_b_tracker` | READ (append-only, immutable) |
| `macro_strategy_scores.jsonl` | **DOES NOT EXIST** | `strategy_scoreboard` | **Stage B has graded zero** |
| `deploy_log.jsonl` | 11 KB | `ceo_brief` | READ |
| `treasury_ledger.jsonl` | — | `bug_ledger` | READ |
| `discord_digest_queue.jsonl` | 821 B | `ceo_brief`, `eod_summary` | READ |
| `census_alerts.jsonl`, `rss_seen.jsonl`, `text_intel_calls.jsonl` | small | own writers (dedup memory) | INFRA |
| per-clerk outage ledgers (`bhavcopy_clerk`, `nse_results`, `integrated_results`, `macro_lake`, `indices_lake`, `index_history`, `scrip_master`, `report_downloader`) | small | `ops_monitor` pattern scan only | SEMI-READ |

---

## 5. SYNTHESIS — raw data to trade proposal, the actual wire

### Path A — the OPTIONS engine (the only path with real execution authority)

```
09:10  master_scheduler  waits for 09:15
        │
        ├─ market_loop.fetch_market_state
        │     ├─ dhan_guard: NIFTY/BANKNIFTY trend read (SMA50/200) + India VIX
        │     ├─ vol_bridge.compute_regime_overrides(graph_edges)  → risk_pct ×0.70 / wider wings
        │     └─ analysis/regime_filters.advise()   ← THE ONE analysis seam
        │           ├─ _distribution()   ← deals_history.jsonl (smart money distributing?)
        │           ├─ _sector_bearish() ← sector_index_bars.json  ⚠️ 20 DAYS STALE
        │           └─ crisis_regime()   ← VIX ≥25 / ≥15% d/d spike / macro_shocks window
        │
        ├─ options_proposer.run_headless(advisory=…)
        │     ├─ trade_planner.map_technical_to_strategy(trend × IV band)
        │     ├─ StrategyConstructor → real chain legs, real premiums
        │     ├─ adaptive_sizing.adjust_option_lots()  ← autopsy of our OWN resolved record
        │     ├─ exposure_gate.gate_entry()   ← ONE spread per underlying+direction
        │     │      └─ on block: trial.record_block → shadow_trades (opportunity cost)
        │     ├─ portfolio_manager.request_entry()  ← SPAN margin lock, VIX-stressed
        │     └─ confluence/evidence.capture_for_entry → evidence_snapshots
        │            └─ discovery/shadow_runner: fire registered patterns  (0 registered)
        │
        ├─ journal.jsonl  (status pending_approval)
        ├─ PAPER_AUTO_APPROVE=1 → decide_pending approves itself
        └─ live_bridge: intraday advisory exits + ONE sanctioned square-off (#69)

15:30  scheduler self-terminates
15:45  eod_summary   → Discord card
16:30  ceo_brief     → Discord card
nightly plan_tracker resolves against real daily high/low → outcomes → tuner → brain_weights
```

### Path B — the EQUITY desk (darlings)

```
Mac 19:15  patience_basket.eod_chain
   bhavcopy → fo_bhavcopy → dynamic_pricer (anchored VWAP buy zones, ATR stops)
            → valuation_scorer (1-100) → darling_tiers (7 tiers)
            → darlings_levels.json / darling_tiers.json  ── shipped to VM ──┐
Sat 10:00  weekly_recalibration: fresh filings → re-screen → No-Orphan pins  │
                                                                             ▼
VM 19:50   firm_treasury --rotate  → treasury_state.equity_budget_rs (₹60,000 today)
VM market hours  equity_desk (inside market_loop)
   darling in buy zone? → equity_entry_checks halt stack
        never_short_darling → liquidity_filter (fo_liquidity tier1/ban) → …
   → portfolio_manager.request_entry (eqd: prefixed margin_lock)
```

### Path C — the MACRO engine (declares, but touches nothing)

```
VM 19:50  macro_nightly
   macro_lake (FRED) → indices_lake (NSE) → macro_features._vector_at
   → macro_fingerprints (DTW vs episode library) → macro_regime.declare()
   → macro_regime.json + append-only macro_regime_declarations.jsonl (17 rows)
   → strategy_scorer: grade any declaration whose phase window has ELAPSED
        → macro_strategy_scores.jsonl   ← FILE DOES NOT EXIST YET. Zero graded.
   → strategy_scoreboard → strategy_scoreboard.json → ceo_language sentence on the cards
```

**Path C has zero execution authority by rule (Rule 5 / #63).** It appears on
the CEO card as a sentence and nowhere else.

### The honest picture of the synthesis

Of ~15 ingested feeds, **exactly four can change a trade**: quotes/bars,
option chain, VIX, and the two veto inputs (deals distribution + sector trend,
plus F&O liquidity on the equity side). Everything else is either evidence
recorded for later, or capture for a future that hasn't arrived.

---

## 6. DERIVED DATA — data we make out of data

| Derived thing | Made from | How | Why it exists | Live? |
|---|---|---|---|---|
| `brain_weights.json` | resolved BUY archetypes | `tuner.py` scores fresh-cross vs RSI-oversold | nudges `forecast.py` bias | yes, thin (needs 10 samples/archetype) |
| Adaptive sizing multipliers | our own resolved outcomes | `adaptive_sizing.py` Beta posterior centred on break-even | shrink size after a losing archetype; an EARNED veto can refuse | **yes — LIVE on both desks** |
| `evidence_snapshots` | 6 layers at proposal time | `confluence/evidence` | so `explain.py` can reconstruct *why* a trade was taken | yes, **only 9 rows** |
| `entity_affinity` (19,824) | `deals_history.jsonl` | co-occurrence of client ↔ ticker, decaying `concentrates_in` edges | "which promoter group accumulates where" | yes, feeds `daily_context` + evidence |
| `graph_edges` (91) | reviewed outcomes only (#34) | `sleep_phase.write_causal_links` via local Ollama on the **Mac** | `vol_bridge` regime classification | yes, but Mac-dependent |
| `daily_context` (25 frames) | vix + macro + news + deals + affinity + flows | one NULL-honest row/day | the substrate every miner mines | yes — **but 35 frames short of usable** |
| `simulated_trades` (366) | historical replay through the REAL pipeline | `simulator.py` as-of injection (#36) | train the skeptic; give the miners a corpus | yes — **P&L is ~10× inflated (synthetic chains, 62–79% generosity band). Never quote it as expected return.** |
| `skeptic_model.pkl` | `simulated_trades` | RandomForest, 0.60 balanced-accuracy ship gate (#44) | P(win) warning below 0.40 | **abstaining — no model file present** |
| Valuation scores (1–100) | filed financials, winsorized sector z-scores → sigmoid | `valuation_scorer` | the "cheap?" leg of a darling tier | yes |
| Darling levels (buy zone / stop / target) | bhavcopy bars | `dynamic_pricer`: anchored VWAP off the highest-volume up-day of 60 sessions, ATR-widened, HVN bands, confirmed pivots | equity desk entry & exit geometry | yes |
| 7-tier grade | levels ∩ valuation ∩ forensic flags | `darling_tiers` | the desk's single instruction per name | yes |
| Macro fingerprints + archetypes | 38y macro lake | banded DTW, clustered by macro ERA not human labels | "what regime is this?" | yes, advisory only |
| Auto-discovered episodes | 38y lake, ZERO human labels | `auto_discovery`: `co_stress` = 2nd-largest \|z\|, ≥3-channel floor; AND-gate over circular-shift + phase-randomized nulls | find regimes we never named | **working, 0 admissions** — COVID missed the bar by 0.008 |
| Opportunity cost | `shadow_trades` `BLOCKED_BY_RISK` rows | `opportunity_cost.collect()` | "are our gates saving money or costing it?" | yes, **n=5 resolved** |
| Evolution lineage | loss clusters in `simulated_trades` | Analyst→Critic dialectic on local Ollama, whitelisted params only (#49) | parameter mutation with 3 gates | Mac-only, Saturday 02:00 |

---

## 7. NET OUTPUT + THE HANGING-DATA TABLE

### What the system actually emits

| Output | Where | Cadence |
|---|---|---|
| Paper trade proposals + auto-approval | `journal.jsonl`, `margin_locks` | market hours |
| Intraday advisory exits + one sanctioned square-off | Discord + `plan_tracker` | market hours |
| Morning brief card | Discord | 08:05 weekdays |
| Daily suggestions digest | email | 08:00 weekdays |
| EOD summary card (MTM, active, net delta, firm MTM) | Discord | 15:45 weekdays |
| CEO brief (ops / issues / deploys / risk) | Discord | 16:30 weekdays |
| Ops health card | Discord | 20:30 daily |
| Weekly validation digest | Discord | Sat 10:00 |
| Weekly performance card | Discord | Sat 10:05 — **currently abstaining** |
| Darling tier summary | Discord | Mac EOD |
| MCP tools (9 read-only) | `.mcp.json` | on demand |
| Dashboard + gateway | Cloudflare tunnel | on demand |

### THE HANGING-DATA TABLE

| # | Orphan | Volume | Why it's hanging | KEEP-AND-USE / DELETE |
|---|---|---|---|---|
| 1 | `data/lake/intraday_15m.jsonl` | 25,782 rows, 2.8 MB, unbounded flat file | built as "substrate for a future intraday-feature layer"; that layer doesn't exist | **KEEP-AND-USE** — this is the only fuel a "temporary-trend" bucket will ever have. But **rotate it**: date-partition it like every other lake dataset before it eats the 2 GB of free disk. |
| 2 | `data/lake/events/` (corporate announcements) | 2,601 partitions | classified and stored, never joined to anything | **KEEP-AND-USE** — wire `STRUCTURAL_RISK` / `LEGAL_RISK` into `equity_entry_checks` as a halt. That is a 20-line change and it is the single highest-value orphan here. |
| 3 | `data/rss_signals.jsonl` | 183 KB unclassified | classifier is a no-op on the VM by cost design | **DECIDE, then act.** Either enable a cheap cloud backend for it or stop the 18:50 job. Storing unclassified headlines forever is the worst of both. |
| 4 | `data/fundamental_reports/` PDFs | 267+ ticker folders | its reader (`annual_report_analyzer`) is in `research_archive/` | **KEEP THE PDFs. ~~STOP THE CRAWL~~ — CORRECTED 2026-08-05.** The original line was WRONG on two counts, verified by grep: (a) `report_downloader` is on **NO schedule** — not in `setup_cron.sh`, `CRON_SETUP.md`, the Mac crontab, or any script; there is no crawl running to stop. (b) The module is **LOAD-BEARING**: `bhavcopy_clerk`, `fo_bhavcopy`, `integrated_results` and `nse_results` all import its `_fetch_bytes`/`_fetch_json` as the shared NSE safe-crawl layer, so removing it would break four live clerks. Only its unscheduled PDF-download CLI is idle. The open question is just whether to keep the already-downloaded PDFs — an owner call, no code change either way. |
| 5 | `data/patience_basket.json` | 19 KB | zero references anywhere in the tree | **DELETE** |
| 6 | `data/lake/business_metrics/`, `fundamental_reports_auto*`, `_bench`, `_v11` | ~25 dirs | orphaned with archived modules | **DELETE** (they are regenerable and the modules are archived) |
| 7 | `logs/resonance_advisories.jsonl` | last write 2026-07-10 | `resonance.py` is composed nowhere on a live path today | **DECIDE** — either wire resonance into the exit advisory or archive the module. Currently it is a dead ledger of a dead layer. |
| 8 | `logs/greeks_snapshots.jsonl` | 10 KB | `portfolio_greeks` writes, nothing reads | **KEEP-AND-USE** — this is your vega/delta history. One line on the weekly card ("net vega vs budget, 7-day range") makes it live. |
| 9 | `logs/exposure_blocks.jsonl` | **645 rows** | superseded by the `shadow_trades` route | **KEEP AS AUDIT, REPORT THE COUNT.** 645 blocks vs ~25 entries is the most under-reported number in the system (§9). |
| 10 | `logs/affinity_advisories.jsonl` | 7 KB | write-only | **KEEP-AND-USE or DELETE** — decide whether affinity is an input or a curiosity. |
| 11 | `data/lake/darlings_daily.jsonl` | 105/day | built 08-04 so entry zones are visible; no consumer written yet | **KEEP-AND-USE** — finish the job it was built for (entry-zone proximity on the morning brief). |
| 12 | `data/sector_index_bars.json` | 1.2 MB, frozen 07-16 | read by a **live veto**, refreshed by nothing | **FIX, NOT DELETE.** This is a bug, not an orphan. |
| 13 | `data/macro_snapshot.json` | 929 B, 07-10 | hand-editable fallback that has become the actual value | verify `macro_tracker`'s live path still works |
| 14 | `logs/autonomous_bug_report.jsonl` | **74 items** | the Thursday Protocol that was supposed to triage them never ran | **TRIAGE** — this is a standing owner directive, 15 days overdue |
| 15 | `simulated_trades` P&L | 366 rows | ~10× inflated by synthetic chains | **KEEP for mechanics, NEVER quote as return.** Add the caveat to any card that shows it. |

---

## 8. USAGE FREQUENCY TAGS

| Artifact | Tag | If DORMANT — why does it exist? |
|---|---|---|
| `market_snapshot.json` | DAILY (per cycle) | — |
| `journal.jsonl`, `margin_locks`, `account_state`, `treasury_state` | DAILY | — |
| `darling_tiers.json`, `darlings_levels.json`, `darlings_valuation.json` | DAILY (Mac EOD) | — |
| `fo_liquidity.json` | DAILY | — |
| `bulk_deals.json`, `deals_history.jsonl` | DAILY | — |
| `news_sentiment.json` | DAILY | — |
| `macro_regime.json`, `macro_regime_declarations.jsonl` | DAILY | — |
| `daily_context` (25 rows) | DAILY write / **DORMANT read** | Accumulating history for future pattern-mining — **explicitly**. The miners need 60 frames; at ~1/trading-day that is roughly mid-September. This is the single dependency gating Department 5's existence. |
| `sizing_adjustments.jsonl` | DAILY | — |
| `problems.jsonl`, `deploy_log.jsonl` | DAILY | — |
| `intraday_15m.jsonl` | DAILY write / **DORMANT read** | Accumulating history for a future intraday-feature layer. Stated intent, no owner, no date. |
| `darlings_daily.jsonl` | DAILY write / **DORMANT read** | Built 08-04 for entry-zone visibility; consumer not written yet. |
| `lake/chains/` | DAILY write / **DORMANT read** | Legitimate: decision #36 — historical option chains cannot be bought later. Buy them now, use them whenever. |
| `lake/events/` (corporate announcements) | **DORMANT** (no fetch on cron either — backfill only) | No stated reason. This is a genuine gap, not a strategy. |
| `rss_signals.jsonl` | DAILY write / **DORMANT** | Cost-safety by design; the consequence was not intended. |
| `entity_affinity` | WEEKLY-ish | — |
| `graph_edges` | WEEKLY (Mac-dependent) | — |
| `financial_results/`, `darlings_queue.json` | WEEKLY (Sat) | — |
| `scrip_reconciliation.json` | WEEKLY (Sat) | — |
| `evolution_lineage.json` | WEEKLY (Sat 02:00, Mac) | — |
| `greeks_snapshots.jsonl` | 2-HOURLY write / **DORMANT read** | No stated reason. |
| `exposure_blocks.jsonl` | DAILY write / **DORMANT read** | Superseded by `shadow_trades`; kept as raw audit. Legitimate — but the *count* should be reported. |
| `equity_curve` (18 rows) | Per settlement / DORMANT read | Only the MCP reads it. This is the drawdown series the reports don't show (§9). |
| `evidence_snapshots` (9) | Per entry / rare read | `explain.py` on demand. Fine. |
| `candidate_patterns`, `pattern_audit` | **DORMANT — EMPTY** | Blocked on `daily_context` depth. Legitimate, but it means the weekly validation digest currently reports on an empty set. |
| `macro_strategy_scores.jsonl` | **DOES NOT EXIST** | Legitimate: nothing has matured. Stage B needs 60 sessions; 17 declarations exist and none has completed its phase window. |
| `simulated_trades` (366) | MONTHLY (skeptic retrain, evolution) | — |
| `bars_cache.json`, `sector_index_bars.json` | **DORMANT write, LIVE read** | ⚠️ inverted and dangerous — see §2. |
| `fundamental_reports/` PDFs | **DORMANT** | Reader archived. |
| `patience_basket.json`, `business_metrics/` | **DEAD** | Superseded. |
| `resonance_advisories.jsonl` | **DEAD** (07-10) | Layer not composed anywhere live. |
| `autonomous_bug_report.jsonl` | DAILY write / **DORMANT read** | Waiting on a human protocol that has not run. |

---

## 9. REPORTING GAP AUDIT

*"I want to trust the system but there are gaps in reporting I can't name."*
Here they are, named.

### The reports we send today

| Card | When | Contains |
|---|---|---|
| Morning brief | 08:05 | macro sentence, watchlist results-proximity, book going in |
| Suggestions digest (email) | 08:00 | per-ticker SMA/RSI read |
| EOD summary | 15:45 | today's MTM P&L, resolved count, brain W/L, active spreads/equities, net delta, firm MTM + absolute return, equity desk line |
| CEO brief | 16:30 | Operations (heartbeats), Issues, Deployments, Risk & Capital, Macro read |
| Ops health | 20:30 | problem lines, silent jobs, host telemetry |
| Validation digest | Sat 10:00 | lifecycle counts, validated/killed, placebo FDR, Stage-B block, opportunity cost |
| Performance card | Sat 10:05 | Sharpe/Sortino/max-DD/win-rate — **only if n ≥ 20; currently silent** |
| Darling tiers | Mac EOD | tier movements |

**Ceiling:** `notifier.budget_gate` caps Discord at **5 cards/day** (#84).
Anything else spools to `discord_digest_queue.jsonl` and rides inside the next
digest. This is a good rule, but it means every new report competes for a slot.

### (a) Questions a portfolio owner would ask that our reports CANNOT answer

| Question | Why we can't answer it today |
|---|---|
| **"What is my current drawdown from peak?"** | `equity_curve` has the data (18 rows). No card shows it. The EOD card shows absolute return only. |
| **"How much of this ₹39,424 came from luck vs the strategy?"** | Nothing reports concentration. In fact **all 19 real trades are the same structure in the same direction** (`bear_put_spread`). One market regime produced the entire track record. No card says this. |
| **"How many trades did we NOT take, and what would they have made?"** | 645 exposure blocks; `opportunity_cost.collect()` exists and has n=5 resolved. It appears **only** in the Saturday digest, never in a daily card. |
| **"Is the system learning anything?"** | `candidate_patterns` = 0. The Saturday digest reports on an empty registry and reads as "quiet", not as "never started". |
| **"How long has this position been open?"** | `eqd:a1e1f4a0` has been open since **2026-07-22 — 14 days**; options lock `17400708` since 07-24. No card reports position age. `book_context.position_dossier` computes age but is CLI-only. |
| **"Are the darlings near their buy zones? Should I be adding?"** | `darlings_daily.jsonl` was built for exactly this on 08-04 and nothing reads it. |
| **"Which of my data feeds went stale?"** | Heartbeats check *"was the log touched today"*, not *"is the artifact fresh"*. `sector_index_bars.json` has been stale 20 days and every card said ✅. |
| **"What is my worst realistic day?"** | No VaR, no stress line. `portfolio_greeks` computes a 5-point vega shock and writes it to a file nobody reads. |
| **"Is the ₹2L pool fully deployed or sitting idle?"** | Desk cash is on the CEO card; the options side's free margin is not. |
| **"What did the macro engine say last month, and was it right?"** | 17 declarations, 0 graded. `strategy_scoreboard` has no file to roll up. |

### (b) Where reports show a number without its confidence or context

1. **`Absolute return +18.03%` on the EOD/CEO card.** No `n`, no drawdown, no
   dispersion, no "one strategy, one direction". Nineteen trades in one regime
   is a story, not a return. The card even guards CAGR ("annualizing a days-old
   number would be noise") — the same honesty is missing for the return itself.
2. **`Brain Map W/L: 12W/7L`.** A raw win-rate with no Wilson lower bound —
   even though `validation/stat_gates.wilson_lower` exists and is described in
   MODULES.md as *"every displayed win-rate's honest number"*. The one card the
   owner reads daily is the one place it isn't applied.
3. **`Firm MTM Rs.236,056`** mixes realized (settled), options unrealized
   (snapshot-first mark ladder) and equity unrealized (live quote). Three
   different confidence levels, one number, no marker for which leg is a
   modelled mark vs a real quote.
4. **`Macro regime matches Taper Tantrum (10 episodes, 73% similarity)`.**
   The gating is genuinely good (`ceo_language` refuses to name an analog
   unless `declare()` declared one) — but "73% similarity" has no null. Given
   `auto_discovery` found that even COVID's cross-asset co-movement sits at
   ~the 95th percentile of *chance* alignment, a bare similarity % overstates.
5. **Equity desk `unrealized +Rs.278`** on ~₹42,727 deployed = +0.65% after
   14 days open. Shown without holding period or cost basis context.
6. **`✅ All 6 jobs due by now ran`** — "ran" means the log file was touched.
   A job that ran and did nothing useful is indistinguishable from a good day.

### (c) What happens silently that should be reported

| Silent event | Evidence | Owner sees |
|---|---|---|
| **645 risk-gate blocks** | `exposure_blocks.jsonl` | one Discord note per (ticker, direction) per day, max — no total, ever |
| **Discovery miner skipped 17 consecutive nights** | `.discovery_nightly_state.json` `consecutive_skips: 17`, `depth.frames 25 < 60` | one note every 7th skip, buried |
| **Performance card abstains** | `performance.run()` posts *only* on `verdict == "ok"` | **nothing at all.** Deliberate ("no weeks of not-enough-data spam") — but the effect is that Saturday looks identical whether we have a track record or not |
| **74 bugs in the autonomous ledger** | `autonomous_bug_report.jsonl` | nothing since 07-21 |
| **Stage B has graded zero declarations** | `macro_strategy_scores.jsonl` absent | the digest's Stage-B block renders, but "0 graded" reads as normal |
| **Dhan throttle sleeps** | `_throttle()` sleeps without logging | explicitly disclaimed on the CEO card, still unmeasured |
| **`live_quote` failures** | fixed 08-04 to name reasons on stderr — but stderr is not a ledger | the 15:45 EOD blank-price event still has **no confirmed cause** |
| **Data-feed staleness** | `sector_index_bars.json` 20 days old, `bars_cache.json` 27 days old | ✅ on every card |
| **Adaptive-sizing vetoes** | `sizing_adjustments.jsonl` → `bug_ledger` | folded into a ledger, spooled to a digest section, easily missed |
| **`evidence_snapshots` captured for only 9 of 25 entries** | table count | nothing |

### Concrete fixes — smallest first

| # | Fix | Effort | Why it's worth it |
|---|---|---|---|
| 1 | Add `n=19` and the **Wilson lower bound** next to every displayed win-rate (`eod_summary.build_eod_card`, `ceo_brief._risk_field`). `stat_gates.wilson_lower` already exists. | ~10 lines | The single biggest honesty upgrade available. |
| 2 | Add **one drawdown line** to the EOD card from `equity_curve`: `peak ₹X · now ₹Y · DD -Z%`. | ~15 lines | Answers the #1 unanswerable question. |
| 3 | Add **`blocked today: N (total N)`** to the CEO card from `exposure_blocks.jsonl`. | ~10 lines | Turns the most-hidden number into a daily one. |
| 4 | Add **position age** to the risk field (`14d open` next to each). `book_context` already computes it. | ~10 lines | Catches forgotten positions. |
| 5 | Make the **performance card post its abstain once a week** with the countdown (`17/20 — 3 more resolved trades`). | 1-line change to the `verdict == "ok"` guard | Silence currently looks like health. |
| 6 | Add a **freshness sentinel** to `ops_monitor`: for a named list of artifacts (`sector_index_bars.json`, `bars_cache.json`, `darling_tiers.json`, `macro_regime.json`, `fo_liquidity.json`), flag any file older than its expected cadence. | ~40 lines | This is the class of bug that made ✅ lie for 20 days. |
| 7 | Add a **"strategy concentration" line**: `19 trades · 1 structure (bear_put_spread) · 1 direction`. | ~15 lines | Names the elephant automatically, forever. |
| 8 | Put **`consecutive_skips`** and **`frames X/60`** on the CEO card whenever the miner skips. | ~10 lines | Makes the learning layer's dormancy visible instead of inferable. |
| 9 | Add a **weekly Data Health card**: rows captured per feed vs expected, per-clerk outage codes, disk %. | ~80 lines, 1 Discord slot | Department 1 is our best department and has no report of its own. |
| 10 | Triage `autonomous_bug_report.jsonl` (74 items) and make `bug_ledger` emit a **count + top-3** into the CEO card. | half a session | 15 days overdue against a standing directive. |

Fixes 1–5 are all sub-20-line edits inside existing card builders with existing
tests. They can land in one session and they close most of §(a) and §(b).

---

## 10. SESSION HYGIENE — one file, one page

**The problem, measured.** A new session is told (CLAUDE.md Rule 1) to read
`HANDOVER.md` (165 KB) and `PROJECT_TIMELINE.md` (17 KB), then likely
`ARCHITECTURE.md` (52 KB), `MODULES.md` (170 KB), `DECISIONS.md` (136 KB).
That is **~540 KB / ~140k tokens of re-learning before the first line of work.**
Parallel sessions each pay it, and (ledger Issue 23, 2026-08-04) they also
collide: one session swept another's six files into its commit.

### The proposal — `CONTEXT.md` at repo root, hard-capped at 60 lines

Not a new document to maintain by hand. It is a **generated header** plus a
**hand-written 5-line block**, regenerated by `scripts/wrap_session.sh`
(which already runs at session end and already stages exactly one file).

```markdown
# CONTEXT.md — read this first. If it and HANDOVER.md disagree, this wins.
<!-- AUTO — regenerated by wrap_session.sh. Do not hand-edit below this line. -->
AS OF: 2026-08-05 14:20 IST · main @ f65f583 · suite 1,700 green
LIVE:  VM 28 cron + 3 systemd · Mac 3 cron + 2 LaunchAgents · MCP 9 tools
MONEY: pool ₹2,00,000 · realized ₹39,424 · open 7 (4 options, 3 equity)
       equity budget ₹60,000 · 19 resolved real trades (12W/7L)
CLOCKS: Stage-B 17 declarations / 0 graded · daily_context 25/60 frames
        discovery skipped 17 consecutive nights · bug ledger 74 untriaged
STALE:  sector_index_bars.json (20d, feeds a LIVE veto) · bars_cache.json (27d)
<!-- /AUTO -->

## ACTIVE WORK (hand-written, max 5 lines, delete when done)
- [VM lane] watching for the next `live_quote` failure to name the 15:45 cause
- [Mac lane] first unattended on-demand Ollama run — verify trap released port

## DO NOT TOUCH
- logs/macro_regime_declarations.jsonl, macro_strategy_scores.jsonl, outcomes — APPEND-ONLY
- research_archive/** — never import from src/
- Macro engine → capital wiring — Dept 5 gate, never a code change
- Ollama background agent must stay DISABLED (System Settings → Login Items)

## SESSION HANDOFF (last 3 only, newest first)
- 2026-08-04 VM  · d7d888f · desk price capture + bhavcopy→VM · OPEN: 15:45 cause unknown
- 2026-08-04 Mac · ca6558f · Ollama on-demand only · OPEN: RunAtLoad on the miner
- 2026-08-01 Mac · —       · AD-2 closed, 0 admissions · OPEN: motif gate unbuilt
```

**Handoff line format (one line, always the same 5 fields):**
`DATE · LANE · SHA · what changed in ≤8 words · OPEN: the one unresolved thing`

### Branch discipline (tightening what `docs/dev_workflow.md` §3 already says)

1. `main` stays deployable — the VM only ever pulls `main`. Unchanged.
2. **One lane = one department = one commit scope.** Unchanged.
3. **NEW — the anti-Issue-23 rule: never `git add -A`, never `git add .`, never
   `git commit -a`.** Stage explicit paths only. Issue 23 happened because one
   session staged the whole working tree while another was live in it.
4. **NEW — before your first commit, `git log --oneline -3` and check
   `CONTEXT.md`'s AS-OF sha.** If the sha moved since your session started,
   another lane is live: stage narrowly and say so in the commit body.
5. `lovable-ui` never auto-merges. Unchanged.

### Cost of the protocol

`CONTEXT.md` at 60 lines is **~2 KB** against ~540 KB. A session reads it, and
reads the big documents only for the department it is about to touch. The
generated block costs `wrap_session.sh` about 30 lines of shell over queries
this document already proves are one-liners.

---

## Appendix — document drift found during this audit

| Document | Says | Reality |
|---|---|---|
| `CRON_SETUP.md` | "All 25 jobs" | 28 (HANDOVER 08-04 confirms 28; the table lists 26 rows) |
| `CLAUDE.md` Rule 6 | "1,589 tests" | 1,700 (already noted in HANDOVER 08-04) |
| `MODULES.md` header | "Current as of 2026-07-25 · 141 modules · 1,589 tests" | stale by 11 days |
| `ARCHITECTURE.md` Dept 1 | lists `report_downloader` as feeding "Department 8's forensic reader" | that reader is in `research_archive/` since 07-25 |

None of these is dangerous on its own. Together they are why `CONTEXT.md`
should be generated rather than written.

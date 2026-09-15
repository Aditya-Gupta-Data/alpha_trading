# SYSTEM_BLUEPRINT.md — the whole desk on one page, and where it is weak

*Written 2026-09-12 for the Architect's structural review (CTO pass), from
the code as deployed at `1deb165` on the VM, the VM's own `brain_map.db` and
`journal.jsonl` (copied 2026-09-11 17:56 IST), `scripts/setup_cron.sh`, and the
three department maps this file supersedes for the bird's-eye view:
`ARCHITECTURE.md` (the 8 departments, 08-11), `SYSTEM_XRAY.md` (the data
audit, 08-16) and `MODULES.md` (one line per file). Every claim below names its
file; where the record is unknown it says so (RULE 3).*

**Read this first.** The desk is a paper-money, single-box, poll-driven
research engine that ingests ~15 outside feeds honestly, proposes one family
of defined-risk index spreads plus a funded equity "darling" book, gates both
through a real capital layer, and records everything it does into an
append-only memory that a statistical court is supposed to grade. Departments
1 (Data) and 3 (Risk) are institutional-grade. Department 5 (Validation) has
never tried a case. That asymmetry is the gap analysis in §7.

```
 OUTSIDE WORLD ──► 1. INGESTION ──► lake / json / jsonl / brain_map.db
                        │
                        ▼
                   8. ANALYSIS   (regime, smart money, darling tiers, macro clock)
                        │  advisory verdicts only (veto / allow / abstain)
                        ▼
   ┌─── 2. DECISION ────────────────────┐    ┌── SHADOW PROVING PATHS ─────────┐
   │ options_proposer  (live capital)   │    │ insolvency_short  glassbreaking  │
   │ equity_desk       (live capital)   │    │ equity_shadow_proposer (telemetry)│
   └──────────────┬─────────────────────┘    │ h4_shadow  macro Stage-B  placebo │
                  ▼                          └──────────────┬──────────────────┘
   3. RISK & CAPITAL  exposure gate → halt latch → margin lock → journal        │
                  │                                                              │
                  ▼                                                              ▼
   4. MEMORY   journal.jsonl · brain_map.db (outcomes, events, evidence)  ◄── shadow_trades
                  │
                  ▼
   5. VALIDATION   registry → trial → stat_gates (Wilson LB vs structural null)
                  │
                  ▼
   6/7. TELEMETRY & INTERFACES   ops_monitor · ceo_brief · eod · Discord · gateway
```

The composition law (decision #63) binds every arrow: only Decision proposes,
only Risk blocks, every other layer annotates, authority is earned in
Validation. The one carve-out: a Department-8 verdict may be hand-wired only
while it is strictly risk-reducing (a veto).

---

## 1. Ingestion & data flow

### 1.1 Sources → module → cadence → machine → store

| Source | Module | When | Where | Lands in |
|---|---|---|---|---|
| **Dhan** quotes, daily OHLC, option chains, expiry list, India VIX | `src/dhan_client.py` (the raw SDK wrapper, data endpoints only) behind `src/dhan_guard.SafeDhanClient` (the one market-data door) | on demand, host-wide throttle `data/.dhan_throttle` ≥ 1.1 s between calls | VM only (one token per client id, #48; renewed 07:00 by `src/renew_token.py` from GCP Secret Manager) | in-memory; marks published to `data/market_snapshot.json` by `live_bridge` |
| Dhan 15-min price tape (watchlist + desk book) | `src/ingestion/intraday_tracker.py` | */15 09:00–15:59 M-F, self-gated to market hours | VM | `data/lake/intraday_15m.jsonl` (flat, append) |
| Dhan daily all-darlings close | same, `--darlings` | 15:50 M-F | VM | `data/lake/darlings_daily.jsonl` |
| Dhan EOD option chains | `src/ingestion/chain_archiver.py` | 15:40 M-F | VM | `data/lake/chains/<slug>/date=…/*.jsonl.gz` |
| Dhan live candles from the loop | `src/live_bridge.py` | per cycle | VM | `data/lake/candles/<slug>/date=…` |
| Dhan MCX + global index EOD | `src/ingestion/cross_asset.py` | 19:40 daily | VM | `data/lake/cross_asset/`; outage codes `logs/cross_asset.jsonl` |
| **NSE** equity bhavcopy (OHLCV, delivery %) | `src/ingestion/bhavcopy_clerk.py` | 19:15 daily `--backfill 5` | VM (since 08-04) | `data/lake/bhavcopy/YYYY-MM-DD.csv` |
| NSE F&O bundle (fo, secban, fovolt) | `src/ingestion/fo_bhavcopy.py` | inside the Mac sync agent | **Mac only** (NSE bot-walls datacentre IPs) | `data/lake/fo_bhavcopy/`, derived `data/fo_liquidity.json` |
| NSE bulk/block deals | `src/ingestion/deals_tracker.py` | 19:30 daily | VM | `data/bulk_deals.json`, `data/deals_history.jsonl`, lake `deals_raw`/`deals_census` |
| NSE FII/DII flows | `src/ingestion/flows_tracker.py` | 19:35 daily | VM | `data/fii_dii_flows.json`, lake `flows` |
| NSE corporate announcements | `src/ingestion/corporate_events.py` | 19:25 daily | VM | `data/lake/events/date=…` |
| NSE results calendar | `src/ingestion/earnings_calendar.py` | 19:20 daily | VM | `data/earnings_calendar.json`, lake `earnings` |
| SEBI integrated filings (XBRL) / legacy results / annual-report PDFs | `integrated_results.py`, `nse_results.py`, `report_downloader.py` | manual, Mac | Mac | `data/lake/financial_results/`, `data/fundamental_reports/` |
| Dhan **public** scrip master (no token) | `src/ingestion/scrip_master.py` | Sat 09:30 + every sync run (7-day guard) | Mac | `data/darling_ids.json`, `data/scrip_reconciliation.json` |
| FRED macro (Brent, DXY-broad, USDINR, DGS10) + NSE index history | `macro_lake.py`, `indices_lake.py` inside `macro_nightly` | 19:50 daily | VM | `data/lake/macro/<KEY>.csv` |
| Google-News → Gemini sentiment | `src/news_processor.py` | 19:10 daily | VM | `data/news_sentiment.json` (v3 dual-horizon) |
| yfinance sector index bars | `scripts/fetch_sector_bars.py` | every sync run (180-min throttle) | **Mac only** | `data/sector_index_bars.json` → shipped to VM |
| Publisher RSS | `rss_ingester.py` | **disabled 08-05**; file + ledger orphaned | — | `data/rss_signals.jsonl` (unread) |

The **Mac → VM lane** (`scripts/mac_auto_sync.sh`, LaunchAgent
`com.aditrader.sync`, login + hourly, one real run per 180 min) ships exactly
seven files over `gcloud`: `sector_index_bars.json`, `darlings_valuation.json`,
`darlings_queue.json`, `darling_pins.json`, `fo_liquidity.json`,
`darling_ids.json`, `bars_cache.json`. It deliberately does **not** ship
`darling_tiers.json` / `darlings_levels.json` (VM-native since 08-11). The
reverse lane pulls `market_snapshot.json` down so the Mac never spends a Dhan
call. Known trap: `gcloud` under cron picks macOS python 3.9 unless
`CLOUDSDK_PYTHON` is pinned (`config.gcloud_env()`).

### 1.2 Cleaning and adjustment

- **Corporate actions** (`src/ingestion/corporate_actions.py`, decision #90):
  backward adjustment only — bars strictly before an ex-date are divided by the
  cumulative ratio; the ex-date bar and everything after stay the raw exchange
  print, so no live mark is ever an adjusted number. Authority order:
  `config/corporate_actions.json` (49 seeded rows, none yet
  `verified_against_nse_circular`) → lake-evidence `detect_candidates()` only
  when a caller passes `adjust="auto"` → nothing (the cliff stays visible; a
  ratio is never invented). Dividends and rights are not adjusted.
- **Scrip-master reconciliation** (`scrip_master.py`): every
  `SECURITY_ID_MAP` id diffed weekly; `symbol_mismatch` is the dangerous verdict
  (born from the LTIM delist and TATAMOTORS demerger, Issues 14/15).
- **Abstention is a value.** Every clerk parses NSE blanks to `None`, logs a
  missing day as an honest 404, interpolates nothing. Downstream: Sharpe on
  n<20 is `None` (#72), an un-priceable leg is excluded and counted (#71), a
  stale snapshot reads `None`, a stale valuation greys a darling to `ungraded`.
- **Freshness** is policed twice: natively at the consumer
  (`equity_desk.TIERS_MAX_AGE_DAYS=3` → no new entries; `IDS_MAX_AGE_DAYS=14`
  → unmarked; `equity_entry_checks.LIQUIDITY_MAX_AGE_DAYS=7` → fail closed;
  `market_snapshot.read(max_age)` → None) and centrally by
  `src/staleness_guard.py` (13 registered artifacts, two policies: `IGNORE`
  drops a consumer's opinion, `MONITOR` alerts only; the guard itself fails
  safe to "stale").

### 1.3 Storage map

| Store | Kind | Writer | Mutability |
|---|---|---|---|
| `data/lake/<dataset>/date=YYYY-MM-DD/*.jsonl.gz` | partitioned lake (`src/lake.py`: `write_partition` atomic replace, `append_rows`, `archive_blob` sha-addressed) | ingestion only; everything else scans | one-way door |
| `data/lake/bhavcopy/*.csv`, `macro/*.csv`, `financial_results/`, `fundamental_reports/` | non-partitioned lake dirs | clerks | rebuilt per day |
| `data/journal.jsonl` | the trade ledger of record (three tolerated schema generations) | `src/journal.py` (`log`, `update_entry` under `flock`) | **append-only**; outcomes filled once |
| `data/brain_map.db` | SQLite memory, 22 tables (§2.1) | one owner module per table | mixed; outcomes/events/ledgers append-only |
| `data/portfolio.json` | paper cash + holdings | `src/portfolio.py` | overwrite |
| `data/market_snapshot.json` | the shared live read-model (~60 s) | `live_bridge` only, atomic | overwrite |
| `data/*.json` artifacts (darlings_*, fo_liquidity, macro_*, news_sentiment, brain_weights, bulk_deals, flows, earnings, sector bars) | derived state | one producer each | rebuilt |
| `logs/macro_regime_declarations.jsonl`, `logs/macro_strategy_scores.jsonl`, `outcomes` | the forward-test record | `macro_regime`, `strategy_scorer`, `brain_map` | **append-only and immutable (RULE 3)** |
| `logs/*_shadow.jsonl`, `treasury_ledger`, `exposure_blocks`, `sizing_adjustments`, `greeks_snapshots`, `equity_shadow_journal`, `proposal_ledger`, `problems`, `autonomous_bug_report`, `discord_digest_queue` | operational ledgers | named module each | append |
| `config.json` + `config/` | versioned, non-secret tunables and data (watchlist, sector universe, corporate actions, macro episodes) | humans | git |
| `.env` (Mac: full V2 credential set; VM: client id + 24 h token only) | secrets | `renew_token` | never in git |

---

## 2. Processing & memory

### 2.1 `data/brain_map.db` — 22 tables, one owner each

| Table | Holds | Owner (writer) | Rows on VM 09-11 |
|---|---|---|---|
| `events` / `event_outcome_link` | one row per observation (news, signal, chart tag) and the glue to resolutions | `brain_map.record_event` / `record_resolved_entry` | 1,270 / 1,166 |
| `outcomes` | one row per resolved trade + post-mortem + regime | `brain_map.record_outcome` via `plan_tracker.record_post_mortem` | 406 (≈366 sim + real) |
| `simulated_trades` | Phase-7 simulator replays, `sim:` refs, full friction stack | `simulator.py` | 366 (~10× P&L-inflated by synthetic chains — never quote as return) |
| `graph_edges` | causal triples with decay | `graph_engine.add_edge` (mined by `edge_miner`), decayed by `decay_engine` (Task K) | 160 |
| `semantic_nodes`, `semantic_event_link`, `ingest_log` | journal-text consolidation (sleep phase A/B/C, Ollama only) | `sleep_phase` | 1 / 2 / 4 |
| `entity_affinity`, `entity_affinity_ingested` | client↔group deal accumulation | `knowledge_graph/entity_affinity` (Task F) | 20,120 / 3,151 |
| `daily_context` | the Market Frame: one NULL-honest row per session (VIX band, macro marks, news net, deals census, FII/DII, affinity) + JSON payload | `daily_context.py` (Task G, 20:00) | 63 frames |
| `candidate_patterns`, `pattern_audit` | every mined hypothesis, frozen definition, governed status; the FDR denominator | `validation/registry` | **9 rows, all `CANDIDATE`** / 45 |
| `shadow_trades` | prospective fires of registered patterns, blocks, signals | `validation/trial` (`record_shadow_fire`, `record_block`, `record_signal_fire`) | 103: 86 `BLOCKED_BY_RISK`, 17 `SIGNAL_SHADOW`, **0 organic pattern fires** |
| `placebo_ledger` | seeded information-free hypotheses, the measured false-discovery rate | `validation/placebo` | **0** |
| `evidence_snapshots` | the 6-layer Evidence stamped at proposal time | `confluence/evidence` | 30 |
| `account_state` (singleton), `margin_locks`, `equity_curve`, `account_events` | the firm's one cash pool, per-entry locks (`eqd:` prefix = equity desk), marks, halts | `portfolio_manager` | 1 / 60 (13 open) / 48 / 67 |
| `treasury_state` (singleton) | equity desk's routed budget | `firm_treasury` | 1 |
| `wealth_lock_ledger` | 50 % profit sweeps earmarked to GOLDBEES (paper) | `wealth_lock` | 16 |

### 2.2 The nightly processing clock (VM, IST)

```
15:35 main · 15:40 chain_archiver · 15:45 eod_summary · 15:50 darlings tap · 16:30 ceo_brief
19:10 news → 19:15 bhavcopy → 19:18 dynamic_pricer → 19:20 earnings → 19:22 darling_tiers
19:25 events → 19:30 deals → 19:35 flows → 19:40 cross_asset → 19:45 daily_archiver
19:50 macro_nightly (M1 features → M4 declare → Stage-B score)
19:56 firm_treasury --rotate
20:00 sleep_phase  (A/B/D need Ollama → VM runs G daily_context, H CUSUM, I shadow-resolve,
                    J h4_shadow, K decay)
20:20 discovery.nightly  (gated: heartbeats green + no INGESTION problems + ≥ 40 frames)
20:30 ops_monitor · 20:40 bug_ledger · Sat 10:00 validation.digest · Sat 10:05 performance
```

### 2.3 Miners and engines (all with zero execution authority)

| Engine | Input → output | Gate | State today |
|---|---|---|---|
| `discovery/nightly` → `run_miners` → `cooccurrence_miner` + `sequence_miner` | resolved `outcomes` × `daily_context` tags → `CANDIDATE` rows | health + depth (`MIN_CONTEXT_FRAMES` 60→50→40, #87/#88) + every threshold in `validation/stat_gates` | first pass 08-18; 9 candidates, all still CANDIDATE |
| `discovery/shadow_runner` | each real proposal's tag picture vs registered patterns → `shadow_trades` with `host_ref` | resolves by inheriting the host outcome (Task I) | 0 organic fires |
| `validation/h4_shadow` (Task J) | pyramid-lookback signal on the live book | `MAX_ADDS=2`, SIGNAL_SHADOW rows | 17 rows, 13 resolved |
| Macro Regime Engine M1–M4 (`macro_features` → `macro_fingerprints` → `macro_playbooks` → `macro_regime`) | 38-year lake → DTW archetype match → nightly **immutable** declaration | DECLARED only with `SIM_FLOOR` + `MIN_ANALOGS`; runner-up always shown | declaring nightly; **0 graded** (Stage B waits on calendar time, ~Oct 13) |
| Stage A/B (`strategy_registry`, `strategy_scorer`, `strategy_scoreboard`) | declarative index recipes → embargoed forward grades → Wilson-bounded cells | forward stratum never pooled with in-sample | `slow_burn` carried 0 strategies in 52 nights |
| `analysis/auto_discovery` AD-1..4 | cross-asset lake → shock candidates | circular-shift + phase-randomised surrogates, OOS; **motif gate unbuilt** | manual, Mac; 0 admitted |
| `validation/placebo` | seeded nulls through the same harness | parallel BH batch, compare-to-budget | never seeded |
| `edge_miner` (Mac, Ollama) / `evolution.py` (Mac, Ollama, Sat 02:00 if installed) | causal triples / ONE whitelisted-parameter mutation, double-backtested, human-applied | RevertOnRegression | opportunistic |

### 2.4 Darling tiering (Dept 8, advisory only)

`fundamental_screener` → `data/darlings_queue.json` → `dynamic_pricer.levels_for`
(anchored VWAP at the highest-volume up-day of 60 sessions, widened to a
high-volume node = buy zone; stop = floor − 1.5×ATR; trims = confirmed pivot
highs; Law-3 overextension) → `data/darlings_levels.json`; `valuation_scorer`
(Mac only: P/E 0.40 + PEG 0.30 + P/S 0.30 → 1–100, negative EPS = VETO) →
`darlings_valuation.json`; `darling_tiers.grade_one` (first matching rule wins:
strong_buy · weak_buy · strong_hold · weak_hold · weak_sell · strong_sell ·
watch · ungraded) → `darling_tiers.json`; Saturday `weekly_recalibration`
re-screens filings and **pins** (No-Orphan rule) override the daily clock.

### 2.5 The memory loop

Write: `plan_tracker` resolves → `record_post_mortem` → Gemini `analyst` →
`brain_map.record_resolved_entry` (outcome + events + links, idempotent) →
`evidence.persist_entry_snapshot`. Read at proposal time:
`forecast.query_similar_events` (`memory_context`), `options_proposer._memory_context_for`
(graph_edges), `skeptic_agent` (Random Forest, advisory), `confluence.evidence`
6-layer snapshot with `alignment_line` ("evidence not gate"). LLMs: Gemini on
the VM (news, post-mortems, Discord replies); Ollama on the Mac only (parser,
edge miner, evolution). `text_intelligence` is the one backend seam (#74).

---

## 3. Strategy routing

### 3.1 Live capital path A — index/stock option spreads

```
master_scheduler (09:10, composition root)
 └─ market_loop.run_market_loop  [market hours, 2 h cooldown per underlying, 9-name universe]
     ├─ live_bridge.fetch_live_market_state  → analysis + VIX + vol_bridge overrides
     ├─ market_snapshot.write                → the shared read-model
     ├─ underlying_router.prioritise         → reorder, never filter
     ├─ regime_filters.advise                → smart-money/sector bullish veto · crisis (no short premium)
     └─ options_proposer.build_proposal
          market_view (SMA 50/200 grade + fresh cross + RSI≤30 mean-revert unless strong_bearish)
          → horizon_for → pick_expiry → physical_settlement_gate (stock options ≥ 7 d)
          → chain geometry → the structure if/elif:
               neutral  → iron_condor / iron_butterfly  (VIX ≤ 16 only)
               bullish  → bull_call_spread
               bearish  → bear_put_spread
          → _leg_fill (#70, fill_basis quoted|ltp) → size_lots → MAX_RISK_PER_TRADE_RS ₹10,000
          → adaptive_sizing.adjust_option_lots (shrink/veto only)
```

`src/trade_planner.map_technical_to_strategy` is a pure copy of that matrix
(it can also emit `bear_call_spread`, for which no constructor exists) and is
**not wired** into `build_proposal`. There is no strategy router object:
routing is one `if/elif` chain in `options_proposer.py` and its unwired twin.

### 3.2 Live capital path B — the equity darling book

```
equity_desk.run_darling_live_cycle  [inside the same loop; tiers ≤ 3 d old or NO new entries]
  exits (track_open_shadows: stop / target / 45-day time stop) → strong_sell force-exits → settlements
  → entries: equity_shadow_proposer.propose_darling_entries over strong_buy + in-zone weak_buy
       evaluate_darling_entry (fill_basis live | eod_close; target = first trim pivot ≥ 1R else 2R)
       → equity_entry_checks (never_short · corporate_risk_halt [fail-CLOSED] · liquidity_filter
         [fail-CLOSED, tier1 only] · expiry_week_halt · overextension_halt)
       → adaptive_sizing.equity_verdict → fund_entry (desk ruin halt −10 %, size_entry, budget)
       → portfolio_manager.request_entry("eqd:"+id)   ← the same firm door as options
firm_treasury.run_rotation (19:56): base 30 % + tilts, deadband, step cap → treasury_state + ledger
```

### 3.3 Shadow proving paths (zero capital, by construction)

| Path | What it proves | Ledger | Schedule | Court |
|---|---|---|---|---|
| `strategies/insolvency_short` (#89) | CIRP / default filings → bear put spread; F&O+tier1 gate | `logs/insolvency_short_shadow.jsonl` | none | none — F&O subset has wrong sign, n<10 |
| `strategies/glassbreaking` (#96, 09-11) | falling_knife (RSI ≤ 30 / anchored-VWAP support after ≥ 8 % drop) and early_breakout (gap ≥ 2 % + 2× volume, SMA bypassed) → bull call spread only; +1R/+2R tranches; ATR trail | `logs/glassbreaking_shadow.jsonl` | none (no candidate feed yet) | registered TRIAL hypotheses; `court_scorecard` on `stat_gates.promotable` |
| `equity_shadow_proposer` PAPER_TELEMETRY | block-VWAP pullbacks at `capital_allocated: 0` | `logs/equity_shadow_journal.jsonl` | in the loop | none (telemetry) |
| `validation/h4_shadow` | pyramid add-on signal on the live book | `shadow_trades` SIGNAL_SHADOW | Task J nightly | trial evidence |
| Macro Stage B | archetype recipes, embargoed forward grades | `logs/macro_strategy_scores.jsonl` | 19:50 | Wilson cells |

The rule that binds all five: a shadow may write its own ledger and court
rows; it may never import `journal.log`, `firm_treasury`, `portfolio_manager`
or touch a lock (guard tests grep the source).

---

## 4. Risk & execution layer

### 4.1 Entry gates, in the order a proposal meets them

1. **Exposure gate** `exposure_gate.gate_entry` — one open position per
   underlying+direction (#68). **Fail-open** by rule; blocks are logged to
   `logs/exposure_blocks.jsonl` and recorded as opportunity-cost rows.
2. **Halt list** `portfolio_manager.request_entry` walking `ENTRY_HALT_CHECKS`:
   the **latched risk-of-ruin halt** (`MAX_DRAWDOWN_PCT` 10 % trailing; #92b —
   breaching writes `ruin_halt_latched` to `account_events`, `trading_halted`
   reads the latch not the live number; the only exit is
   `--reset-halt --why … --yes`, which re-arms if still breached), then the
   **daily breaker** (`MAX_DAILY_LOSS_PCT` 3 %), then **margin exhaustion**
   against `available_cash`.
3. **Margin** = `required_margin_for` → `portfolio.calculate_span_margin`
   (offset-aware) × lots × `span_stress_factor(vix)`, locked in `margin_locks`
   keyed by journal ref. Capital moves translate `peak_equity` by the rupees
   moved so drawdown measures trading only (#92a).
4. **Sizing caps**: `OPTIONS_RISK_PER_TRADE_PCT` budget → `MAX_RISK_PER_TRADE_RS`
   hard rupee cap → `adaptive_sizing` (Wilson-bounded shrink or veto, never a
   boost) → equity: `DESK_RUIN_PCT` and the treasury budget.
5. **Regime and contract gates**: `StrategyConstructor.validate_regime` refuses
   range-bound structures above VIX 16 or with VIX unknown;
   `physical_settlement_gate` keeps stock options ≥ 7 days from expiry;
   `regime_filters` veto bullish index spreads on distribution and disable
   short premium in a crisis.
6. **Approval**: `PAPER_AUTO_APPROVE` or a Discord button, both through the
   single mutation seam `options_proposer.decide_pending`; the `human_pulse`
   tripwire disarms auto-approval after 3 unattended trading days.

### 4.2 Exits and settlement (one path: `plan_tracker`)

- Spreads: `_resolve_spread` walks daily closes with a **linear time-value
  model** (intrinsic + entry time value decaying to zero at expiry), clamped
  to the structure's max loss / max profit; exits at 65 % of max profit or 2
  days before expiry (7 for stock options). The **wall-clock expiry backstop**
  (#95, 09-11) settles anything past expiry at intrinsic on the last close ≤
  expiry, or at max loss after a 3-day no-data grace, naming the basis.
- Intraday: `live_bridge.intraday_square_off` re-verifies the 65 % gate on
  **real chain quotes** (all-or-nothing) and settles through
  `resolve_intraday_profit_take` — the only exit priced off quotes rather
  than the model.
- Costs: `portfolio.calculate_trade_frictions` per leg both ends (2026 STT,
  stamp, exchange, GST, SEBI, DP) + `apply_slippage` (VIX multiplier × lot
  depth × liquidity tier: tier1 0.10 %, tier2 0.25 %, illiquid 0.50 %, #91);
  a `quoted` fill skips entry-side slippage (#70).
- Cash: `_settle_spread_cash` → `portfolio_manager.release_entry` →
  `release_margin` → on profit `wealth_lock.sweep_on_settlement` (advisory).
- Equity: `equity_desk.settle_exit` = gross − `delivery_frictions` both sides −
  tier slippage → `release_margin`; `sweep_orphan_locks` re-drives a lock whose
  ledger row already shows an exit.
- Pyramiding (M4A, #96): `plan.tranches` ladders are **observed** on the
  outcome (`pyramid_pnl_rs`, `booked: False`), never booked; the ATR trail on
  spreads lives only in `_resolve_spread_trailed` for the shadow grader.

### 4.3 What "execution" means here

Nothing places an order. `dhan_client` calls data endpoints only; `src/api.py`
refuses a real-money decision with 403; the only Dhan write capability in the
owner's hands is the MCP connector outside this repo. Fills are model marks
(`_leg_fill` at top-of-book at entry, the linear model at exit), positions are
whole and instantaneous, and the broker's own book is never read (the
read-only sync of it is scoped in `docs/dhan_broker_book_sync_plan.md`, #94,
not built).

---

## 5. Telemetry & ops

| Report | When | Reads | Says |
|---|---|---|---|
| `morning_brief` | 08:05 M-F | watchlist, journal, `darlings_daily`, levels | macro sentence, results proximity, buy-zone proximity |
| `portfolio_report` | 2-hourly | journal + snapshot marks | P&L card (dropped, never spooled, under the Discord budget) |
| `eod_summary` | 15:45 | journal, `brain_map` resolutions, `exposure_blocks`, `proposal_ledger` | MTM, open book, net delta, Wilson line (n ≥ 5), drawdown line, blocked count, position ages |
| `ceo_brief` | 16:30 | heartbeats, log sweep (own offset), `deploy_log`, `eod_summary` numbers, miner state | 5 fields: Operations · Issues (`humanize_issue`, DH-902 = Data API plan lapsed since 09-11) · Deployments · Risk & Capital · Pattern Miner |
| `ops_monitor` | 20:30 | `logs/*.log` incremental sweep, `EXPECTED_JOBS` heartbeats, `staleness_guard.scan`, `/proc/meminfo` | health card; **RED alarms (09-11)**: any DH-9xx code, auth/data-access codes above the cap, ≥ 2 consecutive zero-capture sessions, MemAvailable < 100 MB; any alarm forbids the ✅ card; `--verdict` is the stateless on-demand read |
| `bug_ledger` | 20:40 | `problems.jsonl`, silent `account_events` rejections, treasury aborts | `autonomous_bug_report.jsonl` (2 active rows) |
| `validation/digest` | Sat 10:00 | registry, trial, Stage B, opportunity cost | weekly court digest led by placebo FDR (currently on an empty court) |
| `performance` | Sat 10:05 | outcomes | Sharpe/Sortino/DD — posts only at n ≥ 20 |
| `daily_health_and_queue.sh` | manual | Stage-B tracker, `ops_monitor --verdict`, Mac queue | the owner's one command; exit 2 on RED |

All Discord traffic passes `notifier.fire_broadcast` (5 cards/day budget,
`system_crash` always, scheduled cards reserved, overflow spooled to
`discord_digest_queue.jsonl`). Interfaces: `src/api_server.py` (fail-closed
key auth, mounts `src/api.py`, owns the Discord approve/reject action) behind
a Cloudflare **quick** tunnel whose URL rotates on restart; `alpha-discord-bot`
(read-only on the engine); `src/brain_mcp.py` (read-only SQLite MCP).

Infrastructure: one GCP `e2-micro` (964 MB RAM, 1 GB swap since 09-10, disk
80 %) running 30 cron lines and three named systemd services
(`alpha-trading`, `alpha-discord-bot`, `cloudflared-tunnel`; a fourth is
counted in HANDOVER but not named anywhere in-repo — unverified), plus one
8 GB Mac laptop for NSE-IP jobs, Ollama and artifact production.

---

## 6. The numbers that describe the system today (VM ledger, 2026-09-11)

| Measure | Value |
|---|---|
| Resolved approved option spreads, lifetime | 30: 24 bear_put_spread (bearish), 3 iron_condor, 3 bull_call_spread |
| Underlyings traded | NIFTY 50 ×16, NIFTY BANK ×10, NIFTY FIN SERVICE ×3, ICICIBANK ×1 |
| Open locks | 13 (9 spreads, 4 equity), none past expiry |
| Equity desk | 85 entries, 70 resolved, 9 ever funded; 61 zero-capital telemetry |
| `candidate_patterns` | 9, all CANDIDATE; 0 TRIAL, 0 VALIDATED, 0 DEAD |
| `shadow_trades` | 103; 0 organic pattern fires |
| `placebo_ledger` | 0 |
| Macro declarations graded | 0 (Stage B target ~2026-10-13, ~9 calls) |
| Critic's readiness verdict (09-09) | not ready for real capital: per-trade Sharpe 0.23 on 29 trades |

---

## 7. Gap analysis — the weak links

Three structural gaps, ranked by how much of the system's stated purpose they
block. Each names the evidence, what it costs, and the smallest honest fix.

### GAP 1 — The proving court has never heard a case, so nothing can ever earn authority

> **Status 2026-09-15 — the fix below is BUILT and on cron (decision #97,
> `src/validation/run_proving_court.py`, 21:00 IST).** First sitting tonight.
> Coverage caveat: spreads are priced only where a 15:40 chain is archived
> (five equity-option names); tier1 names without one are counted as
> signals, not fires.

**Evidence.** Department 5 is the system's constitutional centre: only it may
grant a pattern the right to size or veto (#63). On the VM's own database it
holds 9 candidates that have sat in `CANDIDATE` since the first miner pass on
08-18, zero `TRIAL`, zero organic `shadow_trades` fires (all 103 rows are
risk-gate blocks or the H4 signal), a `placebo_ledger` with **zero** seeded
nulls (so the harness's false-discovery rate has never been measured), Stage
B with zero graded declarations after 52 nights, AD-3 enrolment unbuilt, and
now two fresh M4A hypotheses registered as TRIAL with **no candidate feed and
no schedule** to generate a single fire. The Saturday digest reports on this
empty court every week and reads as "quiet".

**Why it matters.** Every upstream department exists to feed this one. The
miners, the macro clock, the shadow strategies and the evidence snapshots are
all producing hypotheses that nothing grades, which means the desk cannot
distinguish its one real edge (30 trades, 24 of them the same bear put on two
indices in one regime) from luck, and cannot promote anything new without a
hand-wired decision — exactly what #63 forbids.

**Root cause, structurally.** The court's inputs are all *optional side
effects* of other jobs: `shadow_runner` fires only when a real proposal's tags
match a registered pattern (none do yet), Stage B is gated on calendar time,
placebo seeding was never scheduled, and every shadow strategy was shipped
"on no cron" as the safe default. There is no job whose only purpose is
"put cases in front of the court".

**Smallest fix.** One nightly Dept-5 job (a sleep-phase task, after Task G)
that: seeds the placebo batch once; walks every `CANDIDATE` older than N days
into `TRIAL`; runs `glassbreaking.run` + `grade` and `insolvency_short.run`
over a bhavcopy-derived candidate list with chain reads through
`dhan_guard`; and prints `court_scorecard` per hypothesis onto the CEO card.
Freeze-compatible (shadow only), one Discord slot, and the first week yields
the first real n.

### GAP 2 — One token, one box, one laptop, one polled model mark: the desk is blind more often than it knows

**Evidence.** The 2026-09-07 → 09-10 outage: the Data API plan lapsed, every
cron "ran", every log was touched, the daily health command said
`all_ok=True`, 13 positions went unmarked and three expired spreads sat open
with ₹73,845 locked until bars returned. The e2-micro hung for five market
hours on 09-09 with no warning. The Mac sleeps through its sync window
(4 of 11 weekdays in August) while a **live veto** reads a Mac-shipped file.
The RED alarms and the expiry backstop shipped on 09-11 close the *detection*
half; the *structure* is unchanged: one Dhan token per client id (#48), one
paid data plan renewed by hand (next: 2026-10-10, diarised only in
HANDOVER), one 1 GB VM for 30 jobs plus three services, one laptop with a
non-datacentre IP that NSE crawls depend on, a 15-minute poll instead of a
push feed, and spread marks from a linear time-value model rather than the
chain (only the intraday square-off looks at real quotes).

**Why it matters.** Institutional standard is that a data outage degrades to
a *known* stale state, never to a silent green. Here the honest-abstention
culture is excellent inside each module, but the *aggregate* signal ("is the
desk seeing prices right now, and are the marks real?") had no owner until
three days ago and still has no redundancy behind it. Marks that come from a
model also mean the drawdown that drives the ruin latch is itself a model.

**Smallest fix, in order.** (a) Resize the VM (e2-small/medium, 20 GB disk) —
money, not code. (b) Replace the laptop with an always-on home node for the
NSE-IP and Ollama roles (the Architect's hardware list of 09-12). (c) Add a
`data_plan_expiry` row to `staleness_guard` with a 7-day amber so the 10-10
renewal is a card, not a diary entry. (d) Put the **mark basis** on every
P&L card (`model` / `quoted` / `snapshot`) and adopt the chain-quoted mark
for open spreads at 15:45, so the number the halt latches on is a market
number.

### GAP 3 — Execution realism and the memory schema stop at "one whole position, one number"

**Evidence.** Fills are instantaneous and whole: `_leg_fill` prints the
top-of-book at entry, exits come from the model, there is no partial fill, no
order state, no rejection, and no reconciliation against any broker book
(PROP_ROADMAP M2's six-row gap table, unchanged). The memory layer mirrors
that: `outcomes.journal_ref` is UNIQUE with one `r_multiple`, `margin_locks`
holds one lock per ref, `exposure_gate` counts one position per
underlying+direction. The M4A work of 09-11 had to be built as *observation*
(`pyramid_pnl_rs` beside `pnl_rs`, `booked: False`) precisely because a
tranche has nowhere to live. Strategy routing is a hard-coded `if/elif`
duplicated in an unwired planner matrix, so a new structure or primitive
needs edits in two places and a fourth for the exposure gate's direction map.
The record itself carries a known ₹26,982.14 reconciliation gap (Issue 24)
that every lifetime figure must hand-correct.

**Why it matters.** The critic's 09-09 verdict ("not ready for real capital")
is as much about these seams as about the sample size: a system whose fills,
legs and adds cannot be represented cannot be back-tested against its own
live record, and cannot be handed a real order path without rewriting its
memory. Every institutional desk models the order lifecycle before it models
the alpha.

**Smallest fix.** A `trade_legs` table (parent ref, leg/tranche id, qty,
fill basis, fill price, order state) additive to `brain_map.db`, written by
`plan_tracker` alongside `outcomes`; `margin_locks` gains a `parent_ref`; the
exposure gate keys on parent. Then one `strategy_router` module that owns the
matrix `options_proposer` and `trade_planner` currently duplicate, with the
direction map inside it. Both are freeze-compatible migrations (additive
schema, no behaviour change) and are the precondition for M2, M4A booking,
and the read-only broker-book sync (#94) all at once.

### Smaller drift found while mapping (record, not roadmap)

- `CRON_SETUP.md` says the treasury rotates at 19:50; the installer says
  19:56. Docs quote "31/31 cron lines"; the installer has 30 plus `CRON_TZ`.
  HANDOVER counts "4 services"; three are named in-repo.
- `SYSTEM_XRAY.md` §9 fixes 1–4 (Wilson line, drawdown line, blocked count,
  position age) are implemented (`eod_summary`, 08-17) but the audit text
  still lists them as open; fixes 5 and 9–10 (performance abstain countdown,
  weekly Data Health card, bug-ledger top-3) are genuinely open.
- `data/rss_signals.jsonl` (183 KB) and `logs/resonance_advisories.jsonl`
  are write-only orphans of disabled paths; `data/lake/intraday_15m.jsonl` is
  the one unbounded flat file in the lake (7.7 MB log, 2.8 MB tape).
- `journal._PLAN_KEYS` silently dropped `plan.trailing` until 09-11, so the
  08-05 equity ATR trail had never been able to fire on a real row.

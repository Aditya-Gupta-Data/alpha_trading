# MODULES.md — Master Component Index

One-line-plus purpose for every file that matters. If a file isn't listed
here, it's either generated (`__pycache__/`), static assets, or trivial. For
system flow between these, see `ARCHITECTURE.md`.

**Maintenance rule (2026-07-10):** any commit that adds, moves, or
repurposes a module updates this file IN THE SAME COMMIT. This index is
the first stop for every code-review/search agent — point them here
instead of letting them grep the tree from scratch; a stale index is what
makes reviews expensive.

## Data layer (market data)

| File | Purpose |
|---|---|
| `src/dhan_client.py` | THE market-data source. DhanHQ SDK wrapper: `SECURITY_ID_MAP` (verified against Dhan's scrip master), `get_daily_ohlc`, `get_ohlc_since`, `get_live_price`, `get_quote`, `get_daily_closes`, `get_option_chain`/`get_expiry_list`. Data-only — no order methods. |
| `src/dhan_guard.py` | `SafeDhanClient` — hardened, audited access to the same market-data endpoints: classified DH-9xx failures on `last_error` + rolling `audit` (auth vs data outages distinguishable), single rate-limit retry, `strict=True` raise mode (tests only). Freshness guard voids >60s-old INDEX quotes mid-session only (equities idle legitimately; >3h "age" = miszoned stamp, ignored). Re-exports `dhan_client.unwrap_payload`. Same return shapes/empty states as `dhan_client`. |
| `src/token_provider.py` | The self-healing token seam (Issue 5 fix): `get_token()` re-reads `.env` on mtime change so long-running processes pick up an external renewal WITHOUT restart; `set_override()` for tests/push. `dhan_client._get_client` consumes it — the ONLY Dhan SDK construction site. |
| `src/market_snapshot.py` | Phase 8: the engine's published market read-model at `data/market_snapshot.json` — `live_cycle(publish_snapshot=True)` writes spots + every open position's mark atomically each cycle; viewers (`portfolio_report.get_live_marks`, the dashboard) READ it instead of re-fetching quotes, keeping the live loop the single Dhan consumer (decision #48). `read(max_age_seconds=...)` treats stale/corrupt/missing as None; write failures never touch the loop. |
| `src/data_fetcher.py` | Thin re-export of `dhan_client.get_quote` — kept for the original `get_quote(ticker)` contract older callers use. |
| `src/live_bridge.py` | Phase 6H live market-hour adapter: `fetch_live_market_state` (drop-in `fetch_fn=` for `run_market_loop` — live spot appended to the daily-close trend read), `parse_packet`/`CandleAggregator` (quote snapshots → 15-min OHLC), `evaluate_open_positions`/`live_cycle` (real-time advisory exit signals on open spreads via `plan_tracker`'s pure helpers, de-duped Discord alerts). READ-ONLY on all trade state (decision #41). Daemon: `python3 -m src.live_bridge`. |
| `src/indicators.py` | Pure-Python SMA and Wilder's RSI, no dependencies. |

## Knowledge graph

| File | Purpose |
|---|---|
| `src/graph_engine.py` | Phase 6C reasoning layer: `ensure_schema` (creates `graph_edges` table + temporal columns), `add_edge` (idempotent writer — stamps `valid_from`, clears `invalid_at` on reinforce), `GraphEngine` (loads ACTIVE edges only — `WHERE invalid_at IS NULL` — into a `networkx.DiGraph` for 2-hop BFS context queries). |
| `src/regime.py` | Regime-Aware Memory vocabulary (roadmap #4): `vix_band` (low <13 / mid 13-16 / high >16 — the single source; evolution + planner boundaries align), `regime_for(view, vix)` attached to journal entries at creation, `encode_for_model` for the skeptic's v2 feature contract, and the as-of-date historical backfill (`python3 -m src.regime backfill --db …`, bars-cache-driven, idempotent, never guesses — untaggable rows get 'unknown'). Stored as additive nullable `regime_trend`/`regime_vix` columns on `outcomes` + `simulated_trades`; queried via `query_similar_events(tags, regime=…)`'s `in_regime` block. |
| `src/brain_map.py` | Phase 6 relational event-pattern memory: sqlite `events`/`outcomes`/`event_outcome_link` at `data/brain_map.db` (+ additive regime/post_mortem columns), `record_event`/`record_outcome`/`link_event_outcome`, `query_similar_events(tags, regime=…)`, `ingest_existing` backfill CLI, `_normalize_tag` (THE tag normalizer everything clusters on). |
| `src/local_parser.py` | Phase 10B local-Ollama text→JSON extractors: `LocalExtractor` (event frames, `chat_json`, causal triples) — ONLY network I/O is localhost Ollama, zero market-data imports (decision #30), every failure returns None quietly (offline logged once per process). |
| `src/sleep_phase.py` | Off-hours consolidation batch (cron 20:00 IST): decay sweep, episodic replay/causal-link writing via local Ollama (VM skips LLM tasks silently — it runs no Ollama by design), evolution Task E hook. |
| `src/ingestion/macro_tracker.py` | Phase 7: Crude / Gold-India / Gold-World / USDINR → SHORT(5-bar)/MEDIUM(21)/LONG(126) directional matrix. Dhan live path uses VERIFIED ids from `config/macro_securities.json` only (never guessed), fails open per-metric/per-horizon to hand-editable `data/macro_snapshot.json`, else "unknown". `INDEX_IMPACT_WEIGHTS` maps each permutation onto NIFTY 50 / NIFTY BANK bias; gold India-vs-World divergence flags currency-driven moves. |
| `src/ingestion/news_parser.py` | Phase 7: headline/social text → strict 5-key signal frame {target_entity, event_classification, directional_bias, horizon_impact, confidence_score} via `LocalExtractor`; `canonicalize_entity` is the shared entity vocabulary (Brent→CRUDE, NIFTY→"NIFTY 50", strips .NS). |
| `src/ingestion/deals_tracker.py` | Phase 8 (decision #60): EOD NSE bulk & block deals → per-ticker smart-money footprint {net_qty, net_value_rs, buy_deals, sell_deals, block_deal, marquee_names, marquee_net}. Live NSE path (cookie handshake) fails open to hand-editable `data/bulk_deals_snapshot.json`, else "none"; net-direction not raw count; marquee tagging from `config/deals_watchlist.json`. Writes `data/bulk_deals.json` (daily aggregate) AND appends `data/deals_history.jsonl` (raw per-deal ledger, idempotent per day, via `append_raw_deals`/`read_deal_history`) — the substrate the entity-affinity layer learns from; plus a per-day census row (tape-quality telemetry, near-dup alias candidates for human review) and the raw NSE payload hashed into the lake. `--backfill YYYY-MM-DD` crawls NSE's historical archives (60-day windows, throttled, raw windows archived, per-row report-date parsing across eras, idempotent per day) — run from the Mac. `load_deals()` reads the aggregate back for consumers. Advisory-only, not yet wired into forecasts. |
| `src/lake.py` | Phase 0 (holy-grail plan): the cold store beside brain_map.db — date-partitioned gz-JSONL under `data/lake/<dataset>/date=YYYY-MM-DD/`, atomic writes, gzip-member appends (intraday taps), `scan`/`read_day` readers, `archive_blob` (sha256; upstream revisions become loud rev- files). ONLY ingestion writes; greppable end-to-end (zcat); no Parquet/DuckDB/new datastore. HOT-state rules (#19/#25) unchanged. |
| `src/ingestion/chain_archiver.py` | Phase 0: post-close (15:40 IST Mon-Fri) capture of the nearest 4 NIFTY/BANKNIFTY expiries (full `oc` + spot + VIX) → `data/lake/chains/` — historical chains are unbuyable (#36); throttled, fail-open per expiry, weekend no-op, heartbeat-monitored. |
| `src/ingestion/daily_archiver.py` | Phase 0: daily 19:45 IST snapshot of the perishable artifacts (news_sentiment.json, the macro matrix) into the lake (`news_daily/`, `macro_daily/`) — the load-bearing prerequisite for every cross-layer join; independent + fail-open per artifact. |
| `src/ingestion/flows_tracker.py` | Phase 1: NSE FII/DII daily cash provisional figures → `data/fii_dii_flows.json` + lake `flows/` + raw archive. Loose category matching (FII/FPI spellings drift), derives net when absent, never guesses zeros. Live → snapshot → "none". |
| `src/ingestion/earnings_calendar.py` | Phase 1: NSE results-calendar → `data/earnings_calendar.json` ({TICKER: next results date}, earliest-upcoming wins) + lake history; whole-calendar overwrite per run so postponements self-heal. `days_to_results(ticker)` is the deterministic, entry-filter-orthogonal feature (#50 lesson) consumers stamp at proposal time. |
| `src/confluence/evidence.py` | Phase 2 (holy-grail plan §5.1): the Evidence Snapshot substrate — one canonical per-layer record {layer, direction, strength, stance, detail, abstained} with adapters for technical/news/macro/affinity/flows/VIX + days_to_results; `build_evidence_snapshot` captures what every layer said at proposal time (explicit abstention, never a guessed neutral — #50's NULL-honesty), `summarize` renders the card lines, `persist_snapshot` stores to the additive `evidence_snapshots` table keyed by journal_ref (first capture wins). Capture-only: nothing scores or gates. WIRED (P2-1b): `capture_for_entry` stamps every headless proposal in options_proposer.run_headless (fail-open — a stamp failure never blocks a proposal); `persist_entry_snapshot` joins the snapshot to its outcome at plan_tracker.record_post_mortem, keyed by journal_ref. |
| `src/graph_viz.py` | Knowledge-graph visualizer: `python3 -m src.graph_viz` reads brain_map.db (graph_edges + entity_affinity) and writes a fully self-contained interactive HTML (data/graph_viz.html) — Canvas force layout, provenance-colored edges (steel=outcome-derived causal, gold=smart-money affinity, red core=loss-permanent λ=0, ghosted dashes=expired), node size=degree, drag/zoom/search/inspect, dark+light themes, zero network/CDN. Read-only viewer. |
| `src/knowledge_graph/entity_affinity.py` | Phase 8 (decision #61): accumulates `data/deals_history.jsonl` into an entity↔promoter-group affinity graph — `canonicalize_client`, `load_entity_groups`, `accumulate_entity_affinity` (folds per-day, idempotent; projects a decaying `concentrates_in` edge into `graph_edges` for linked pairs only), `build_affinity_readmodel` → `data/entity_affinity.json`, `evaluate_distribution_signals` → DISTRIBUTION/ACCUMULATION advisories in `logs/affinity_advisories.jsonl`. All-time concentration + recent-window net direction. No-LLM, VM-safe; runs as Sleep-Phase Task F. Advisory only — no trades, not wired into scoring. |
| `src/knowledge_graph/resonance.py` | Phase 7: `evaluate_portfolio_resonance(parsed_event, macro_matrix)` cross-refs incoming flow against open journal positions → CONFLICT (exit/cut-loss advisory + expiry-roll when the LONG thesis survives) / RESONANCE (extend-target + concrete strike-roll from the position's own legs) / NEUTRAL, horizons blended by days-to-expiry. Journal via pure file stream; brain_map strictly `mode=ro` (one conn per sweep, memoized per strategy); advisory-only, writes nothing except `log_advisories` → `logs/resonance_advisories.jsonl`. |
| `src/decay_engine.py` | Phase 6E temporal decay sweep: `migrate_schema` (adds `valid_from`/`invalid_at`/`decay_lambda` to `graph_edges`), `apply_decay_sweep` (daily `w(t) = w₀·exp(−λ·t)` pass — reduces `confidence_score`, stamps `invalid_at` when weight < 0.1). Run via `python3 -m src.decay_engine` or after the Sleep Phase cron. |
| `src/vol_bridge.py` | Phase 6F quantitative execution bridge: `classify_regime(edges)` classifies "Expansion"/"Contraction"/"Neutral" from aggregate signed edge weights; `compute_regime_overrides(conn, mode)` queries the live graph and returns `build_proposal` kwargs — under Expansion either `risk_pct` (×0.70, scale_risk mode) or `short_strike_otm_pct` (×1.50, widen_wings mode). Stateless, no writes, fail-safe. Wired into `market_loop.fetch_market_state` + `options_proposer.run_headless`. |

## Signal & suggestion engine

| File | Purpose |
|---|---|
| `src/rules.py` | Alert rule evaluation: `price_above`, `price_below`, `percent_up`, `percent_down`. |
| `src/suggestions.py` | Trend (50/200 SMA) + momentum (14-day RSI) read per stock; `analyze()`/`describe()`. |
| `src/suggest.py` | Entry point: emails a daily suggestions digest (`python -m src.suggest`). |
| `src/news_processor.py` | Isolated: Google News RSS -> Gemini -> `data/news_sentiment.json` (score -5..+5 + driver). Imports NO core trading code (isolation principle). |
| `src/forecast.py` | Combines technicals + news sentiment into a transparent, rule-based checklist: bias/confidence/drivers/time-horizon. Reads `data/brain_weights.json` for learned per-archetype adjustments. |

## Trading & risk

| File | Purpose |
|---|---|
| `src/strategy.py` | Turns signals into full trade PLANS: entry rule, stop-loss, target, risk:reward, rationale, risk-based position sizing. `propose_plans()` / `propose()`. |
| `src/portfolio.py` | Paper portfolio math: `data/portfolio.json`, buy/sell, cash + 25%-per-stock rails. |
| `src/portfolio_manager.py` | Phase 6G capital & margin allocation layer: Rs.10,00,000 simulated account pool in `brain_map.db` (additive tables `account_state`/`margin_locks`/`equity_curve`/`account_events`). `request_entry` locks SPAN margin per entry (silent reject on margin exhaustion), `release_margin` settles realized P&L + ratchets peak equity, hard 10% trailing-drawdown halt (`MAX_DRAWDOWN_PCT`). Gates `run_headless` only when trading the real paper book (injected books bypass — decision #40); fail-open seams `gate_headless_entry`/`release_entry`. Inspect: `python3 -m src.portfolio_manager`. |
| `src/trade_planner.py` | Phase 6I technical-to-options planner: `map_technical_to_strategy(technical_state)` — pure evaluation matrix (trend × IV regime → iron_condor / bull_call_spread / bear_call_spread / bear_put_spread / no_trade) with structural leg specs (side, CE/PE, strike + offset from ATM, Bank Nifty grid). Support/resistance override the 2%-OTM shorts; never contradicts the VIX-16 regime gate (decision #42). Phase 6J: every tradeable plan carries theoretical economics (`estimate_plan_economics` — leg premiums from the simulator's synthetic-chain model, net credit/debit, max profit/loss per lot — never Rs.0 placeholders). Zero side effects. |
| `src/market_loop.py` | The entry-side daemon body (composed by master_scheduler): `fetch_market_state` (trend read + India VIX + vol-bridge overrides) → `options_proposer.run_headless` per index during 09:15–15:30 IST; per-index 2h `CooldownRegistry` (`seed_from_journal` rebuilds it across restarts from `created_at`); `is_market_open`/`ist_now` = the canonical IST clock everyone imports. |
| `src/options_proposer.py` | Phase 5 spread proposer: trend read → `StrategyConstructor` legs → margin gate (`pm.gate_headless_entry`, skipped for injected sandbox books) → journal PENDING_APPROVAL + Discord alert. `decide_pending(short_id, approve, why)` is THE decision seam (CLI --review-pending, API bridge, Discord buttons, auto-approve all converge here). `PAPER_AUTO_APPROVE` env switch (default OFF, decision #53; NEVER applies to injected books) auto-approves headless proposals through the same path. |
| `src/analyst.py` | Gemini post-mortem on every tracker-resolved trade (plan vs actual → variance/unexpected/guardrails JSON) stored in `outcomes.post_mortem` — fail-safe, resolution never blocks on the LLM. |
| `src/wealth_lock.py` | Paper-scope Wealth-Locking Flywheel: 50% GOLDBEES sweep LEDGER (advisory rows + Discord card, never a cash movement) on profitable settlements, hooked from `portfolio_manager.release_entry` (whose released=True answer is committed BEFORE the sweep and never flipped by a sweep failure). |
| `src/positions.py` | Read-only open-positions view (terminal, gateway `GET /api/discord/positions`, bot `/positions`): predicates IMPORTED from `plan_tracker._trackable`/`_spread_trackable` — single source, the view and the tracker cannot disagree. `src/view_positions.py` is its ASCII-table CLI (`python3 -m src.view_positions`). |
| `src/portfolio_report.py` | 2-hourly read-only Discord report card (cron `0 */2`, market-hours self-gate) AND `get_live_marks()` — THE shared mark ladder every consumer uses (dashboard + card): engine snapshot first (zero Dhan calls), direct SafeDhanClient fetch only for uncovered positions (equities; stale snapshot). `read_exposure` = mode=ro SELECTs on the capital tables. `_spread_detail` = the one wording for spread marks. |
| `src/calibration/mfe_mae_analyzer.py` | MFE/MAE expectancy surface (spec §3.1/§3.2): journal + simulated_trades read-only (mode=ro), one bar-fetch per ticker via SafeDhanClient, winner-based Apex TP/SL suggestion with a 20-trade abstention floor. Advisory only. |
| `src/journal.py` | Appends every approved/rejected decision to `data/journal.jsonl` with signal, risk levers, pattern tags, plan block, outcome. |
| `src/plan_tracker.py` | Resolves OPEN plan-carrying trades against real daily OHLC high/low (stop/target/time-stop) — NOT a naive last-price check. Closes paper positions on resolution (bracket-order semantics). |
| `src/review.py` | Legacy 7-day price-drift scorecard for pre-plan (non-4B) journal entries. |
| `src/tuner.py` | Learning loop: scores resolved BUY archetypes (fresh-cross vs RSI-oversold), writes `data/brain_weights.json`, which `forecast.py` consumes. |
| `src/evolution.py` | Procedural Evolution (`python3 -m src.evolution`, Mac-side, local Ollama only): mines loss clusters from `simulated_trades` (underlying × strategy × VIX band, provenance = journal_refs), deterministic HER-style hindsight buckets, counterfactual win-contrast, then an Analyst→Critic→resolution dialectic (all replies strict-JSON-gated) proposing ONE mutation from the whitelisted `EVOLVABLE_PARAMETERS` registry (VIX gate, risk %, OTM %, profit-take, pre-expiry days — bounds-checked; the LLM never writes code). Survivors are double-backtested via the Phase 7 simulator (baseline vs `override_parameters`, in-memory DBs, cached bars) — RevertOnRegression discards cluster-fixes that degrade global Sharpe/drawdown. Promoted candidates → `candidates/evolution_<ts>.md` (4-section format + deterministic unified diff) + `data/evolution_lineage.json` version tree (v1→v2 per parameter, failed attempts remembered). NOTHING auto-applies — human gatekeeping only. `--refresh-bars-cache` pulls bars/VIX through the VM (Mac holds no token, decision #48). Runs as sleep-phase Task E where Ollama exists; silent skip elsewhere. |
| `src/trade.py` | Interactive terminal paper-trading session (`python -m src.trade`) — the original human-in-the-loop flow. |
| `src/simulator.py` | Phase 7 time-travel simulator: replays history through the REAL proposal+resolution pipeline via as-of-date injection (decision #36); results land idempotently in `simulated_trades` + outcomes/events/links. CLI fetches daily bars AND India VIX history (`_fetch_vix_series` — so the VIX gate and stored rows see true readings; `--no-vix` to skip). Run: `python3 -m src.simulator --start … --end …`. |
| `src/skeptic_agent.py` | Phase 11 Random Forest Skeptic: `RandomForestAuditor` builds the frozen `FEATURE_NAMES` vector (graph context + market numbers) and, once `data/skeptic_model.pkl` exists, emits P(win) with a ⚠️ warning below 0.40. ABSTAINS (None, no warning) without a trained model. Advisory only, fail-safe, no market-data imports. |
| `src/train_skeptic.py` | Phase 7b trainer: fits the skeptic's forest on `simulated_trades` (frozen feature order, scratch dropped, class-balanced), evaluates on a stratified holdout, and persists `data/skeptic_model.pkl` + meta sidecar ONLY above the `MIN_BALANCED_ACCURACY` 0.60 ship gate (decision #44 — a noise model must never ship; `--force` for experiments). Run: `python3 -m src.train_skeptic [--dry-run]`. |

## Alerting & notifications

| File | Purpose |
|---|---|
| `src/main.py` | Alert entry point (`python -m src.main`) — watchlist -> rule check -> email digest. (NOT the trading-session entry point — that's `src/master_scheduler.py`.) |
| `src/master_scheduler.py` | Phase 7A master scheduler (`python3 -m src.master_scheduler`): one command runs a full automated paper-trading day — waits for the 09:15 IST open, supervises the entry loop (market_loop + live adapter, margin-gated PENDING_APPROVAL proposals) and exit loop (live_bridge advisory alerts) as asyncio tasks, self-terminates at 15:30, graceful SIGINT/SIGTERM shutdown, Discord session bookends (account snapshot + planner playbook). Cron instructions: `CRON_SETUP.md`. |
| `src/notifier.py` | Gmail SMTP (`send_digest`), async Discord text push (`send_discord_message`), structured embed broadcaster (`broadcast_alert` async / `fire_broadcast` sync bridge — colour-coded embeds for trade lifecycle events). Phase 6J test-env muzzle: `webhooks_muzzled()` blocks ALL webhook HTTP when `IS_TEST_ENV` is truthy or a pytest run is detected — muzzled sends are logged locally and report False. |
| `src/ops_monitor.py` | Nightly ops sweep (`python3 -m src.ops_monitor`, cron 20:30 IST): incrementally scans `logs/*.log` for problem-shaped lines (byte-offset state — each problem reported once), appends every finding to `logs/problems.jsonl`, checks each scheduled job's log was touched today (weekday-aware heartbeats, includes `push_token_to_vm.log`), posts a terse Discord health card. |
| `src/renew_token.py` | Daily DhanHQ token minting (`python3 -m src.renew_token`, cron 07:00 IST on BOTH machines): V2 PIN+TOTP flow, credentials from `.env` when present (the Mac) or fetched at runtime from **GCP Secret Manager** via metadata-server OAuth (the VM — pure stdlib, keys never on disk, decision #47); legacy `/v2/RenewToken` kept only as a loud deprecated fallback. Rewrites ONLY the token line (`.env.bak` kept). |
| `scripts/push_token_to_vm.sh` | Pushes ONLY the freshly-renewed `DHAN_ACCESS_TOKEN` to the VM. **NOT on any automatic schedule** (decision #48: DhanHQ allows one active token per account, so an unattended Mac renewal can invalidate and overwrite the VM's currently-valid token — the "redundancy" was actually a race that could break the live engine). Kept as a manual/dev tool for troubleshooting only. Token crosses via a chmod-600 temp file over `gcloud compute scp` (never embedded in a command line); the VM updates its `.env` via `renew_token.replace_token` and restarts `alpha-trading.service`. `--dry-run` to preview. |
| `src/edge_miner.py` | Opportunistic Mac-side causal mining (decision #47, LaunchAgent at login + 21:00 via `scripts/mine_edges.sh`): guards (Ollama up, gcloud present, >20h since last success) → pulls the VM's `brain_map.db` → runs `sleep_phase.write_causal_links` with local Ollama → replays only the NEW triples through idempotent `graph_engine.add_edge` on the VM (never file overwrite) → refreshes the Mac's read-only `data/` copies for chat_agent (originals archived once at `data/mac-archive-pre-vm/`). `--force` skips the 20h gate. No paid LLM API involved, ever. |
| `src/eod_summary.py` | Daily EOD broadcaster (`python3 -m src.eod_summary`, 15:30 IST): queries journal + brain_map.db for today's MTM P&L, active positions count, and net delta exposure; posts a terse embed card via `broadcast_alert`. |
| `src/config.py` | Loads + validates `config.json` at import time (fails loudly on missing/bad keys) — RSI thresholds, SMA windows, risk levers, tuner params. |

## Interfaces (front doors)

| File | Purpose |
|---|---|
| `src/api.py` | Unified local FastAPI backend: all `/api/*` routes (see `ARCHITECTURE.md`), the hourly auto-sync loop, the Phase 6 web dashboard routes (`GET /dashboard`, `/api/web/positions` via the shared mark ladder, `/api/web/events` SSE change-watcher), `ApiKeyMiddleware` (key-optional local mode; accepts X-API-Key / Bearer / `?api_key=` — the query form exists because EventSource can't send headers), env-gated `EXTRA_CORS_ORIGINS`. |
| `src/api_server.py` | Phase 9 strict PUBLIC gateway (the one process the tunnel exposes): mounts the full `src.api` app behind fail-closed auth (503 when API_KEY unset, 401 otherwise; only `/api/health` public) + the two-way Discord bridge `POST /api/discord/action` → `decide_pending`. |
| `src/discord_client.py` | Outgoing async webhook push (lightweight, no gateway connection) — separate from the interactive `discord_bot.py`. |
| `src/web/static/dashboard.html` | Single-file dark-terminal positions dashboard (Phase 6): SSE-driven, deliberately NO polling timers; reads `?api_key=` off its own URL and propagates it to fetch + EventSource so it works behind the gateway. |
| `src/discord_bot.py` | Discord analyst bot: `/analyze` slash command (forecast), chat replies via Gemini. Read-only on the engine (imports only `forecast.py`). |
| `src/chat_agent.py` | ADiTrader reasoning mirror: local Discord bot (@mention-gated, single authorized user) routing stateless queries to local Ollama with a terse DB context (`fetch_agent_context`). `@ADiTrader portfolio` bypasses the LLM entirely — `build_portfolio_snapshot` formats the Phase 6G account (starting capital, free cash, locked margin, active trades, net P&L) as hard numbers (Phase 6J). Read-only, no execution pathways. |
| `src/web/watchlist_store.py` | Reads/writes `config/watchlist.yaml` (ruamel round-trip, preserves comments); ticker validation goes through `dhan_client`. |
| `src/web/static/index.html` | Legacy static dashboard HTML, served by `src/api.py`'s `GET /`. Superseded by `lovable-frontend/` for active development. |
| `lovable-frontend/` | React (TanStack Start + Vite) dashboard. **Gitignored on `main`** — lives only on the `lovable-ui` branch. See `DATA_CONTRACT.md`. |

## Config & data files (not code)

| File | Purpose |
|---|---|
| `config.json` | Non-secret tunables: RSI/SMA thresholds, risk levers, tuner params. Required keys enforced by `src/config.py`. |
| `config/watchlist.yaml` | The watchlist: tickers, type (stock/index), optional alert rules. |
| `.env` / `.env.example` | Secrets (git-ignored) / safe placeholder template (versioned). Keys: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `GEMINI_API_KEY`, `DISCORD_BOT_TOKEN`, `ALERT_EMAIL_*`. |
| `data/portfolio.json` | Paper portfolio (cash + holdings). Git-ignored, personal, live. |
| `data/journal.jsonl` | Every trade decision ever logged (one JSON object per line). Git-ignored, personal, live. |
| `data/news_sentiment.json` | Latest Gemini-scored news sentiment per ticker. Git-ignored (regenerable). |
| `data/brain_weights.json` | Tuner's learned per-archetype weights. Git-ignored (regenerable). |

## Tests

| File | Purpose |
|---|---|
| `tests/test_notifier.py` | `broadcast_alert` embed dispatch, `fire_broadcast` sync bridge, EOD card builder, `query_todays_resolutions`, `compute_net_delta_exposure` — 53 tests, offline, pytest-mock. |
| `tests/test_vol_bridge.py` | Phase 6F vol bridge: polarity classification, net-signal arithmetic, regime boundary precision, macro shock scenarios, scale_risk/widen_wings parameter scaling, run_headless integration — 31 tests, offline. |
| `tests/test_live_bridge.py` | Phase 6H live bridge: packet parsing, candle-bucket playback, live fetch_market_state contract (hours gate, dead quote, vol_overrides), intraday profit-take/pre-expiry/clamp arithmetic, alert de-dup, read-only sandbox spy — 19 tests, offline. |
| `tests/test_trade_planner.py` | Phase 6I planner: routing matrix, trend/IV classifier boundaries, Bank Nifty strike snapping, support/resistance overrides, VIX-gate consistency, purity + import guard — 21 tests, offline. |
| `tests/test_train_skeptic.py` | Phase 7b trainer: frozen feature-order contract, scratch dropping, thin-data refusals, separable-pattern learning, ship-gate refusal of coin-flip models + `--force` override, trained pickle waking the skeptic from abstain — 12 tests, offline (temp-dir model files only). |
| `tests/test_ops_monitor.py` | Ops sweep: problem-pattern matching, incremental offsets, rotation recovery, dedupe counts, self-log exclusion, weekday-aware heartbeats, jsonl ledger, card formatting, broken-notifier safety — 10 tests, offline. |
| `tests/test_master_scheduler.py` | Phase 7A scheduler: session-window math, past-close misfire exit, loops armed + self-close at 15:30, stop-event (signal) shutdown, pre-open wait cancellation, dying-loop safety, planner playbook lines — 8 tests, offline (hand-wound IST clock). |
| `tests/test_regime.py` | Regime memory: band boundaries, capture at entry creation (live + simulator), schema migrations, NULL-honesty for pre-regime rows, regime-filtered query + backward compat, skeptic v2 feature slots + trainer fallbacks, as-of backfill idempotency — 11 tests, offline. |
| `tests/test_edge_miner.py` | Edge miner: 20h due-gate, Ollama/gcloud skip guards, new-triple diffing (reinforces excluded), full pull→mine→apply→refresh cycle with a fake gcloud runner (asserts scripts ship as FILES, never inline `-c` python over ssh), no-apply-when-nothing-new, failed-pull safety — 7 tests, offline. |
| `tests/test_evolution.py` | Procedural Evolution: cluster mining + provenance, hindsight buckets, counterfactuals, proposal/JSON schema gates, consensus-gate blocks + withdrawals, override_parameters crash-safe restore, portfolio metric math, all three backtest verdicts, lineage version chains, 4-section candidate markdown + real diff, orchestrator end-to-end, sleep-phase graceful skip — 14 tests, offline (scripted fake LLM). |
| `tests/test_resonance.py` | Phase 7 suite: macro matrix (snapshot fallback, mocked Dhan live path incl. DH-906 fail-open), news parser coercion (all Ollama HTTP mocked), resonance verdicts (the bear-put-vs-crude-crash CONFLICT scenario, strike/expiry-roll payloads, horizon blending), mode=ro graph guard — 27 tests, offline. |
| `tests/test_market_snapshot.py` | Phase 8 read-model: atomic write/read, staleness window, live_cycle publishing, the dashboard's zero-Dhan snapshot path + equity/spread mixed ladder + honest mark_source — 12 tests, offline. |
| `tests/test_rules.py` | Alert rule logic, offline. |
| `tests/test_portfolio.py` | Portfolio math + strategy proposals, plus the Phase 6G capital layer (margin lock/exhaustion boundaries, consecutive-loss drawdown scenarios, 10% risk-of-ruin halt, `run_headless` gate integration — in-memory DB). Offline. |
| `tests/test_forecast.py` | Forecast checklist scoring, offline (monkeypatches `suggestions.analyze`). |
| `tests/test_tuner.py` | Tuner weight learning, offline (fake journal entries). |

## Documentation suite (this file's siblings)

| File | Purpose |
|---|---|
| `OVERVIEW.md` | Vision + non-negotiables. Read first. |
| `ARCHITECTURE.md` | System flow diagrams + hosting/state model. |
| `MODULES.md` | This file. |
| `DECISIONS.md` | Log of locked architectural decisions + why. |
| `HANDOVER.md` | Cold-start: credentials, env vars, boot commands, current state. |
| `DATA_CONTRACT.md` | Exact JSON schemas + API contract for the frontend. |
| `PLAN.md` | Phase 4 (4A-4F) build plan and status — historical build log. |
| `VISION_PLAN.md` | User's Phase 5+ master blueprint (Discord, Brain Map, simulator, event ingestion). |
| `DECISIONS.md`, `PLAN.md`, `VISION_PLAN.md` are historical/planning docs and are NOT rewritten at every milestone the way `OVERVIEW`/`ARCHITECTURE`/`MODULES`/`HANDOVER` are — they're appended to as new phases/decisions land. |

# MODULES.md — Master Component Index

One-line-plus purpose for every file that matters. If a file isn't listed
here, it's either generated (`__pycache__/`), static assets, or trivial. For
system flow between these, see `ARCHITECTURE.md`.

## Data layer (market data)

| File | Purpose |
|---|---|
| `src/dhan_client.py` | THE market-data source. DhanHQ SDK wrapper: `SECURITY_ID_MAP` (verified against Dhan's scrip master), `get_daily_ohlc`, `get_ohlc_since`, `get_live_price`, `get_quote`, `get_daily_closes`, `get_option_chain`/`get_expiry_list`. Data-only — no order methods. |
| `src/data_fetcher.py` | Thin re-export of `dhan_client.get_quote` — kept for the original `get_quote(ticker)` contract older callers use. |
| `src/live_bridge.py` | Phase 6H live market-hour adapter: `fetch_live_market_state` (drop-in `fetch_fn=` for `run_market_loop` — live spot appended to the daily-close trend read), `parse_packet`/`CandleAggregator` (quote snapshots → 15-min OHLC), `evaluate_open_positions`/`live_cycle` (real-time advisory exit signals on open spreads via `plan_tracker`'s pure helpers, de-duped Discord alerts). READ-ONLY on all trade state (decision #41). Daemon: `python3 -m src.live_bridge`. |
| `src/indicators.py` | Pure-Python SMA and Wilder's RSI, no dependencies. |

## Knowledge graph

| File | Purpose |
|---|---|
| `src/graph_engine.py` | Phase 6C reasoning layer: `ensure_schema` (creates `graph_edges` table + temporal columns), `add_edge` (idempotent writer — stamps `valid_from`, clears `invalid_at` on reinforce), `GraphEngine` (loads ACTIVE edges only — `WHERE invalid_at IS NULL` — into a `networkx.DiGraph` for 2-hop BFS context queries). |
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
| `src/journal.py` | Appends every approved/rejected decision to `data/journal.jsonl` with signal, risk levers, pattern tags, plan block, outcome. |
| `src/plan_tracker.py` | Resolves OPEN plan-carrying trades against real daily OHLC high/low (stop/target/time-stop) — NOT a naive last-price check. Closes paper positions on resolution (bracket-order semantics). |
| `src/review.py` | Legacy 7-day price-drift scorecard for pre-plan (non-4B) journal entries. |
| `src/tuner.py` | Learning loop: scores resolved BUY archetypes (fresh-cross vs RSI-oversold), writes `data/brain_weights.json`, which `forecast.py` consumes. |
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
| `src/api.py` | Unified local FastAPI backend — the ONLY thing the React dashboard talks to. All `/api/*` routes (see `ARCHITECTURE.md`). Runs the hourly auto-sync background loop. |
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
| `tests/test_edge_miner.py` | Edge miner: 20h due-gate, Ollama/gcloud skip guards, new-triple diffing (reinforces excluded), full pull→mine→apply→refresh cycle with a fake gcloud runner, no-apply-when-nothing-new, failed-pull safety — 7 tests, offline. |
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

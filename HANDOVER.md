# HANDOVER.md — Cold-Start Brief

Read this to pick up the project cold in a new agent session. For vision see
`OVERVIEW.md`, for system flow see `ARCHITECTURE.md`, for the file index see
`MODULES.md`, for why past calls were made see `DECISIONS.md`. **This file is
updated only at milestone states, not on every commit** — check `git log`
for anything more recent than what's written here.

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

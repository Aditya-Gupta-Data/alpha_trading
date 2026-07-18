# next_gen_engine/ — Stage 4/5 Institutional Maturity (STAGED, NOT WIRED)

Built 2026-07-17 (Build Day, owner's master blueprint) as a **local staging
ground**: nothing here is imported by the live engine, scheduled, or
deployed. Each module is pure/injectable and unit-tested so review and the
later deploy are cheap.

**The anti-orphan rule applies** (learned the hard way with
`pattern_registry.py`/`trial_runner.py`): every module below names its
CANONICAL integration target. At deploy time the logic moves INTO that
target (or is wired from it) — this folder must never become a second,
drifting implementation of things `src/` already does.

### Phase 1 — Advanced portfolio risk management

| Module | What it adds | Canonical integration target |
|---|---|---|
| ~~`portfolio_risk_manager.py`~~ | **MERGED 2026-07-19** into its canonical target: the daily realized-loss circuit breaker now lives in `src/portfolio_manager.py` as an entry in the composed `ENTRY_HALT_CHECKS` list (review #2 halt-stack rule), beside the 10% trailing-drawdown halt. The staging file is deleted per the anti-orphan rule; its tests moved to `tests/test_margin_stress.py`. | done — see `src/portfolio_manager.py` |
| `wealth_flywheel.py` | Turns `src/wealth_lock.py`'s advisory sweep into a concrete PAPER ORDER (whole GOLDBEES units from a live quote, honest cash residual). | Extension of `src/wealth_lock.py` (`sweep_on_settlement` gains an order object); GOLDBEES id must be scrip-master-verified before any live pricing. |
| `trailing_stops.py` | Wilder-ATR chandelier trailing stop (ratchet, never widens; data gap retains previous stop). | `atr()` belongs in `src/indicators.py`; the advisory loop belongs in `src/live_bridge.py` next to the existing exit alerts. Roadmap item "ATR trailing stops". |

### Phase 2 — Execution quality (Stage 4)

| Module | What it adds | Canonical integration target |
|---|---|---|
| `execution_algo.py` | Limit-chasing paper execution plan (mid → walk to touch over 30s, tick-snapped, honest MISS never a phantom mid fill) + protective-leg-first sequencing for spreads. | `src/options_proposer.py`'s `_leg_fill` layer (decision #70 honest fills) — REFINES the paper fill model; there is no real order routing anywhere (paper-only). |

### Phase 3 — Thematic playbooks & wisdom extraction

| Module | What it adds | Canonical integration target |
|---|---|---|
| `wisdom_extractor.py` | Qualitative text (macro memo / transcript) → strict backtestable JSON frame (`target_sector`, `direction`, `timeframe_days`, `volatility_regime`, `fundamental_filters`, thesis, confidence). Unknown enums / out-of-range values drop to null — never a value the backtester can't trust. | **Routes through `src/text_intelligence.get_extractor()` (decision #74) — NOT a new LLM client** (owner constraint). At deploy becomes `src/ingestion/wisdom_extractor.py` beside `news_parser`, writing frames to the lake. Stays a text_intelligence *client* forever. |

### Phase 4 — Architecture scaling (EDA & WebSockets) — DRAFTS

| Module | What it adds | Canonical integration target |
|---|---|---|
| `redis_pubsub.py` | EDA adapter: `EventPublisher`/`EventSubscriber` + versioned envelope + `alpha.<domain>.<event>` channels. `redis` is an OPTIONAL dep (lazy import — never breaks the suite); logic tested against an in-memory `FakeBroker`. | NEW infra. Publish calls slot into `options_proposer` (proposal.created), `live_bridge` (position.exited / quote.tick), `portfolio_manager` (account.halted); Discord bridge + lake writer become subscribers. Draft until the owner commits to running a broker. |
| `dhan_websocket.py` | DhanHQ live-feed streaming TEMPLATE: binary header + ticker-packet decoders (`struct`, tested), `build_subscribe_frame`, `KeepAlive` ping/pong math, `backoff_schedule`. `websockets` lazy-imported on the run path only; async loop is a documented skeleton (`NotImplementedError`). | NEW real-time path → `src/ingestion/dhan_feed.py`, feeding candle capture / exit alerts from ticks and publishing onto the Phase-4 bus. Reuses `src/token_provider` — never a second credential source. |

**Optional dependencies (deliberately NOT in `requirements.txt`):** `redis`,
`websockets`. Both are imported lazily on their live paths only, so this
folder — and the full test suite — imports and runs on a box without them.
They get added to requirements as part of the Phase-4 adoption commit, not
before.

Tests: `tests/test_next_gen_engine.py` — 25 hermetic tests (no network, no
files, no clocks beyond injected values), collected by the main suite so
this folder can't silently rot.

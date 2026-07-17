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

| Module | What it adds | Canonical integration target |
|---|---|---|
| `portfolio_risk_manager.py` | DAILY realized-loss circuit breaker (halts new entries for the rest of the IST day). | Sits beside `src/portfolio_manager.py`'s existing 10% trailing-drawdown halt — gate call added in `gate_headless_entry`. Does NOT replace it. |
| `wealth_flywheel.py` | Turns `src/wealth_lock.py`'s advisory sweep into a concrete PAPER ORDER (qty from a live GOLDBEES quote). | Extension of `src/wealth_lock.py` (`sweep_on_settlement` gains an order object); GOLDBEES id must be scrip-master-verified before any live pricing. |
| `trailing_stops.py` | Wilder-ATR trailing stop calculator (ratchet, never widens). | `atr()` belongs in `src/indicators.py`; the advisory loop belongs in `src/live_bridge.py` next to the existing exit alerts. Roadmap item "ATR trailing stops" (post-observation queue). |
| `execution_algo.py` | Limit-chasing paper execution plan (mid → walk to touch over 30s) + protective-leg-first sequencing for spreads. | `src/options_proposer.py`'s fill layer (decision #70 honest fills) — this REFINES the paper fill model; there is no real order routing anywhere (paper-only system). |

Phases 3–4 of the blueprint (`wisdom_extractor.py` → must route through the
existing `src/text_intelligence.py` manager, decision #74, NOT a new LLM
client; `redis_pubsub.py` + DhanHQ WebSocket template → new infra deps,
draft-only) are deliberately not started yet per the owner's execution
order.

Tests: `tests/test_next_gen_engine.py` (hermetic, collected by the main
suite so this folder can't silently rot).

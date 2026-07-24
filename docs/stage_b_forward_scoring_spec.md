# Stage B — the 60-Session Forward-Scoring Clock (architecture)

> **Status: design spec, 2026-07-24 (owner directive: pivot from the deep-past
> data wall to clean, live, out-of-sample validation). The Strategy Registry's
> PREFER/SHOW verdicts today are IN-SAMPLE (historical episodes). Stage B builds
> the OUT-OF-SAMPLE record — what the recipes actually earn going forward — so a
> recipe can graduate from "looked good in history" to "proven live."**
>
> Companion to `docs/strategy_registry_spec.md`. Realizes spec §3-4's
> graduation clause and the macro engine's Law 4 ("shadow first").

---

## 0. The one-paragraph version (plain English)

Every night the engine already declares a regime and prints its ranked recipes
onto an immutable, timestamped ledger — *before* the outcome is knowable. Stage
B is the other half: a scoring pass that waits for the market to play forward,
measures what each declared recipe **actually returned** over its horizon, and
grades the call. Do that night after night and each recipe builds a **live,
out-of-sample track record**. After enough graded calls, the same Dept-5
rulebook that judges everything else decides whether the recipe's live
performance *confirms* its historical edge (promote) or *contradicts* it (kill).
No proxies, no glitchy history, no look-ahead — just the record of calls we made
in public and how they paid.

---

## 1. Design laws (inherited)

1. **Out-of-sample BY CONSTRUCTION.** A recipe is graded only against market
   data dated strictly AFTER its declaration timestamp. The immutable ledger is
   the proof; look-ahead is structurally impossible (the `timelock` contract).
2. **Advisory-only until graduated.** An in-sample PREFER earns nothing beyond a
   card. Only a forward-confirmed recipe earns real (risk-reducing, one-door)
   authority — through `regime_filters.advise`, never a new path.
3. **Wilson lower bound, never the headline.** The forward hit-rate is reported
   with its one-sided LB; promotion is decided on the honest number.
4. **NULL-honest.** A declaration whose forward window can't be priced (holiday
   gaps, a sector with no live data) is SKIPPED and named, never guessed.
5. **Forward and in-sample are NEVER pooled.** The live record is its own
   stratum (the real/sim-split discipline). A recipe's live n stands alone.
6. **Reuse, don't reinvent.** The ledger, the return math, stat_gates, the
   shadow-trade lifecycle, the nightly cron, and the weekly digest all exist.
   Stage B is one new module plus wiring.

---

## 2. What already exists (build FROM these)

- **The prediction ledger** — `declare()` already appends one immutable line
  per night to `logs/macro_regime_declarations.jsonl`, and (via SR-3) each
  declared horizon carries its `top_strategies` (name, ev, hit_rate, wilson_lb,
  **significant**) + `strategy_verdict`. This IS the "what we predicted, when"
  record. Stage B reads it; it does not touch it.
- **The return math** — `macro_playbooks.episode_phase_returns(anchor, …)` +
  `strategy_registry.evaluate_leg(spec, phases, phase)` compute a recipe's
  drift-removed return for ANY anchor date. Point them at a *declaration date*
  instead of a historical episode and they measure the forward window — same
  code path, zero new math.
- **The judge** — `validation/stat_gates` (Wilson LB, exact binomial) and the
  `validation/registry` lifecycle (CANDIDATE → VALIDATED → …, soft-only, every
  transition audited).
- **The precedent** — `discovery/shadow_runner` + `validation/trial`
  (`record_shadow_fire`/`resolve_shadow`) already run prospective shadow firings
  of registered PATTERNS into brain_map and resolve them from outcomes. Stage B
  is the exact analog for RECIPES: fire on a regime *declaration*, resolve on
  *forward index returns*.
- **The cron + the megaphone** — `macro_nightly` (VM, 19:50 IST) is the clock;
  `validation/digest` (Sat 10:00 IST Discord) is the weekly scoreboard.

---

## 3. The loop — LOG → RESOLVE → SCORE → ACCUMULATE → GRADE

```
  nightly declare()  ──►  logs/macro_regime_declarations.jsonl   (LOG, exists)
                                     │
  strategy_scorer (new) ────────────┘
     for each PAST declaration whose forward horizon has now elapsed:
        RESOLVE   is D + phase_horizon(P) <= today?  (else wait)
        SCORE     realized return of each declared recipe over [D, D+horizon],
                  via episode_phase_returns(anchor=D) + evaluate_leg   (reuse)
        ACCUMULATE append the graded call to logs/macro_strategy_scores.jsonl
                  and roll it into the per-recipe forward record
        GRADE      once a recipe has >= MIN_FWD_CALLS resolved, stat_gates
                  rules: does the LIVE hit-rate beat the drift-removed null?
```

Each step is idempotent and greppable; re-running the scorer never double-counts
(a call is keyed by (declaration_date, horizon, strategy_id)).

---

## 4. The Scorer — the one new module (`src/analysis/strategy_scorer.py`)

**Input:** the declaration ledger + the live macro lake.

**Resolution.** A declaration made on session `D` for phase `P` becomes
*resolvable* once `forward_horizon(P)` sessions have elapsed since `D`. Horizons
match the phase spans (configurable): shock P1 ≈ 10, P2 ≈ 34, P3 ≈ 74 sessions;
slow_burn proportionally longer. Un-elapsed declarations are left pending
(re-checked each night) — the embargo discipline from `validation/trial`.

**Scoring.** For a resolvable declaration, the scorer treats `D` as a live
anchor and runs the REAL registry evaluator: `episode_phase_returns(anchor=D)`
over the forward window, then `evaluate_leg` per declared recipe → the recipe's
realized drift-removed return, and a win = return > 0 (its structural null).
This is byte-identical to the in-sample math, only the anchor is a live date —
so what "win" means is exactly what it means historically.

**Which recipes.** ALL rendered recipes in a declared cell are scored (not just
the PREFER one), so today's SHOW recipes can earn forward validation and today's
PREFER recipes can be forward-*contradicted*. Each is stamped with its
in-sample verdict at declaration time, so we can later ask the money question:
*did in-sample PREFER predict live success better than SHOW?*

**Output.** One immutable line per graded call to
`logs/macro_strategy_scores.jsonl`:
`{declaration_date, resolved_on, horizon, archetype, phase, strategy_id, name,
in_sample_verdict, in_sample_significant, realized_return, win, null}`.

---

## 5. The scoreboard + graduation

**Rollup** (`data/strategy_scoreboard.json`, rebuilt each scoring pass) — per
recipe: `{in_sample: {verdict, hit, lb}, forward: {n_calls, wins, hit_rate,
wilson_lb}, status}`. `status ∈ ACCUMULATING | FORWARD_CONFIRMED |
FORWARD_CONTRADICTED | DORMANT (regime not seen lately)`.

**Graduation (Dept 5).** Once a recipe has `n_calls >= MIN_FWD_CALLS`, stat_gates
rules on the LIVE stratum alone: FORWARD_CONFIRMED when the forward hit-rate's
Wilson LB clears the drift-removed null; FORWARD_CONTRADICTED when it bleeds
below (auto-flag, one Discord card — the `validation/monitor` CUSUM/Wilson-
crossing pattern). Only FORWARD_CONFIRMED unlocks the risk-reducing execution
hook (spec §4), and only via a new owner ruling — the Review-#2 authority law.

**Honest note on `MIN_FWD_CALLS` and "60 sessions."** The 60-session clock is
CALENDAR time for the machine to run to the Oct-1 target, NOT a promise of 60
graded calls. A regime-conditioned recipe is only tested when its regime is
declared, and most nights declare "no confident match." So graded calls
accumulate SLOWLY and unevenly across regimes — the same analog-count reality as
the in-sample side, now with clean live data. The scoreboard states each
recipe's live n honestly; a recipe with 3 live calls is labeled ACCUMULATING,
not promoted. This is the correct, slow, honest path.

---

## 6. Cron wiring — exactly how the daily clock runs

`macro_nightly.run()` gains a fourth fail-open stage AFTER the declare tick:

```
  1. ingest FRED globals            (exists)
  2. ingest NSE indices for today   (exists)   ← the live sector data the scorer needs
  3. declare() onto the ledger      (exists)   ← LOG
  4. strategy_scorer.run()          (NEW)      ← RESOLVE + SCORE + ACCUMULATE + GRADE
```

Stage 4 is caught independently (a scorer error never aborts the clock or the
declaration), reads the lake in-process (e2-micro-safe — it's dict/return math,
no featurizer), and writes only its own ledger + scoreboard. One heartbeat line
notes calls_resolved / calls_pending so ops can see the clock ticking.

**Weekly**, `validation/digest` gains a Stage-B block: recipes forward-tracking,
calls graded this week (Wilson-CI'd), and any status transition (a recipe
crossing to FORWARD_CONFIRMED is the headline the owner waits for).

---

## 7. Build sequence

| Step | What | Lane | Cost |
|---|---|---|---|
| SB-1 | `strategy_scorer.py` — resolve + score matured declarations (reuse episode_phase_returns/evaluate_leg) + the graded-call ledger; timelock-tested | Dept 8/5, ~1 session | ₹0 |
| SB-2 | Wire stage-4 into `macro_nightly` (fail-open) + heartbeat line | Dept 8, ~½ session | ₹0 |
| SB-3 | `strategy_scoreboard.json` rollup + the weekly `digest` Stage-B block | Dept 5, ~1 session | ₹0 |
| SB-4 | Graduation logic (stat_gates on the live stratum) + FORWARD_CONFIRMED/CONTRADICTED transitions + one-card alerts | Dept 5, ~1 session | ₹0 |
| SB-5 | The 60 calendar sessions run (cron + ledger) — the clock itself, to the Oct-1 first verdict | — | ₹0 |

All ₹0, no new datastore (JSONL ledgers + one rollup artifact), no new authority
path. SB-1→SB-4 are a few focused sessions; SB-5 is calendar time on the VM cron
and runs regardless of subscription tier.

---

## 8. What this does NOT promise

- It will not grade 60 calls in 60 days. Declarations are sparse; live evidence
  accrues slowly and per-regime. That is honest, not a defect.
- It does not make an in-sample PREFER tradeable. Only forward confirmation
  through the Dept-5 court earns authority, and only with an owner ruling.
- It does not re-backtest itself. stat_gates rules on the immutable forward
  ledger — the record of public calls — never on a simulation of the engine.
- It cannot rescue a recipe the market contradicts. If energy stops leading at
  shock onset, the live record will say so, and the recipe is flagged — which is
  the entire point of paying to watch.

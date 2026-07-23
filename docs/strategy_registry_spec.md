# Strategy Registry — Regime-Conditioned Playbooks (PLANNING ONLY)

> **Status: design spec, 2026-07-23 (owner directive: cancel the Prop
> Desk Dashboard; build a Strategy Registry so a declared regime carries
> *what trades historically worked* in it). NO code exists yet. This is
> the pre-build artifact, written during the observation-week freeze —
> the freeze-legal activity (specs, not modules), same as
> `macro_regime_engine_spec.md` and `cycle_hunter_plan.md` before it.**
>
> **Owner's north-star sentence:** the nightly cron should stop saying
> only *"It's Regime S1"* and start saying *"It's Regime S1 —
> historically in S1, Strategy A returned X% (hit k/N), Strategy B
> returned Y%."* One regime, multiple viable strategies, each with an
> honest number attached.

---

## 0. The one-paragraph version (plain English)

We already know how to say *which pattern* the market is in (the Macro
Regime Engine, M1–M4, live). We do **not** yet say *what to do about it*.
The Registry closes that gap. A **strategy** is a small, declared trade
recipe — "go long Pharma," "long FMCG / short Metal," "rotate into the
defensive basket." We **replay every recipe across every historical
episode of each regime**, using the exact same index-level return math
the playbook table already uses, and we attach the honest result
(average return, win-rate, and the statistician's *lower bound* on that
win-rate) to the regime. When the nightly run declares a regime, it now
also prints the ranked recipes for that regime. **It moves no money** —
it is a forecaster on the record until Dept 5 scores it, exactly like the
regime call itself.

---

## 1. Design laws (inherited, non-negotiable)

These are the same laws as `macro_regime_engine_spec.md §0`. The Registry
does not get its own ethics.

1. **Abstention beats hallucination.** A strategy is ranked in a regime
   cell ONLY above a support floor and a separation floor. "No strategy
   separates from chance at S1's analog depth" is a first-class,
   shippable output — not a failure to paper over.
2. **Descriptive history, never a forecast of return.** Every number is
   "how this recipe did across N past analogs of this regime," stamped
   with N and the episode names. It is NEVER "expected return." (The
   `macro_playbooks.json` `advisory_note` already says exactly this; the
   Registry inherits the sentence verbatim.)
3. **Advisory-only, risk-reducing, one door.** The Registry advises
   nothing until Dept 5's `stat_gates` scores its ledger (spec Law 4). If
   it ever graduates, it flows through the ONE existing seam
   (`regime_filters.advise` → `build_proposal(advisory=)`), never a new
   authority path (Review #2 ruling).
4. **Shadow first.** The Registry publicly prints ranked strategies every
   night for ≥60 sessions and gets SCORED on them (did the top-ranked
   strategy actually beat the base rate out of sample?) before any
   advisory weight.
5. **NULL-honest, ₹0, offline-build / VM-read.** A missing sector close
   at a window edge contributes nothing (never a fabricated leg). The
   registry artifact is built on the Mac when the catalog/strategy set
   changes and READ on the VM (the fingerprint-cache discipline); the
   nightly never recomputes it on the e2-micro.

---

## 2. Vocabulary — and a naming collision we must respect

The word **"strategy" is already taken** in this codebase, twice:

- Dept 2 **option structures** — `strategy.py::StrategyConstructor`,
  `bull_put_spread`, `iron_condor` … and the `strategy_stats` MCP tool.
- `discovery/strategy_evidence.py` — "which *structure* has earned
  evidence for a pattern," REAL/SIM split, Wilson-LB ranking.

The Registry's strategies (sector rotations, long/short pairs) are a
**different species** — index-level, macro-horizon, not option legs. To
keep the firm's language unambiguous:

- The unit is a **`RegimeStrategy`** (a.k.a. a *playbook strategy*).
- The module is `src/analysis/strategy_registry.py`.
- The artifact is `data/macro_strategies.json`.
- The Dept-2 `strategy_*` names and `strategy_evidence` are **untouched**.

Lineage, stated plainly: the Registry is the **generalization of
`macro_playbooks.py`**. Today `macro_playbooks` iterates a hardcoded list
of `SECTORS` and, per (archetype, phase, sector), reports the excess-
return distribution. That is precisely one `RegimeStrategy` family —
`long_sector` — with the honesty layer deferred. The Registry lifts the
sector loop into a **strategy loop** and turns the honesty layer ON.

Reused vocabulary (unchanged, from the macro spec):
- **Episode** — a dated macro anchor (`config/macro_episodes.yaml`, 17
  shock + 7 slow_burn today).
- **Archetype** — DTW cluster of episodes (`A1..A4` shock, `S1..S2`
  slow_burn). The regime label the owner means by "S1."
- **Phase** — offset window within an archetype's horizon clock
  (`shock`: P1/P2/P3 on a 0–120-session clock; `slow_burn`: S1/S2/S3 on a
  0–500-session clock — `PHASES_BY_HORIZON`).
- **Cell** — one (archetype, phase) pair. The Registry's output is keyed
  by cell, just like the playbook table.

---

## 3. How a Strategy is defined in code

A `RegimeStrategy` is **declarative data + a pure evaluator** — never
arbitrary code in a cron. It is a frozen JSON spec whose canonical hash
is its `strategy_id` (the pattern-registry idea, `validation/registry.py`:
re-registration is a no-op, dead strategies stay dead — #49 lineage).

```jsonc
{
  "strategy_id": "<sha256 of the canonical spec>",   // frozen identity
  "name": "long_defensives_short_cyclicals",         // human handle
  "kind": "basket_rotation",                          // from a FIXED vocabulary
  "params": {                                          // kind-specific
    "longs":  ["NIFTY_FMCG", "NIFTY_PHARMA"],
    "shorts": ["NIFTY_METAL", "NIFTY_REALTY"]
  },
  "horizon": "shock",            // which phase clock it is scored on
  "hold": "phase",               // v1: enter at phase start, exit at phase end
  "thesis": "War Playbook: crises favour defensives over cyclicals",
  "source": "seed"               // seed | discovered | placebo
}
```

### 3.1 The `kind` vocabulary is fixed and small (v1)

Templates, not lambdas — three reasons: (a) **safety** (no `eval` in a
nightly job); (b) **look-ahead safety** (each template provably reads
only phase-window closes, so it cannot see the future — see §5); (c) each
template gets ONE `assert_future_blind` timelock test and a discovery
feature does not merge without it (existing law, `validation/timelock.py`).

| `kind` | `params` | return of one episode-leg |
|---|---|---|
| `long_sector` | `{sector}` | excess return of the sector vs NIFTY over the phase window (**= today's playbook cell, exactly**) |
| `long_short_pair` | `{long, short}` | `r(long) − r(short)` — market-neutral spread |
| `basket_rotation` | `{longs[], shorts[]}` | mean(r over longs) − mean(r over shorts); one-sided if `shorts` empty (long basket vs NIFTY) |
| `benchmark_tilt` | `{sector, weight}` | `w·r(sector) + (1−w)·r(NIFTY) − r(NIFTY)` — overlay vs pure index |

**One family in v1, index-level only** (owner ruling 2026-07-23 —
*exclude options for now*). Every `kind` above is **return-bearing**:
each leg is a real NIFTY sector-index close we already ingest, so every
"return" is a real buyable-proxy number (sector ETF / index future) and
**the inflated options simulator is nowhere in this path**. Options
structures are **deferred to the roadmap (§11 Stage D)**, where they
re-enter only through a door that does not lie — never the ~10× synthetic
chain sim. This is the leanest honest surface; it ships and scores first.

### 3.2 The evaluator (pure, NULL-honest)

```
evaluate_leg(spec, anchor, phase_window, lake) -> float | None
```

Reuses `macro_playbooks._range_return` verbatim (first usable close ≥
window-start, last usable close ≤ window-end, edges snap INWARD, `None`
when < 2 usable sessions). A leg that cannot be priced (sector history
starts 2019-10; pre-2019 episodes have no sector legs) returns `None` and
**contributes nothing** — never a guessed 0. This is the same rule that
gives `macro_playbooks` its `episodes_without_sector_legs` honesty.

---

## 4. How we backtest it into win-rates / EV

The backtest **is `build_playbooks` with the sector loop replaced by a
strategy loop, and the honesty layer switched on.** No new statistics are
invented; every number below is an existing `stat_gates` primitive.

For each `RegimeStrategy S`, for each cell `(archetype A, phase P)` on
S's horizon:

1. **Gather analog legs.** For every member episode `e ∈ A`, compute
   `r_e = evaluate_leg(S, anchor(e), window(P))`. Drop `None`s. Result:
   `{episode: return}` over ≤ |A| analogs (A1 has 8; S1 has 5; fewer
   where sector history is missing).
2. **Aggregate honestly** (`macro_playbooks._aggregate` shape, extended):
   `n`, `mean_return` (**this is the EV**), `median`, `min`, `max`,
   `hit_rate` (share with return > 0), and the named per-episode returns.
3. **Attach the statistician's rails** (`validation/stat_gates`):
   - **Wilson lower bound** on `hit_rate` — the honest win-rate number.
     Ranking is on the LOWER BOUND, never the headline (the
     `strategy_evidence.py` rule: a 6/7 = 86% whose LB is 47% does NOT
     out-rank a 12/15 = 80% whose LB is 58%).
   - **Structural null** — a strategy must beat the *unconditional* base
     rate of its own construction (a long/short pair's null is 50% up-
     moves of that spread across ALL history, never a naive 50% — the
     circularity fix, `stat_gates` structural breakeven).
   - **Support floor** `MIN_EPISODE_LEGS = 5` (owner ruling 2026-07-23,
     matching `strategy_evidence`). A cell with fewer real analog legs is
     **not rendered** — a 4-analog "win-rate" is noise wearing a
     percentage. **Consequence, stated honestly (§6.2):** with sector
     index history starting only 2019-10, most archetypes have < 5
     post-2019 sector legs, so at floor 5 the *sector-rotation* strategies
     mostly ABSTAIN until the pre-2019 sector CSVs are ingested; the
     NIFTY-level strategies (history to 1995) still render.
4. **Correct for multiple testing across the WHOLE build.** We are
   scoring `n_strategies × n_cells` hypotheses at once; that is the #1
   overfitting risk. **Benjamini–Hochberg** (`stat_gates.benjamini_
   hochberg`) runs over the entire registry build so a catalog of 30
   strategies × 12 cells cannot surface a lucky one as "significant."
5. **Placebo meter** (`validation/placebo.py` + `noise.py` pattern). The
   build INCLUDES information-free strategies — `source:"placebo"`: "long
   a fixed-but-arbitrary sector," "long/short an arbitrary pair." They
   flow through the identical pipeline. Their realized survival rate is
   the **false-discovery thermometer**: if placebo strategies rank as
   high as real ones, the Registry is measuring noise, and the **kill
   criterion fires** (cycle_hunter_plan.md) — we ship "no separation,"
   not a laundered ranking.

Output per cell = a **ranked list** of surviving strategies:
`[{name, kind, params, n, mean_return (EV), hit_rate, wilson_lb,
verdict}]`, `verdict ∈ PREFER | SHOW | ABSTAIN` (PREFER only when the top
strategy's LB clears the runner-up's headline — genuine separation, the
`strategy_evidence` verdict rule; SHOW = clears floors but no separation;
the cell abstains when nothing clears).

Artifact `data/macro_strategies.json` mirrors `macro_playbooks.json`'s
structure: `built_at`, `params` (floors, BH q, phase clocks,
`source_templates_built_at`), `strategies` (the frozen specs),
`table[archetype][phase]` → ranked list, `placebo_report` (the FDR
meter), and the inherited `advisory_note`. Built on the **Mac lane**,
rebuilt only when the episode catalog OR the strategy set changes — pure
offline compute, kilobytes.

---

## 5. Look-ahead & leakage — the thing that kills backtests

Three distinct leaks, three explicit guards:

1. **Intra-strategy look-ahead.** Each template reads only the phase
   window's own closes → provably future-blind, enforced by an
   `assert_future_blind` timelock test per template (§3.1). No strategy
   merges without it.
2. **Fitting strategies to their own scores.** A human must NOT hand-pick
   "long whichever sector happened to win in S1." Guard: strategies are
   **registered by frozen hash BEFORE the backtest reveals their score**,
   they come from a-priori economic theses (the War Playbook is the
   model) or from the unsupervised discovery layer (AD-*) proposing into
   the court — and the **placebo meter measures whether the surviving set
   actually beats arbitrary recipes.** If it doesn't, we built a noise
   machine and we say so.
3. **The archetype-assignment leak (the subtle one).** An episode's
   archetype is derived from its *post-anchor* trajectory — it is only
   knowable after the fact. That is FINE for the *descriptive* registry
   ("in past S1 episodes, X worked") but it means the Registry answers a
   **conditional**: *IF today is S1, here is what worked in past S1s.*
   The "IF" is `declare()`'s job and carries its own risk — the DTW
   similarity score and analog count. The Registry never owns the regime
   call; it owns only the "given the regime" clause. Every output states
   this so nobody reads a conditional as a promise.

---

## 6. The honesty layer — keeping real and inflated numbers apart

### 6.1 v1 is index-level only — the sim-inflation trap is simply not entered

The standing **sim-realism caveat** (`simulator.py` backtest P&L is
synthetic-option-chain INFLATED ~10×, 62–79% generosity band) is about
the **options** simulator. v1 never touches it: every strategy leg is a
real NIFTY sector-index close, measured by the same `_range_return` that
already builds the playbook table. No synthetic chain, no modeled
premium, no 10× inflation — the returns are what the indices actually
did. Options structures are **deferred to §11 Stage D**, and when they
return it is through honest doors only (a real-volatility *verdict* read
from live VIX, or the real forward option-chain history the
`chain_archiver` is already capturing) — never the inflated sim.

### 6.2 The caveats stated in the artifact, not hidden

- **Tiny n — the load-bearing constraint.** 17 shock + 7 slow-burn
  episodes is the whole universe. A1 caps at 8 analogs, S1 at 5, and at
  the owner's `MIN_EPISODE_LEGS = 5` **only A1, A2 and (at full coverage)
  S1 can ever render a strategy** — A3/A4/S2 will honestly show
  "insufficient analogs." This is why Wilson-LB ranking, the floor, BH,
  and the placebo meter are load-bearing, not decoration. Power is
  **analog-count-limited by construction**; the Time Machine backfill
  (cycle_hunter Phase A) is what deepens it.
- **Sector history floor 2019-10 × floor 5 = sector rotations mostly
  abstain today.** Most archetypes have < 5 post-2019 sector legs, so the
  sector-rotation strategies — the ones this whole directive is about —
  will ABSTAIN until the owner drops the pre-2019 NIFTY sector CSVs
  (FMCG/AUTO/BANK/IT/PHARMA + India VIX, 2013–2018) into the
  `index_history` clerk (the same pending download already noted for M3).
  This is not a bug; it is the data gap showing itself honestly — and a
  named, numbers-backed blocker of exactly the kind the G1 proof-gate
  wants. Until then the Registry speaks mostly in NIFTY-level strategies.
- **No transaction costs in v1** on the index legs (the playbook table
  has none either). Documented; a cost model is a later refinement, not a
  silent omission.

---

## 7. How `declare()` queries the Registry

Minimal, surgical — `declare()` in `macro_regime.py` **already** computes
`playbook_slice = _playbook_slice(playbooks, archetype, phase)` per
declared horizon. We add its sibling:

```python
verdict["strategy_slice"] = registry.top_strategies(
    archetype, phase, k=TOP_K, registry_path=..., require_cache=require_cache)
```

- `top_strategies` is a **dict lookup into the pre-built artifact** —
  e2-micro-cheap, zero featurizer pass. It honors the SAME
  `require_cache` discipline as the fingerprint cache: a missing/stale
  `macro_strategies.json` yields `{"status": "unavailable", ...}`, never
  a recompute, never a crash (the silence-ban: the status is stamped, and
  `macro_nightly` raises it as a loud ops line just like a cache miss).
- The state doc + the immutable ledger line gain a `strategies` block per
  horizon (the ranked list from §4). **The ledger already exists and is
  already scored** — strategy calls ride the same
  `logs/macro_regime_declarations.jsonl` and get graded by the same Dept-5
  clock (did the declared cell's PREFER strategy beat the base rate over
  the next 20/60 sessions?).
- The **family-transition Discord card** (already fires on
  declare/undeclare/phase-change) gains one line: the cell's top
  strategy. Daily sameness stays silent (the tier-engine card
  discipline).

The owner's north-star sentence, assembled from the slice:

> 🧭 **slow_burn: S1 · S1_buildup** (analog `el_nino_2023_24`, sim 0.74)
> · over 5 analogs: **long_defensives_short_cyclicals** +6.2% (hit 4/5,
> LB 38%) · **long_FMCG** +3.1% (hit 4/5, LB 38%) · [3 more]

---

## 8. Product leg (the moat = the demo)

A new brain-MCP tool `regime_strategies(archetype, phase)` returns the
ranked cell as **facts and scores only, never buy/sell verbs** (the SEBI
posture, `cycle_hunter_plan.md` Phase D). It sits beside the existing
`market_regime` and `strategy_stats` tools in `brain_mcp.py`. The graded
declaration ledger IS the sales demo: "here is every regime+strategy call
we made, timestamped, and how each scored." The moat and the product are
the same artifact — no separate build.

---

## 9. Build sequence, cost, and the rulings I need

| Step | What | Lane | Cost |
|---|---|---|---|
| SR1 | `RegimeStrategy` spec + `kind` templates + evaluator + one timelock test per template | Dept 8, ~1 session | ₹0 |
| SR2 | Registry builder (generalize `build_playbooks` over the strategy set) + `stat_gates` layer + placebo strategies → `data/macro_strategies.json` | Dept 8, ~1 session | ₹0 |
| SR3 | `declare()` integration — `strategy_slice`, ledger `strategies` block, card line, `require_cache` discipline | Dept 8/4, ~1 session | ₹0 |
| SR4 | brain-MCP `regime_strategies` tool + Dept-5 scoring hook (grade strategy calls on the existing ledger) | Dept 5/7, ~1 session | ₹0 |
| SR5 | 60-session public shadow scoring (calendar time, not build time) — earns authority or doesn't | Dept 5 | ₹0 |

SR1–SR4 fit inside the Max window (before Aug 8) alongside the existing
schedule; SR5 runs through August on cron regardless of subscription
tier. **Total new spend: ₹0** — rides the proof-gate plan without opening
a gate. No new datastore, no new door, no new authority.

**Owner rulings received 2026-07-23 (locked into this spec):**

1. **Scope = index-level return family ONLY** (owner: *exclude options
   for now*, 2026-07-23 — reversing the earlier include). `long_sector`,
   `long_short_pair`, `basket_rotation`, `benchmark_tilt`. The inflated
   options sim never enters the path; options are deferred to §11 Stage D.
2. **`MIN_EPISODE_LEGS = 5`** (matches `strategy_evidence`). Consequence
   accepted and documented (§6.2): sector rotations mostly abstain until
   the pre-2019 sector CSVs land; NIFTY-level strategies render now.
3. **A-priori seeds only for v1** — ~10–15 War-Playbook theses + the
   `long_sector` baselines, **no hand-picking winners** (frozen-hash
   before scoring). AD-* discovery proposing strategies into the court is
   **deferred** to a later step (not v1).

---

## 10. What this does NOT promise

- It will not rank a strategy in every cell. Most thin cells will
  ABSTAIN — that is correct behaviour, and the Discord silence rule makes
  it cheap.
- It will not turn 5 analogs into a robust edge. The floors, Wilson LB,
  BH, and placebo meter are there precisely so it can admit "no
  separation" instead of inventing one.
- It does not move a rupee on day one. Every strategy call earns its
  authority through the same Dept-5 court the regime call itself faces.
- It is not the champion/challenger duel (`strategy_evidence`'s deferred
  cousin). It READS and RANKS descriptive history; it does not auto-pick
  or auto-apply a strategy (#49, human-gated).

---

## 11. Roadmap beyond v1 — planning ahead

v1 is deliberately the smallest honest thing: index-level recipes,
a-priori seeds, **on the record only**. Everything below is the arc that
follows, and each stage is gated on a **named unlock** (data, calendar, a
build burst, or an owner ruling) — never "someday." The order is the
dependency order; a later stage is a waste of effort until its predecessor
proves out.

| Stage | What it adds | Gated on (the named unlock) | When / cost |
|---|---|---|---|
| **v1** (SR1–SR4, §9) | The Registry itself: index-level recipes ranked per regime, printed nightly, put on the immutable ledger. NIFTY-level strategies render; sector rotations mostly abstain. | Thursday 07-24 bug-review clears (the build-order rule) | pre-Aug-8 · ₹0 |
| **A — wake the sector rotations** | The sector-rotation & long/short recipes (the exciting ones) start rendering, not abstaining. | Owner drops the 2013–2018 NIFTY sector CSVs into the `index_history` drop-folder (the same download M3 already needs) | anytime · ₹0 |
| **B — the scoring verdict** ⭐ | The truth test. 60 forward sessions of declarations get graded by Dept 5: did the top-ranked recipe actually beat the base rate? This is the pass/fail that decides everything after it. | 60 calendar sessions on the cron (≈ the Oct-1 ledger target) | Aug→Oct · ₹0 · runs on Pro too |
| **C — let the machine propose recipes** | The unsupervised discovery layer (AD-\*) nominates NEW strategies into the same court, scored out-of-sample. Moves us from "human wrote the playbook" to "the data suggests the playbook." | **B shows the a-priori seeds have real signal**, + a Max-sized build burst (gate G5) | post-Aug-8 · G5 |
| **D — options, done right** | Options structures re-enter — the thing you asked for, delivered honestly: either as a real-volatility verdict, or priced off the **real** forward option-chain history the engine is already capturing (never the ~10× sim). | Enough real chain history has accrued (`chain_archiver`, capturing since Phase 0) **or** paid chain history (gate G1) | longer horizon |
| **E — graduation to real nudges** | The strategies stop being "on the record only" and begin gently steering the desk — first as caution/sizing dampers, later (a further ruling) as positive tilts — through the ONE existing risk-reducing seam. | **B passes** + a new owner ruling (Review-#2 authority law) | after Oct 1 |
| **F — the product leg** | The graded registry becomes the premium brain-MCP tool: "here's every regime+strategy call we made, timestamped, and how each scored." The moat and the demo are the same artifact. | Stealth Mode lifts (≥ Oct 1 per the current directive) + **B passes** | ≥ Oct 1 |

**The throughline:** build the smallest honest thing (v1) → make it
speak fully (A) → **let it prove or disprove itself on the record (B)** →
and only then spend real effort/money widening it (C/D) or letting it
touch money (E) or selling it (F). Stage **B is the hinge** — if the
Registry's calls don't beat the base rate over 60 honest sessions, the
[[project-cycle-hunter-directive]] kill criterion says we stop and
reassess, not throw more data or money at it. Nothing past B is worth
building until B says "yes."
